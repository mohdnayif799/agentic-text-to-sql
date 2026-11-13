"""Self-hosted PostgreSQL: the read-only engine, the guard in the postgres
dialect, and the prompt adapter.

The first classes run offline. TestLiveServer needs a real server and runs
only when AGENTCREW_TEST_POSTGRES_URL is set (CI has none); it was run
against postgres:17 in Docker. That URL must be a role that CAN write, so
the test proves the app's own layers refuse the write, not just the role:

    docker run -d --name agentcrew-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17
    AGENTCREW_TEST_POSTGRES_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_postgres.py
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from agentcrew.archs.app_runner import DialectClient
from agentcrew.db import dialect as sql_dialect
from agentcrew.db.engine import UnsupportedDatabaseError, postgres_read_only_engine
from agentcrew.db.safety import UnsafeSQLError, guard_sql
from agentcrew.llm import FakeClient

#: Read directly, not through Settings: conftest strips AGENTCREW_* variables,
#: so it is read once at import time, before any test runs.
LIVE_URL = os.environ.get("AGENTCREW_TEST_POSTGRES_URL")


class TestEngineUrl:
    @pytest.mark.parametrize("url", [
        "mysql://u:p@h/db", "mysql+pymysql://u:p@h/db", "sqlite:///x.db",
        "mssql://u:p@h/db",
    ])
    def test_only_postgres_is_accepted(self, url: str) -> None:
        with pytest.raises(UnsupportedDatabaseError, match="PostgreSQL"):
            postgres_read_only_engine(url)

    def test_garbage_url(self) -> None:
        with pytest.raises(UnsupportedDatabaseError):
            postgres_read_only_engine("not a url at all")

    @pytest.mark.parametrize("scheme", ["postgresql", "postgres",
                                        "postgresql+psycopg"])
    def test_postgres_schemes_use_psycopg(self, scheme: str) -> None:
        engine = postgres_read_only_engine(f"{scheme}://u:p@localhost:1/db")
        assert engine.url.drivername == "postgresql+psycopg"
        engine.dispose()


class TestGuardInPostgresDialect:
    @pytest.mark.parametrize("sql", [
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SELECT pg_sleep(30)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
        "SELECT nextval('orders_id_seq')",
        "WITH x AS (SELECT pg_terminate_backend(123)) SELECT * FROM x",
        "SELECT query_to_xml('delete from t', true, true, '')",
    ])
    def test_denied_functions(self, sql: str) -> None:
        with sql_dialect.using("postgres"), pytest.raises(UnsafeSQLError,
                                                          match="not permitted"):
            guard_sql(sql, max_rows=10)

    @pytest.mark.parametrize("sql", [
        "DELETE FROM t", "UPDATE t SET a = 1", "INSERT INTO t VALUES (1)",
        "DROP TABLE t", "TRUNCATE t", "SELECT 1; SELECT 2",
        "COPY t TO PROGRAM 'id'",
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    ])
    def test_writes_are_refused(self, sql: str) -> None:
        with sql_dialect.using("postgres"), pytest.raises(UnsafeSQLError):
            guard_sql(sql, max_rows=10)

    def test_ordinary_reads_pass_with_a_limit(self) -> None:
        with sql_dialect.using("postgres"):
            out = guard_sql("SELECT date_trunc('month', created_at) AS m, "
                            "COUNT(*) FROM orders GROUP BY 1", max_rows=10)
        assert out.limit_injected and "LIMIT 10" in out.sql

    def test_sqlite_is_unaffected_by_the_postgres_list(self) -> None:
        # The deny list is PostgreSQL's; SQLite has no such functions, and its
        # guard behaves exactly as before.
        assert guard_sql("SELECT nextval FROM t", max_rows=5).limit_injected

    def test_unknown_dialect(self) -> None:
        with pytest.raises(ValueError, match="unsupported"), sql_dialect.using("mysql"):
            pass


class TestDialectClient:
    def test_rewrites_the_dialect_name_both_ways_in(self) -> None:
        inner = FakeClient(["ok"])
        client = DialectClient(inner, "PostgreSQL")
        client.complete(system="Write ONE SQLite query.", user="## Database schema "
                        "(SQLite)\nq")
        system, user = inner.prompts[0]
        assert system == "Write ONE PostgreSQL query."
        assert "(PostgreSQL)" in user and "SQLite" not in user


@pytest.mark.skipif(not LIVE_URL, reason="needs AGENTCREW_TEST_POSTGRES_URL")
class TestLiveServer:
    @pytest.fixture(scope="class")
    def seeded(self):
        from sqlalchemy import create_engine

        admin = create_engine(LIVE_URL.replace("postgresql://",
                                               "postgresql+psycopg://", 1))
        with admin.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS ac_marks, ac_students"))
            conn.execute(text("CREATE TABLE ac_students (student_id int PRIMARY KEY, "
                              "name text, joined date)"))
            conn.execute(text("CREATE TABLE ac_marks (student_id int, score int)"))
            conn.execute(text("INSERT INTO ac_students VALUES (1,'A','2025-01-02'),"
                              "(2,'B','2025-03-04')"))
            conn.execute(text("INSERT INTO ac_marks VALUES (1,80),(2,70),(1,90)"))
        yield admin
        with admin.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS ac_marks, ac_students"))
        admin.dispose()

    def test_session_is_read_only_even_for_a_writing_role(self, seeded) -> None:
        engine = postgres_read_only_engine(LIVE_URL, timeout=5)
        with engine.connect() as conn:
            assert conn.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
            with pytest.raises(Exception, match="read-only transaction"):
                conn.execute(text("INSERT INTO ac_marks VALUES (3, 1)"))
        engine.dispose()

    def test_statement_timeout_applies(self, seeded) -> None:
        engine = postgres_read_only_engine(LIVE_URL, timeout=1)
        with engine.connect() as conn:
            with pytest.raises(Exception, match="statement timeout|canceling"):
                conn.execute(text("SELECT pg_sleep(3)"))
        engine.dispose()

    def test_tools_read_and_refuse(self, seeded) -> None:
        from agentcrew.tools import ToolContext, execute_readonly_sql

        engine = postgres_read_only_engine(LIVE_URL, timeout=5)
        tools = ToolContext.create(engine, dialect="postgres", metrics=False)
        result = execute_readonly_sql(
            tools, "SELECT s.name, AVG(m.score) AS avg FROM ac_marks m "
                   "JOIN ac_students s USING (student_id) GROUP BY 1 ORDER BY 1")
        assert result.ok and [r[0] for r in result.rows] == ["A", "B"]
        assert not execute_readonly_sql(tools, "DELETE FROM ac_marks").ok
        assert not execute_readonly_sql(
            tools, "SELECT set_config('default_transaction_read_only','off',false)").ok
        # A write the guard did not catch still hits a read-only transaction.
        with engine.connect() as conn:
            with pytest.raises(Exception, match="read-only"):
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql("CREATE TABLE ac_x (a int)")
        engine.dispose()

    def test_source_profile_joins_and_dates(self, seeded) -> None:
        from datetime import date

        from agentcrew.config import Settings
        from agentcrew.datasets.source import postgres_source, reset_sources

        reset_sources()
        settings = Settings(database_url=LIVE_URL)
        source = postgres_source(settings)
        joins = {j.sql() for j in source.joins}
        assert "ac_marks.student_id = ac_students.student_id" in joins
        tools = source.tool_context(settings)
        ctx = source.context_builder(tools, date(2026, 1, 1), settings).build(
            "average score").render()
        assert "ac_students.joined (DATE): 2025-01-02 .. 2025-03-04" in ctx
        reset_sources()
