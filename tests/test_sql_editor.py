"""The SQL editor's core (agentcrew/sql_editor.py).

The editor must be exactly as safe as the agent: it runs on the agent's own
ToolContext (build_runtime(...).tools), so these tests run through that
object, on NorthStar, on an uploaded dataset and, when a live server is
configured, on PostgreSQL.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentcrew import runlog
from agentcrew.archs.app_runner import build_runtime
from agentcrew.caps import DailyCap
from agentcrew.config import Settings
from agentcrew.datasets.ingest import ingest
from agentcrew.datasets.source import reset_sources, upload_source
from agentcrew.datasets.store import DatasetStore
from agentcrew.db import dialect as sql_dialect
from agentcrew.schemas import ExecutionResult, FailureKind
from agentcrew.sql_editor import (
    DAILY_LIMIT_MESSAGE,
    EMPTY_MESSAGE,
    FAILED_MESSAGE,
    MAX_SQL_CHARS,
    SESSION_LIMIT_MESSAGE,
    SQL_RUNS_USED,
    TOO_LONG_MESSAGE,
    plain_error,
    run_sql,
    runs_left,
)
from agentcrew.tools import ToolContext

LIVE_URL = os.environ.get("AGENTCREW_TEST_POSTGRES_URL")
WRITES = [
    "DELETE FROM customers",
    "UPDATE customers SET region = 'X'",
    "INSERT INTO customers (customer_id) VALUES (1)",
    "DROP TABLE customers",
    "CREATE TABLE t (a int)",
    "SELECT 1; DELETE FROM customers",
]
SQLITE_ESCAPES = [
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "ATTACH '/etc/passwd' AS p",
    "DETACH DATABASE main",
    "PRAGMA writable_schema = ON",
    "PRAGMA table_info(customers)",
    "PRAGMA query_only = OFF",
    "VACUUM",
]


def run(sql: str, tools: ToolContext, *, state: dict[str, Any] | None = None,
        limit: int = 30, cap: DailyCap | None = None):
    return run_sql(sql, tools=tools, state={} if state is None else state,
                   session_limit=limit, cap=cap)


@pytest.fixture
def northstar(settings: Settings) -> ToolContext:
    """The agent's own NorthStar ToolContext, small row cap for the tests."""
    tools = build_runtime(settings.model_copy(update={"today": date(2026, 1, 1)})).tools
    return tools


# ---------------------------------------------------------------------------
# Safety: the guard is the agent's
# ---------------------------------------------------------------------------
class TestRefusals:
    @pytest.mark.parametrize("sql", WRITES)
    def test_write_sql_is_refused(self, northstar: ToolContext, sql: str) -> None:
        out = run(sql, northstar)
        assert not out.ok
        assert out.result is not None and out.result.failure == FailureKind.BLOCKED
        assert out.message and out.message.startswith("Refused:")

    @pytest.mark.parametrize("sql", SQLITE_ESCAPES)
    def test_attach_and_pragma_are_refused(self, northstar: ToolContext,
                                           sql: str) -> None:
        out = run(sql, northstar)
        assert not out.ok
        assert out.result is not None and out.result.failure == FailureKind.BLOCKED
        assert out.message and out.message.startswith("Refused:")

    def test_the_database_is_unchanged_after_refusals(
            self, northstar: ToolContext) -> None:
        before = run("SELECT COUNT(*) FROM customers", northstar).result
        for sql in WRITES:
            run(sql, northstar)
        after = run("SELECT COUNT(*) FROM customers", northstar).result
        assert before is not None and after is not None
        assert before.rows == after.rows

    def test_refused_runs_still_count(self, northstar: ToolContext) -> None:
        state: dict[str, Any] = {}
        out = run("DROP TABLE customers", northstar, state=state)
        assert out.charged and state[SQL_RUNS_USED] == 1

    @pytest.mark.parametrize("sql", ["DELETE FROM t", "COPY t TO '/tmp/x'",
                                     "SELECT 1; DROP TABLE t"])
    def test_postgres_dialect_refuses_writes(self, northstar: ToolContext,
                                             sql: str) -> None:
        tools = ToolContext(engine=northstar.engine, catalog=northstar.catalog,
                            dialect=sql_dialect.POSTGRES, metrics=False)
        out = run(sql, tools)
        assert out.result is not None and out.result.failure == FailureKind.BLOCKED


