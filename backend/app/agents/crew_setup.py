from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from app.agents.single_shot import DOMAIN_CONTEXT, SQL_RULES
from app.core.config import settings


def _validate_sql_guard(sql_with_dialect: str) -> str:
    """Validates a SQL query for safety using AST-based analysis. Input: the SQL query text followed by |dialect (e.g. SELECT * FROM t|postgres)."""
    from app.validators.sql_guard import UnsafeSQLError, ValidationMode, validate_or_raise

    parts = sql_with_dialect.rsplit("|", 1)
    sql = parts[0].strip()
    dialect = parts[1].strip() if len(parts) > 1 else "postgres"

    try:
        validate_or_raise(sql, dialect, ValidationMode.QUERY_UNDER_TEST)
        return json.dumps({"valid": True, "message": "SQL validation passed"})
    except UnsafeSQLError as e:
        return json.dumps({"valid": False, "reasons": e.reasons})
    except Exception as e:
        return json.dumps({"valid": False, "reasons": [str(e)]})


def _sql_guard_tool() -> Any:
    from crewai.tools import tool

    return tool("SQLGuard")(_validate_sql_guard)


ROLE_AGENT_NAMES = {
    "Intent Analyst": "intent_analyst",
    "SQL Query Author": "query_author",
    "SQL Safety Critic": "critic",
}


def agent_name_for(role: Any) -> str:
    role_str = str(getattr(role, "role", None) or role or "agent")
    return ROLE_AGENT_NAMES.get(role_str, role_str.lower().replace(" ", "_"))


