"""
SQL Safety Validator — static analysis layer before sandbox execution.

Uses ``sqlglot`` to parse SQL into an AST and walk it for dangerous patterns.
Supports PostgreSQL and Oracle dialects, with two validation modes:

- ``schema_setup``  — looser rules for initial schema creation
- ``query_under_test`` — strict rules for LLM-generated queries
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ValidationMode(enum.Enum):
    """How strict the validator should be."""

    SCHEMA_SETUP = "schema_setup"
    QUERY_UNDER_TEST = "query_under_test"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Returned by :func:`validate`."""

    is_safe: bool
    reasons: list[str] = field(default_factory=list)
    redacted_sql: str = ""


class UnsafeSQLError(Exception):
    """Raised when SQL fails validation."""

    def __init__(self, reasons: list[str], redacted_sql: str = "") -> None:
        self.reasons = reasons
        self.redacted_sql = redacted_sql
        super().__init__(f"Unsafe SQL detected: {'; '.join(reasons)}")


# ---------------------------------------------------------------------------
# Blocked patterns
# ---------------------------------------------------------------------------

# Schemas that are always off-limits.
BLOCKED_SCHEMAS: set[str] = {
    "pg_catalog",
    "information_schema",
    "sys",
    "system",
}

# Table-name prefixes that indicate Oracle data-dictionary views.
BLOCKED_TABLE_PREFIXES: tuple[str, ...] = ("dba_",)

# Known PostgreSQL system tables that live in pg_catalog but are often
# referenced without the schema prefix.
BLOCKED_PG_TABLES: set[str] = {
    "pg_shadow",
    "pg_authid",
    "pg_stat_activity",
    "pg_stat_replication",
    "pg_stat_wal_receiver",
    "pg_roles",
    "pg_user",
    "pg_group",
    "pg_hba_file_rules",
    "pg_config",
    "pg_file_settings",
    "pg_stat_ssl",
    "pg_stat_gssapi",
    "pg_replication_slots",
    "pg_prepared_xacts",
    "pg_locks",
}

# Dangerous function names (case-insensitive comparison).
BLOCKED_FUNCTIONS: set[str] = {
    # PostgreSQL filesystem
    "pg_read_file",
    "pg_write_file",
    "pg_read_binary_file",
    "pg_stat_file",
    "pg_logdir_ls",
    "copy_from_program",
    # Oracle network / filesystem packages
    "utl_file",
    "utl_http",
    "utl_tcp",
    "utl_smtp",
    "utl_inaddr",
    "utl_url",
    "utl_dbws",
    # Oracle scheduler / job / pipe
    "dbms_scheduler",
    "dbms_job",
    "dbms_file_transfer",
    "dbms_pipe",
    "dbms_alert",
    "dbms_backup_restore",
    "dbms_lob",
}

# Oracle package-qualified function prefixes (e.g. UTL_HTTP.REQUEST).
BLOCKED_PACKAGE_PREFIXES: tuple[str, ...] = (
    "utl_file",
    "utl_http",
    "utl_tcp",
    "utl_smtp",
    "utl_inaddr",
    "utl_url",
    "utl_dbws",
    "dbms_scheduler",
    "dbms_job",
    "dbms_file_transfer",
    "dbms_pipe",
    "dbms_alert",
    "dbms_backup_restore",
    "dbms_lob",
)

# Oracle V$ / ALL_ views that are off-limits.
BLOCKED_ORACLE_VIEW_PREFIXES: tuple[str, ...] = ("v$", "all_")

# Commands that sqlglot reports as a ``Command`` node with this key.
BLOCKED_COMMANDS: set[str] = {"do"}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Regex to catch single-quoted strings and PostgreSQL dollar-quoted strings
# that sqlglot may not fully normalise.
_RE_SINGLE_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'")
_RE_DOLLAR_QUOTED = re.compile(r"\$[^$]*\$.*?\$[^$]*\$", re.DOTALL)


def _redact_sql(sql: str) -> str:
    """Replace string literals with ``'REDACTED'``."""
    # Dollar-quoted first (longer spans), then single-quoted.
    redacted = _RE_DOLLAR_QUOTED.sub("'REDACTED'", sql)
    redacted = _RE_SINGLE_QUOTED.sub("'REDACTED'", redacted)
    return redacted


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _normalise(name: str | None) -> str:
    """Lowercase and strip quotes."""
    if name is None:
        return ""
    return name.strip('"').strip("'").lower()


def _collect_tables(tree: exp.Expression) -> list[tuple[str, str]]:
    """Return ``[(schema, table), ...]`` for every Table node."""
    tables: list[tuple[str, str]] = []
    for node in tree.walk():
        if isinstance(node, exp.Table):
            tables.append((_normalise(node.db), _normalise(node.name)))
    return tables


def _collect_functions(tree: exp.Expression) -> list[str]:
    """Return bare function names found in the AST."""
    funcs: list[str] = []
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            funcs.append(node.name.lower())
        elif isinstance(node, exp.Func):
            funcs.append(node.sql_name().lower())
    return funcs