# ---------------------------------------------------------------------------
# Same caps as the agent
# ---------------------------------------------------------------------------
class TestSameContextAsTheAgent:
    def test_editor_and_agent_share_one_tool_context(self, settings: Settings) -> None:
        s = settings.model_copy(update={"today": date(2026, 1, 1)})
        assert build_runtime(s).tools is build_runtime(s).tools

    def test_row_cap_is_applied(self, northstar: ToolContext) -> None:
        tools = ToolContext(engine=northstar.engine, catalog=northstar.catalog,
                            max_rows=7)
        out = run("SELECT * FROM orders", tools)
        assert out.ok and out.result is not None
        # The guard injects the cap as LIMIT, exactly as for the agent.
        assert out.result.row_count == 7
        assert out.result.sql.upper().endswith("LIMIT 7")

    def test_row_cap_holds_over_a_larger_limit(self, northstar: ToolContext) -> None:
        tools = ToolContext(engine=northstar.engine, catalog=northstar.catalog,
                            max_rows=5)
        out = run("SELECT * FROM orders LIMIT 100000", tools)
        assert out.ok and out.result is not None
        assert out.result.row_count == 5

    def test_agent_row_cap_by_default(self, northstar: ToolContext,
                                      settings: Settings) -> None:
        out = run("SELECT * FROM orders", northstar)
        assert out.result is not None
        assert out.result.row_count == settings.max_result_rows


# ---------------------------------------------------------------------------
# Uploaded dataset
# ---------------------------------------------------------------------------
class TestUploadedDataset:
    @pytest.fixture
    def upload_tools(self, tmp_path: Path):
        settings = Settings(upload_dir=tmp_path / "uploads", max_result_rows=3)
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        rows = "\n".join(f"{i},name {i},{i * 10}" for i in range(1, 11))
        dataset = ingest(store, owner="o", filename="people.csv",
                         data=f"id,name,score\n{rows}\n".encode(), settings=settings)
        source = upload_source(dataset, settings)
        yield build_runtime(settings.model_copy(update={"today": date(2026, 1, 1)}),
                            source=source).tools
        reset_sources()

    def test_select_runs_on_the_upload(self, upload_tools: ToolContext) -> None:
        out = run("SELECT name, score FROM people WHERE id = 2", upload_tools)
        assert out.ok and out.result is not None
        assert out.result.rows == [["name 2", 20]]

    def test_row_cap_on_the_upload(self, upload_tools: ToolContext) -> None:
        out = run("SELECT * FROM people", upload_tools)
        assert out.result is not None
        assert out.result.row_count == 3

    @pytest.mark.parametrize("sql", ["DELETE FROM people", *SQLITE_ESCAPES[:4]])
    def test_writes_and_escapes_refused_on_the_upload(
            self, upload_tools: ToolContext, sql: str) -> None:
        out = run(sql, upload_tools)
        assert out.result is not None and out.result.failure == FailureKind.BLOCKED
        still = run("SELECT COUNT(*) FROM people", upload_tools).result
        assert still is not None and still.rows == [[10]]

    def test_missing_table_is_a_plain_message(self, upload_tools: ToolContext) -> None:
        out = run("SELECT * FROM nope", upload_tools)
        assert not out.ok and out.message
        assert "nope" in out.message
        assert "\n" not in out.message and "Error)" not in out.message


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
class TestLimits:
    def test_session_limit(self, northstar: ToolContext) -> None:
        state: dict[str, Any] = {}
        for _ in range(2):
            assert run("SELECT 1", northstar, state=state, limit=2).ok
        out = run("SELECT 1", northstar, state=state, limit=2)
        assert out.message == SESSION_LIMIT_MESSAGE and not out.charged
        assert out.result is None and state[SQL_RUNS_USED] == 2

    def test_no_session_limit_when_zero(self, northstar: ToolContext) -> None:
        state: dict[str, Any] = {SQL_RUNS_USED: 10_000}
        assert runs_left(state, 0) is None
        assert run("SELECT 1", northstar, state=state, limit=0).ok

    def test_daily_sql_cap_is_charged_and_enforced(
            self, northstar: ToolContext) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=0, max_sql_runs=2)
        assert run("SELECT 1", northstar, cap=cap).ok
        assert run("DROP TABLE customers", northstar, cap=cap).charged
        assert cap.snapshot().sql_runs == 2
        out = run("SELECT 1", northstar, cap=cap)
        assert out.message == DAILY_LIMIT_MESSAGE and not out.charged
        assert out.status == "daily_cap"
        assert cap.snapshot().sql_runs == 2

    def test_editor_runs_leave_the_agents_daily_runs_alone(
            self, northstar: ToolContext) -> None:
        cap = DailyCap(max_runs=1, max_llm_calls=0, max_sql_runs=0)
        for _ in range(3):
            assert run("SELECT 1", northstar, cap=cap).ok
        assert cap.snapshot().runs == 0 and cap.allows_run()

    def test_agent_runs_do_not_block_the_editor(self, northstar: ToolContext) -> None:
        cap = DailyCap(max_runs=1, max_llm_calls=0, max_sql_runs=5)
        cap.charge_run()
        assert not cap.allows_run()
        assert run("SELECT 1", northstar, cap=cap).ok

    def test_no_model_call_is_charged(self, northstar: ToolContext) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=1)
        run("SELECT 1", northstar, cap=cap)
        assert cap.snapshot().llm_calls == 0

    @pytest.mark.parametrize("sql", ["", "   \n  "])
    def test_empty_input_is_not_charged(self, northstar: ToolContext, sql: str) -> None:
        state: dict[str, Any] = {}
        out = run(sql, northstar, state=state)
        assert out.message == EMPTY_MESSAGE and not out.charged and not state

    def test_too_long_input_is_not_charged(self, northstar: ToolContext) -> None:
        state: dict[str, Any] = {}
        out = run("SELECT 1 " + " " * MAX_SQL_CHARS + "-- x", northstar, state=state)
        assert out.message == TOO_LONG_MESSAGE and not state


