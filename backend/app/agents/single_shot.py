from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()

SYSTEM_PROMPT = """You are a SQL generation engine. Given a natural language request, a SQL dialect, and the available database schema metadata, you must output ONLY valid JSON with no markdown, no code fences, no extra text.

Your response must be a single JSON object with exactly these keys:
{
  "query_sql": "The SELECT-only SQL query that answers the user's question using the provided schema",
  "confidence": <float between 0 and 1 indicating how confident you are this is correct>,
  "rationale": "Brief explanation of what you understood the request to mean and how the SQL addresses it"
}

CRITICAL RULES:
1. Output ONLY the JSON object - no other text, no markdown, no code fences, no explanations, no notes
2. query_sql must be a SINGLE SELECT statement ONLY (or WITH ... SELECT)
3. query_sql must NEVER contain: DROP, DELETE, TRUNCATE, ALTER, GRANT, REVOKE, INSERT, UPDATE, MERGE, CREATE, CALL, EXECUTE, DECLARE, BEGIN, COMMIT, ROLLBACK, COPY, or any DDL/DML
4. query_sql must reference ONLY tables and columns that exist in the provided schema metadata
5. query_sql must NOT contain comments (/* */ or --)
6. query_sql must NOT contain multi-statement SQL (only one statement)
7. Do NOT generate dangerous functions like pg_sleep, pg_read_file, utl_file, etc.
8. Do NOT access system tables like pg_catalog, information_schema, dba_*, v$*
9. confidence must be a float between 0.0 and 1.0
10. rationale must be a concise string explaining intent interpretation
11. All SQL in query_sql must be dialect-correct for the specified dialect"""


class AgentResponse:
    def __init__(
        self,
        query_sql: str,
        confidence: float,
        rationale: str,
    ) -> None:
        self.query_sql = query_sql
        self.confidence = confidence
        self.rationale = rationale


class AgentError(Exception):
    pass


class JSONParseError(AgentError):
    pass


class LLMError(AgentError):
    pass


def _build_messages(prompt: str, dialect: str, schema_metadata: str) -> list[dict[str, str]]:
    user_content = (
        f"Dialect: {dialect}\n"
        f"Request: {prompt}\n\n"
        f"Available Schema:\n{schema_metadata}\n\n"
        f"Generate the JSON response with query_sql, confidence, and rationale. "
        f"query_sql must be a single SELECT statement only."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


async def _call_llm(messages: list[dict[str, str]]) -> str:
    api_key = settings.OPENAI_API_KEY
    model = settings.OPENAI_MODEL
    base_url = settings.OPENAI_API_BASE.rstrip("/")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": 4096,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            content: str = data["choices"][0]["message"]["content"]
            return content
    except httpx.TimeoutException:
        raise LLMError(f"LLM request timed out after {settings.LLM_TIMEOUT_SECONDS}s") from None
    except httpx.HTTPStatusError as exc:
        raise LLMError(f"LLM API returned {exc.response.status_code}: {exc.response.text[:500]}") from exc
    except (httpx.RequestError, KeyError, json.JSONDecodeError) as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc


def _extract_json(raw: str) -> dict[str, Any]:
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
        result: dict[str, Any] = json.loads(stripped)
        return result
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                result2: dict[str, Any] = json.loads(stripped[start : end + 1])
                return result2
            except json.JSONDecodeError:
                pass
        raise JSONParseError(f"Failed to parse LLM response as JSON: {stripped[:500]}") from None


def _validate_response(data: dict[str, Any]) -> None:
    required = {"query_sql", "confidence", "rationale"}
    missing = required - set(data.keys())
    if missing:
        raise JSONParseError(f"Missing required fields: {missing}")

    if not isinstance(data["query_sql"], str) or not data["query_sql"].strip():
        raise JSONParseError("Field 'query_sql' must be a non-empty string")

    confidence = data["confidence"]
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        raise JSONParseError(f"confidence must be a float between 0 and 1, got {confidence!r}")

    if not isinstance(data["rationale"], str) or not data["rationale"].strip():
        raise JSONParseError("rationale must be a non-empty string")


async def generate(
    prompt: str,
    dialect: str,
    *,
    schema_metadata: str = "",
    request_id: str | None = None,
) -> AgentResponse:
    rid = request_id or str(uuid.uuid4())
    messages = _build_messages(prompt, dialect, schema_metadata)
    start_ms = time.perf_counter()

    raw_response = await _call_llm(messages)
    elapsed_ms = (time.perf_counter() - start_ms) * 1000

    logger.info(
        "llm_call_completed",
        request_id=rid,
        model=settings.OPENAI_MODEL,
        latency_ms=round(elapsed_ms, 1),
        response_length=len(raw_response),
    )

    parsed = _extract_json(raw_response)
    _validate_response(parsed)

    return AgentResponse(
        query_sql=parsed["query_sql"].strip(),
        confidence=float(parsed["confidence"]),
        rationale=parsed["rationale"].strip(),
    )