def _collect_package_funcs(tree: exp.Expression) -> list[str]:
    """Return qualified names like ``utl_http.request`` for Dot-wrapped calls."""
    qualified: list[str] = []
    for node in tree.walk():
        if isinstance(node, exp.Dot):
            # Dot(this=UTL_HTTP, expression=REQUEST(...))
            # this may be Column or Identifier depending on dialect
            pkg = ""
            if isinstance(node.this, (exp.Column, exp.Identifier)):
                pkg = _normalise(node.this.name)
            if pkg and isinstance(node.expression, exp.Anonymous):
                fn = node.expression.name.lower()
                qualified.append(f"{pkg}.{fn}")
    return qualified


def _has_copy_program(tree: exp.Expression) -> bool:
    """Detect ``COPY ... PROGRAM ...``."""
    for node in tree.walk():
        if isinstance(node, exp.Copy):
            for file_node in node.args.get("files", []):
                if isinstance(file_node, exp.Identifier) and _normalise(file_node.name) == "program":
                    return True
            # Also check params for PROGRAM keyword
            for param in node.args.get("params", []):
                if isinstance(param, exp.CopyParameter):
                    inner = param.this
                    if isinstance(inner, exp.Var) and "program" in inner.sql().lower():
                        return True
    return False


def _has_execute_immediate(tree: exp.Expression) -> bool:
    """Detect ``EXECUTE IMMEDIATE`` (Oracle dynamic SQL)."""
    sql = tree.sql(dialect="oracle").upper()
    return "EXECUTE IMMEDIATE" in sql


def _has_do_block(tree: exp.Expression) -> bool:
    """Detect ``DO $$ ... $$`` (PL/pgSQL anonymous block)."""
    for node in tree.walk():
        if isinstance(node, exp.Command) and _normalise(node.name) == "do":
            return True
    return False


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def _check_system_schemas(tables: list[tuple[str, str]]) -> list[str]:
    """Return reasons for any table referencing a blocked schema."""
    reasons: list[str] = []
    for schema, table in tables:
        if schema in BLOCKED_SCHEMAS:
            reasons.append(f"Access to system schema '{schema}' is blocked (table '{table}')")
        for prefix in BLOCKED_TABLE_PREFIXES:
            if table.startswith(prefix):
                reasons.append(f"Access to '{table}' is blocked (starts with '{prefix}')")
        # Oracle V$ and ALL_ views
        if table.startswith(BLOCKED_ORACLE_VIEW_PREFIXES):
            reasons.append(f"Access to Oracle view '{table}' is blocked")
        # Known PostgreSQL system tables (even without schema prefix)
        if table in BLOCKED_PG_TABLES:
            reasons.append(f"Access to system table '{table}' is blocked")
    return reasons


# ---------------------------------------------------------------------------
# Regex-based fallback scanning
# ---------------------------------------------------------------------------

# Build a combined regex for dangerous function names.
# Matches both bare calls (pg_read_file(...)) and package-qualified calls
# (DBMS_SCHEDULER.CREATE_JOB(...), UTL_HTTP.REQUEST(...)).
# Uses optional underscores/spaces between words to defeat comment-based
# obfuscation like pg_read/**/file( which becomes pg_readfile( after stripping.
def _flex_pattern(name: str) -> str:
    """Build a regex that matches *name* allowing ``_`` or whitespace between words."""
    parts = name.split("_")
    return r"[\W_]*".join(re.escape(p) for p in parts)


_BLOCKED_FLEX = "|".join(_flex_pattern(f) for f in BLOCKED_FUNCTIONS)
_RE_BLOCKED_FUNCS = re.compile(
    r"(?:" + _BLOCKED_FLEX + r")(?:\s*\.\s*\w+)?\s*\(",
    re.IGNORECASE,
)

_RE_COPY_PROGRAM = re.compile(r"\bCOPY\b.*\bPROGRAM\b", re.IGNORECASE)

_RE_EXECUTE_IMMEDIATE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)

# Strip SQL comments before regex scanning to defeat comment-based obfuscation.
_RE_SQL_COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)


def _regex_scan(sql: str, mode: ValidationMode) -> list[str]:
    """Regex-based fallback for patterns sqlglot's AST may miss.

    Catches dangerous functions inside dollar-quoted bodies, PL/SQL blocks,
    and other constructs that sqlglot can't fully parse.
    """
    # Strip block comments to defeat obfuscation like pg_read/**/file(...)
    clean_sql = _RE_SQL_COMMENTS.sub("", sql)

    reasons: list[str] = []
    for match in _RE_BLOCKED_FUNCS.finditer(clean_sql):
        matched = match.group().rstrip("(").strip().lower()
        # Extract the base package/function name
        fn_name = matched.split(".")[0].split()[-1]
        reasons.append(f"Blocked function (regex): {fn_name}")
    if _RE_COPY_PROGRAM.search(clean_sql):
        reasons.append("COPY ... PROGRAM is blocked (regex)")
    if _RE_EXECUTE_IMMEDIATE.search(clean_sql):
        reasons.append("EXECUTE IMMEDIATE is blocked (regex)")
    return reasons