# ---------------------------------------------------------------------------
# Plain errors
# ---------------------------------------------------------------------------
class TestPlainErrors:
    def test_syntax_error_is_one_short_line(self, northstar: ToolContext) -> None:
        out = run("SELEC region FROM", northstar)
        assert not out.ok and out.message
        assert "\n" not in out.message and len(out.message) <= 210
        assert "Traceback" not in out.message

    def test_driver_noise_is_removed(self) -> None:
        result = ExecutionResult(
            ok=False, sql="x", failure=FailureKind.MISSING_OBJECT,
            error_message="(sqlite3.OperationalError) no such column: foo\n"
                          "(Background on this error at: https://sqlalche.me/e/20/e3q8)")
        assert plain_error(result, 15) == "no such column: foo"

    def test_timeout_names_the_limit(self) -> None:
        result = ExecutionResult(ok=False, sql="x", failure=FailureKind.TIMEOUT,
                                 error_message="interrupted")
        assert plain_error(result, 15.0) == (
            "The query took longer than 15 seconds and was stopped.")

    def test_long_message_is_cut(self) -> None:
        result = ExecutionResult(ok=False, sql="x", failure=FailureKind.OTHER,
                                 error_message="x" * 1000)
        assert len(plain_error(result, 15)) == 200

    def test_secrets_are_scrubbed(self) -> None:
        result = ExecutionResult(ok=False, sql="x", failure=FailureKind.OTHER,
                                 error_message="cannot reach postgresql://u:hunter2@h/db")
        shown = plain_error(result, 15, scrub=lambda t: t.replace("hunter2", "***"))
        assert "hunter2" not in shown

    def test_an_unexpected_exception_is_a_plain_message(
            self, northstar: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: Any) -> None:
            raise RuntimeError("internal detail\nTraceback (most recent call last)")

        monkeypatch.setattr("agentcrew.sql_editor.execute_readonly_sql", boom)
        out = run("SELECT 1", northstar)
        assert out.message == FAILED_MESSAGE and out.charged


