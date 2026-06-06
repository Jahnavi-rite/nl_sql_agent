from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

import structlog

try:
    from autogen import AssistantAgent
    AUTOGEN_AVAILABLE = True
except ImportError:
    AssistantAgent = None
    AUTOGEN_AVAILABLE = False

from app.core.config import settings

logger = structlog.get_logger()


class DebateParticipant(ABC):
    name: str

    @abstractmethod
    async def a_generate_reply(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        default_reply: str | None = None,
    ) -> dict[str, Any]:
        ...


def _build_llm_config() -> dict[str, Any]:
    return {
        # AutoGen 0.2 talks to the endpoint via the openai SDK directly (not
        # litellm), so the model must be the bare deployment id the proxy
        # registered (e.g. "qwen3.6") — a litellm "openai/" prefix would 401.
        "model": settings.OPENAI_MODEL_RAW,
        "api_key": settings.OPENAI_API_KEY,
        "base_url": settings.OPENAI_API_BASE.rstrip("/"),
        "temperature": settings.LLM_TEMPERATURE,
        "timeout": settings.LLM_TIMEOUT_SECONDS,
    }


class DebateAuthor(DebateParticipant):
    def __init__(self) -> None:
        self.name = "DebateAuthor"
        if AssistantAgent is None:
            raise RuntimeError("pyautogen is not available; debate feature requires Python <3.13 or pyautogen>=0.10.0 with updated API")
        self.agent = AssistantAgent(
            name=self.name,
            system_message=self._system_prompt(),
            llm_config=_build_llm_config(),
            human_input_mode="NEVER",
        )

    def _system_prompt(self) -> str:
        return (
            "You are the DebateAuthor. Your role is to generate and revise SQL SELECT queries.\n\n"
            "For each round, you will receive:\n"
            "- The user's natural language request\n"
            "- The SQL dialect (postgres or oracle)\n"
            "- Database schema metadata\n"
            "- (Optional) Feedback from the DebateCritic\n\n"
            "You must output ONLY a valid JSON object with exactly these keys:\n"
            '{\n'
            '  "query_sql": "<single SELECT statement>",\n'
            '  "rationale": "<brief explanation of the query>",\n'
            '  "confidence": <float 0.0-1.0>,\n'
            '  "description": "<what changed from previous version, if any>"\n'
            "}\n\n"
            "CRITICAL RULES:\n"
            "1. query_sql must be a SINGLE SELECT statement ONLY (or WITH ... SELECT)\n"
            "2. query_sql must NEVER contain: DROP, DELETE, TRUNCATE, ALTER, GRANT, REVOKE, INSERT, UPDATE, MERGE, CREATE, CALL, EXECUTE, DECLARE, BEGIN, COMMIT, ROLLBACK, COPY\n"
            "3. query_sql must reference ONLY tables/columns from the provided schema\n"
            "4. query_sql must NOT contain comments or multi-statement SQL\n"
            "5. confidence must be a float 0.0-1.0\n"
            "6. When addressing critic feedback, specifically address every objection raised\n"
            "7. Output ONLY the JSON object - no markdown, no extra text\n"
        )

    async def a_generate_reply(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        default_reply: str | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        start_ms = time.perf_counter()
        raw_response = ""
        error: str | None = None
        input_tokens = 0
        output_tokens = 0

        try:
            raw_response = await self.agent.a_generate_reply(
                messages=messages,
                default_reply=default_reply or "{}",
            )
            input_tokens = getattr(raw_response, "usage", {}).get("prompt_tokens", 0) or 0
            output_tokens = getattr(raw_response, "usage", {}).get("completion_tokens", 0) or 0
            raw_response = raw_response.content if hasattr(raw_response, "content") else str(raw_response)
        except Exception as exc:
            error = str(exc)
            logger.error("author_generate_failed", error=error)
            raw_response = default_reply or "{}"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start_ms) * 1000
            logger.info(
                "author_llm_call",
                request_id=request_id,
                latency_ms=round(elapsed_ms, 1),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=error,
            )

        parsed = _extract_author_json(raw_response)
        parsed.setdefault("query_sql", "")
        parsed.setdefault("rationale", "")
        parsed.setdefault("confidence", 0.0)
        parsed.setdefault("description", "")
        parsed.setdefault("token_usage", {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_ms": round(elapsed_ms, 1)})

        return parsed


class DebateCritic(DebateParticipant):
    def __init__(self) -> None:
        self.name = "DebateCritic"
        if AssistantAgent is None:
            raise RuntimeError("pyautogen is not available; debate feature requires Python <3.13 or pyautogen>=0.10.0 with updated API")
        self.agent = AssistantAgent(
            name=self.name,
            system_message=self._system_prompt(),
            llm_config=_build_llm_config(),
            human_input_mode="NEVER",
        )

    def _system_prompt(self) -> str:
        return (
            "You are the DebateCritic. Your role is to rigorously review SQL queries and must find "
            "at least one concrete flaw before you can approve any query.\n\n"
            "For each review, you will receive:\n"
            "- The user's natural language request\n"
            "- The SQL dialect\n"
            "- The proposed SQL query from DebateAuthor\n"
            "- The Author's rationale and confidence\n"
            "- Database schema metadata\n\n"
            "You must output ONLY a valid JSON object with exactly these keys:\n"
            '{\n'
            '  "approved": <true or false>,\n'
            '  "objections": ["<list of specific concrete flaws>"],\n'
            '  "scores": {\n'
            '    "semantic_correctness": <0.0-1.0>,\n'
            '    "edge_cases": <0.0-1.0>,\n'
            '    "performance": <0.0-1.0>,\n'
            '    "best_practices": <0.0-1.0>\n'
            "  },\n"
            '  "rationale": "<detailed explanation of your reasoning>",\n'
            '  "confidence": <float 0.0-1.0 representing your overall score>,\n'
            '  "suggestions": "<specific actionable improvements>"\n'
            "}\n\n"
            "CRITICAL RULES:\n"
            "1. You MUST find at least one concrete flaw before approving (approved: false)\n"
            "2. Be adversarial and thorough - check for SQL injection risks, incorrect JOINs, missing WHERE clauses, wrong aggregations, performance issues, etc.\n"
            "3. semantic_correctness: Does the SQL correctly answer the user's question?\n"
            "4. edge_cases: Does it handle NULLs, empty results, boundary conditions?\n"
            "5. performance: Is it efficient? Does it use proper indexes? Avoid unnecessary subqueries?\n"
            "6. best_practices: Does it follow SQL style, use proper formatting, explicit columns?\n"
            "7. confidence is the average of your four scores\n"
            "8. Output ONLY the JSON object - no markdown, no extra text\n"
        )

    async def a_generate_reply(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        default_reply: str | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        start_ms = time.perf_counter()
        raw_response = ""
        error: str | None = None
        input_tokens = 0
        output_tokens = 0

        try:
            raw_response = await self.agent.a_generate_reply(
                messages=messages,
                default_reply=default_reply or "{}",
            )
            input_tokens = getattr(raw_response, "usage", {}).get("prompt_tokens", 0) or 0
            output_tokens = getattr(raw_response, "usage", {}).get("completion_tokens", 0) or 0
            raw_response = raw_response.content if hasattr(raw_response, "content") else str(raw_response)
        except Exception as exc:
            error = str(exc)
            logger.error("critic_generate_failed", error=error)
            raw_response = default_reply or "{}"
        finally:
            elapsed_ms = (time.perf_counter() - start_ms) * 1000
            logger.info(
                "critic_llm_call",
                request_id=request_id,
                latency_ms=round(elapsed_ms, 1),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=error,
            )

        parsed = _extract_critic_json(raw_response)
        parsed.setdefault("approved", False)
        parsed.setdefault("objections", [])
        if not isinstance(parsed["objections"], list):
            parsed["objections"] = [str(parsed["objections"])]
        parsed.setdefault("scores", {})
        parsed.setdefault("rationale", "")
        parsed.setdefault("confidence", 0.0)
        parsed.setdefault("suggestions", "")
        parsed.setdefault("token_usage", {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_ms": round(elapsed_ms, 1)})

        return parsed


def _extract_author_json(raw: str) -> dict[str, Any]:
    import json

    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        content_lines: list[str] = []
        in_code = False
        for line in lines:
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                content_lines.append(line)
        stripped = "\n".join(content_lines).strip()

    try:
        val = json.loads(stripped)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            val2 = json.loads(stripped[start : end + 1])
            if isinstance(val2, dict):
                return val2
        except json.JSONDecodeError:
            pass
    return {"query_sql": "", "rationale": "", "confidence": 0.0, "description": ""}


def _extract_critic_json(raw: str) -> dict[str, Any]:
    import json

    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        content_lines: list[str] = []
        in_code = False
        for line in lines:
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                content_lines.append(line)
        stripped = "\n".join(content_lines).strip()

    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            if "scores" in result and not isinstance(result["scores"], dict):
                result["scores"] = {}
            return result
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            result2 = json.loads(stripped[start : end + 1])
            if isinstance(result2, dict):
                if "scores" in result2 and not isinstance(result2["scores"], dict):
                    result2["scores"] = {}
                return result2
        except json.JSONDecodeError:
            pass
    return {"approved": False, "objections": ["Parse error"], "scores": {}, "rationale": "", "confidence": 0.0, "suggestions": ""}