def _llm_config() -> Any:
    from crewai import LLM

    return LLM(
        model=settings.OPENAI_MODEL_LITELLM,
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_API_BASE,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


def make_step_callback(
    sid: str,
    rid: str,
) -> Callable[[Any], None]:
    from app.services.stream_events import make_progress
    from app.services.stream_manager import stream_manager

    def _emit(event: Any) -> None:
        stream_manager.publish_event(sid, event.to_dict())

    def callback(step: Any) -> None:
        agent_role = getattr(step, "agent_role", None) or getattr(step, "agent", None) or "agent"
        agent_name = agent_name_for(agent_role)
        description = getattr(step, "description", "") or ""
        _emit(make_progress(agent_name, 50.0, description[:120], rid))

    return callback


def make_task_callback(
    sid: str,
    rid: str,
) -> Callable[[Any], None]:
    from app.services.stream_events import make_artifact, make_complete, make_start
    from app.services.stream_manager import stream_manager

    def _emit(event: Any) -> None:
        stream_manager.publish_event(sid, event.to_dict())

    started: dict[str, bool] = {}

    def callback(task: Any) -> None:
        agent_role = getattr(task, "agent_role", None) or ""
        agent_name = agent_name_for(getattr(task, "agent", None) or agent_role or "agent")
        output_raw = getattr(task, "output", None)
        output_str = str(output_raw) if output_raw else ""

        if agent_name not in started:
            started[agent_name] = True
            _emit(make_start(agent_name, f"{agent_name} started", rid))

        if hasattr(task, "is_last") and task.is_last:
            _emit(make_artifact(agent_name, {"output_snippet": output_str[:200]}, f"{agent_name} completed", rid))
            _emit(make_complete(agent_name, {"output": output_str}, f"{agent_name} finished", rid))

    return callback


def create_nl_sql_crew(
    schema_metadata: str,
    sid: str = "",
    rid: str = "",
) -> Any:
    from crewai import Agent, Crew, Process, Task

    llm = _llm_config()

    intent_analyst = Agent(
        role="Intent Analyst",
        goal="Analyze natural language requests and identify the user's true intent, key entities, and the relevant database tables and columns needed",
        backstory=(
            "You are an expert at understanding what users really want from their descriptions. "
            "You break down requests into clear, actionable components and identify which database "
            "tables and columns are relevant to answer the query. You always output a structured analysis.\n\n"
            f"Domain Knowledge for JDE → Oracle Fusion migrations:\n{DOMAIN_CONTEXT}"
        ),
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )

    query_author = Agent(
        role="SQL Query Author",
        goal="Generate accurate, efficient, and safe SQL SELECT queries that answer the user's question based on the intent analysis and database schema",
        backstory=(
            "You are a senior SQL developer who writes perfect dialect-specific SQL. "
            "You produce only SELECT statements and follow all safety rules. "
            "Your queries use proper JOINs, WHERE clauses, GROUP BY, and ORDER BY as needed. "
            "You output the SQL inside a markdown code block.\n\n"
            f"Follow these rules:\n{SQL_RULES}\n\n"
            f"Domain Knowledge:\n{DOMAIN_CONTEXT}"
        ),
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )

    critic = Agent(
        role="SQL Safety Critic",
        goal="Validate that the generated SQL query is safe, correct, and follows best practices using the SQLGuard tool",
        backstory=(
            "You are a security-conscious database administrator who reviews every SQL query "
            "for safety issues. You use the SQLGuard tool to verify queries and reject any "
            "that contain dangerous operations."
        ),
        allow_delegation=False,
        verbose=False,
        tools=[_sql_guard_tool()],
        llm=llm,
    )

    analyze_intent = Task(
        description=(
            "Analyze the user's natural language request in the context of the available database schema.\n\n"
            "**User Request:** {user_prompt}\n"
            "**SQL Dialect:** {dialect}\n"
            "**Available Database Schema:**\n"
            "```\n{schema}\n```\n\n"
            "Use your domain knowledge about JDE to Oracle Fusion extraction patterns "
            "to understand supplier, customer, and AR invoice migration requests.\n\n"
            "Identify:\n"
            "1. What the user wants to retrieve or know\n"
            "2. Which tables and columns are relevant (only from the schema above)\n"
            "3. Any filtering conditions, groupings, ordering, or aggregations needed\n"
            "4. Any JOIN conditions between tables\n\n"
            "Be specific about which schema objects are needed."
        ),
        expected_output=(
            "A concise analysis: what the user wants, the specific tables and columns needed, "
            "filter conditions, and query structure"
        ),
        agent=intent_analyst,
    )

    write_query = Task(
        description=(
            "Generate a SQL SELECT query that answers the user's request.\n\n"
            "**User Request:** {user_prompt}\n"
            "**SQL Dialect:** {dialect}\n"
            "**Available Database Schema:**\n"
            "```\n{schema}\n```\n\n"
            "**Important:** The previous agent's output above contains the intent analysis. "
            "Use it to guide your SQL generation.\n\n"
            f"{SQL_RULES}\n\n"
            "Output ONLY the SQL query inside a markdown SQL code block:"
        ),
        expected_output="A single SQL SELECT query inside a ```sql ``` code block",
        agent=query_author,
        context=[analyze_intent],
    )

    validate_query = Task(
        description=(
            "Review the generated SQL query for safety and correctness.\n\n"
            "The generated SQL was produced by the previous agent. "
            "Look at the conversation context above to find it.\n\n"
            "Steps:\n"
            "1. Confirm the SQL is a SELECT statement only\n"
            "2. Check no dangerous functions or system tables\n"
            "3. Use the SQLGuard tool to programmatically validate it\n"
            "4. Report the result clearly\n\n"
            "Use the SQLGuard tool by passing the SQL text followed by |{dialect}."
        ),
        expected_output="VALIDATION PASSED or detailed issue description",
        agent=critic,
        context=[analyze_intent, write_query],
    )

    step_cb = make_step_callback(sid, rid) if sid else None
    task_cb = make_task_callback(sid, rid) if sid else None

    return Crew(
        agents=[intent_analyst, query_author, critic],
        tasks=[analyze_intent, write_query, validate_query],
        process=Process.sequential,
        verbose=False,
        step_callback=step_cb,
        task_callback=task_cb,
    )


def extract_sql(output: str) -> str:
    if not output:
        return ""
    pattern = r"```(?:sql)?\s*\n?([\s\S]*?)```"
    matches = re.findall(pattern, output)
    if matches:
        sql: str = str(matches[-1]).strip()
        try:
            parsed = json.loads(sql)
            if isinstance(parsed, dict) and "query_sql" in parsed:
                return str(parsed["query_sql"])
        except json.JSONDecodeError:
            pass
        return sql
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict) and "query_sql" in parsed:
            return str(parsed["query_sql"])
    except json.JSONDecodeError:
        pass
    return output.strip()


def extract_sql_from_tasks(tasks_output: Any) -> str:
    """Return SQL from the query-author task output in a CrewAI result."""
    task_outputs = list(tasks_output or [])
    for task_output in task_outputs:
        agent_role = getattr(task_output, "agent", None) or getattr(task_output, "agent_role", None)
        if agent_name_for(agent_role) != "query_author":
            continue
        raw = getattr(task_output, "raw", None) or str(task_output)
        sql = extract_sql(raw)
        if sql:
            return sql
    if len(task_outputs) > 1:
        raw = getattr(task_outputs[1], "raw", None) or str(task_outputs[1])
        return extract_sql(raw)
    return ""