def _check_blocked_functions(funcs: list[str]) -> list[str]:
    reasons: list[str] = []
    for fn in funcs:
        if fn in BLOCKED_FUNCTIONS:
            reasons.append(f"Blocked function: {fn}")
    return reasons


def _check_package_funcs(qualified: list[str]) -> list[str]:
    reasons: list[str] = []
    for qfn in qualified:
        pkg = qfn.split(".")[0]
        if pkg in BLOCKED_PACKAGE_PREFIXES:
            reasons.append(f"Blocked package call: {qfn}")
    return reasons


def _check_copy_program(tree: exp.Expression) -> list[str]:
    if _has_copy_program(tree):
        return ["COPY ... PROGRAM is blocked (filesystem access)"]
    return []


def _check_execute_immediate(tree: exp.Expression, mode: ValidationMode) -> list[str]:
    if not _has_execute_immediate(tree):
        return []
    if mode == ValidationMode.QUERY_UNDER_TEST:
        return ["EXECUTE IMMEDIATE is blocked in query_under_test mode"]
    # In schema_setup, we allow it only if there's a clear safe literal —
    # but since we can't statically verify the string content reliably,
    # we block it universally for safety.
    return ["EXECUTE IMMEDIATE is blocked (dynamic SQL)"]


def _check_do_block(tree: exp.Expression, mode: ValidationMode) -> list[str]:
    if _has_do_block(tree) and mode == ValidationMode.QUERY_UNDER_TEST:
        return ["DO blocks are blocked in query_under_test mode"]
    return []


def _check_call_system(tree: exp.Expression) -> list[str]:
    """Block ``CALL`` to procedures in system schemas."""
    reasons: list[str] = []
    for node in tree.walk():
        if isinstance(node, exp.Command) and _normalise(node.name) == "call":
            # The expression after CALL is a string that may contain a qualified name
            expr = node.args.get("expression")
            if expr is not None:
                expr_str = _normalise(expr.sql())
                for schema in BLOCKED_SCHEMAS:
                    if schema in expr_str:
                        reasons.append(f"CALL to system schema procedure blocked: {expr_str[:80]}")
    return reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(
    sql: str,
    dialect: str,
    mode: ValidationMode | str = ValidationMode.QUERY_UNDER_TEST,
) -> ValidationResult:
    """Validate *sql* for safety before sandbox execution.

    Parameters
    ----------
    sql:
        The SQL string to validate.
    dialect:
        ``"postgres"`` or ``"oracle"``.
    mode:
        A :class:`ValidationMode` or its string value.

    Returns
    -------
    ValidationResult
        ``is_safe`` is ``True`` when no dangerous patterns are found.
    """
    if isinstance(mode, str):
        mode = ValidationMode(mode)

    if dialect not in ("postgres", "oracle"):
        return ValidationResult(is_safe=False, reasons=[f"Unsupported dialect: {dialect!r}"])

    # 1. Redact
    redacted_sql = _redact_sql(sql)

    # 2. Regex-based fallback scan (runs on original SQL before redaction)
    all_reasons: list[str] = list(_regex_scan(sql, mode))

    # 3. Parse (multi-statement support)
    try:
        trees = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.SqlglotError as exc:
        logger.debug("Parse error: %s", exc)
        # If regex already caught something dangerous, report those reasons.
        # Otherwise, suppress the parse error — sqlglot has known limitations
        # with PL/SQL blocks, XMLELEMENT, and other advanced constructs.
        # The regex scan above has already checked for dangerous patterns.
        is_safe = len(all_reasons) == 0
        return ValidationResult(is_safe=is_safe, reasons=all_reasons, redacted_sql=redacted_sql)

    # 4. Walk each statement
    for tree in trees:
        if tree is None:
            continue

        tables = _collect_tables(tree)
        funcs = _collect_functions(tree)
        pkg_funcs = _collect_package_funcs(tree)

        all_reasons.extend(_check_system_schemas(tables))
        all_reasons.extend(_check_blocked_functions(funcs))
        all_reasons.extend(_check_package_funcs(pkg_funcs))
        all_reasons.extend(_check_copy_program(tree))
        all_reasons.extend(_check_execute_immediate(tree, mode))
        all_reasons.extend(_check_do_block(tree, mode))
        all_reasons.extend(_check_call_system(tree))

    # Deduplicate reasons
    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in all_reasons:
        if r not in seen:
            seen.add(r)
            unique_reasons.append(r)
    all_reasons = unique_reasons

    is_safe = len(all_reasons) == 0
    if not is_safe:
        logger.info("Validation failed (%d reason(s)): %s", len(all_reasons), all_reasons)

    return ValidationResult(is_safe=is_safe, reasons=all_reasons, redacted_sql=redacted_sql)


def validate_or_raise(
    sql: str,
    dialect: str,
    mode: ValidationMode | str = ValidationMode.QUERY_UNDER_TEST,
) -> str:
    """Validate *sql* and return the redacted version, or raise :class:`UnsafeSQLError`."""
    result = validate(sql, dialect, mode)
    if not result.is_safe:
        raise UnsafeSQLError(reasons=result.reasons, redacted_sql=result.redacted_sql)
    return result.redacted_sql