# ---------------------------------------------------------------------------
# Live PostgreSQL (skipped without a server, like tests/test_postgres.py)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not LIVE_URL, reason="needs AGENTCREW_TEST_POSTGRES_URL")
class TestLivePostgres:
    def test_editor_on_self_hosted_postgres(self, tmp_path: Path) -> None:
        from sqlalchemy import create_engine, text

        from agentcrew.datasets.source import postgres_source

        admin = create_engine(LIVE_URL.replace("postgresql://",
                                               "postgresql+psycopg://", 1))
        with admin.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS ac_editor"))
            conn.execute(text("CREATE TABLE ac_editor (a int)"))
            conn.execute(text("INSERT INTO ac_editor SELECT generate_series(1, 20)"))
        try:
            settings = Settings(database_url=LIVE_URL, max_result_rows=4,
                                upload_dir=tmp_path)
            tools = build_runtime(settings, source=postgres_source(settings)).tools
            out = run("SELECT a FROM ac_editor ORDER BY a", tools)
            assert out.ok and out.result is not None
            assert out.result.row_count == 4
            refused = run("DELETE FROM ac_editor", tools)
            assert refused.result is not None
            assert refused.result.failure == FailureKind.BLOCKED
        finally:
            with admin.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS ac_editor"))
            admin.dispose()
            reset_sources()


# ---------------------------------------------------------------------------
# The run log: one line per editor run
# ---------------------------------------------------------------------------
class TestRunLog:
    @pytest.fixture
    def lines(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []

        def capture(record: runlog.RunRecord, *_: Any) -> str:
            seen.append(runlog.to_dict(record))
            return ""

        monkeypatch.setattr("agentcrew.sql_editor.runlog.emit", capture)
        return seen

    def test_a_good_run(self, northstar: ToolContext, lines: list) -> None:
        run("SELECT segment FROM customers GROUP BY segment", northstar)
        (line,) = lines
        assert line["surface"] == "sql_editor" and line["status"] == "done"
        assert line["rows"] > 0 and line["error"] is None
        assert line["llm_calls"] == 0 and line["seconds"] >= 0
        assert "final_sql" not in line, "SQL text only with trace content on"

    def test_sql_text_only_with_trace_content(self, northstar: ToolContext,
                                              lines: list) -> None:
        run_sql("SELECT 1", tools=northstar, state={}, session_limit=0, cap=None,
                trace_content=True)
        assert lines[0]["final_sql"] == "SELECT 1"

    @pytest.mark.parametrize(("sql", "status", "error"), [
        ("DELETE FROM customers", "refused", "blocked"),
        ("SELECT nope FROM customers", "failed", "missing_object"),
        ("", "invalid", None),
    ])
    def test_failures_record_status_and_kind(self, northstar: ToolContext,
                                             lines: list, sql: str, status: str,
                                             error: str | None) -> None:
        run(sql, northstar)
        (line,) = lines
        assert (line["status"], line["error"]) == (status, error)
        assert "rows" not in line

    def test_limits_are_logged(self, northstar: ToolContext, lines: list) -> None:
        state: dict[str, Any] = {SQL_RUNS_USED: 1}
        run("SELECT 1", northstar, state=state, limit=1)
        assert lines[-1]["status"] == "session_limit"

    def test_an_exception_logs_its_type_name_only(
            self, northstar: ToolContext, lines: list,
            monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: Any) -> None:
            raise RuntimeError("secret detail")

        monkeypatch.setattr("agentcrew.sql_editor.execute_readonly_sql", boom)
        run("SELECT 1", northstar)
        assert lines[-1]["error"] == "RuntimeError"
        assert "secret detail" not in str(lines[-1])

    def test_the_line_is_json_on_the_run_logger(
            self, northstar: ToolContext, caplog: pytest.LogCaptureFixture) -> None:
        import json

        with caplog.at_level("INFO", logger=runlog.LOGGER_NAME):
            runlog.log.addHandler(caplog.handler)
            try:
                run("SELECT 1", northstar)
            finally:
                runlog.log.removeHandler(caplog.handler)
        payload = json.loads(caplog.records[-1].getMessage())
        assert payload["surface"] == "sql_editor" and payload["rows"] == 1
