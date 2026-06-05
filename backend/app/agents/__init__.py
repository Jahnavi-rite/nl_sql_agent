from app.agents.crew_setup import create_nl_sql_crew, extract_sql, extract_sql_from_tasks
from app.agents.single_shot import AgentError, AgentResponse, JSONParseError, LLMError, generate

# Debate agents are imported lazily since pyautogen may not be available on Python 3.13+
try:
    from app.agents.debate.debate_runner import run_debate  # noqa: F401
    from app.agents.debate.models import DebateResult  # noqa: F401
    DEBATE_AVAILABLE = True
except (ImportError, Exception):
    DEBATE_AVAILABLE = False

__all__ = [
    "AgentError",
    "AgentResponse",
    "create_nl_sql_crew",
    "extract_sql",
    "extract_sql_from_tasks",
    "JSONParseError",
    "LLMError",
    "generate",
]
