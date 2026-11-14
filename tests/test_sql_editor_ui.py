"""The SQL editor in the app: a collapsed expander under each answer.

Rendered through AppTest with the agent replaced by FakeAgent, whose runtime
carries a real NorthStar ToolContext, so every Run goes through the real
guard and database. FakeAgent fails any attempt to build a model client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ui_harness import FakeAgent, ask, start_app

from agentcrew import caps
from agentcrew.db.engine import read_only_engine
from agentcrew.tools import ToolContext

LABEL = "Edit and run the SQL"


@pytest.fixture
def agent(fake_agent: FakeAgent, demo_env: None, db_path: Path) -> FakeAgent:
    """The demo's configuration (server key), with a real NorthStar context."""
    fake_agent.tools = ToolContext.create(read_only_engine(db_path), max_rows=25)
    return fake_agent


def editor(at: Any) -> Any:
    return next(e for e in at.expander if e.label == LABEL)


def run_in_editor(at: Any, sql: str) -> Any:
    at.text_area(key="sql_text_0").set_value(sql)
    at.button(key="sql_run_0").click()
    return at.run()


def notes(at: Any) -> str:
    return " ".join(m.value for m in at.markdown)


class TestSqlEditorUi:
    def test_collapsed_and_prefilled_with_the_final_sql(self, agent: FakeAgent) -> None:
        at = ask(start_app(), "q")
        box = editor(at)
        assert box.proto.expanded is False
        assert at.text_area(key="sql_text_0").value == "SELECT region, n FROM t"

    def test_runs_without_a_model_call_and_counts(self, agent: FakeAgent) -> None:
        at = ask(start_app(), "q")
        agent_runs = caps._cap.snapshot().runs if caps._cap else 0
        at = run_in_editor(at, "SELECT segment FROM customers GROUP BY segment")
        assert not at.exception, at.exception
        assert not at.error, [e.value for e in at.error]
        assert len(agent.settings_seen) == 1, "the editor must not run the agent"
        assert at.session_state["sql_runs_used"] == 1
        assert caps._cap is not None
        snap = caps._cap.snapshot()
        assert snap.sql_runs == 1 and snap.max_sql_runs == 1000
        assert snap.runs == agent_runs, "editor runs leave the agent's runs alone"
        assert snap.llm_calls == 0
        assert at.dataframe, "the result table is shown"

    def test_row_cap_is_shown(self, agent: FakeAgent) -> None:
        at = run_in_editor(ask(start_app(), "q"), "SELECT * FROM orders")
        # The answer's own table comes first; the editor's result is last.
        assert at.dataframe[-1].value.shape[0] == 25
        assert "capped at 25 rows" in notes(at)

    @pytest.mark.parametrize("sql", ["DELETE FROM customers",
                                     "ATTACH DATABASE 'x.db' AS x",
                                     "PRAGMA writable_schema = ON"])
    def test_refused_statements_are_a_short_error(self, agent: FakeAgent,
                                                  sql: str) -> None:
        at = run_in_editor(ask(start_app(), "q"), sql)
        assert not at.exception
        assert [e.value for e in at.error][0].startswith("Refused:")

    def test_a_bad_query_shows_no_stack_trace(self, agent: FakeAgent) -> None:
        at = run_in_editor(ask(start_app(), "q"), "SELECT nope FROM customers")
        assert not at.exception
        (message,) = [e.value for e in at.error]
        assert "nope" in message and "Traceback" not in message and "\n" not in message

    def test_session_limit(self, agent: FakeAgent,
                           monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCREW_SESSION_SQL_RUN_CAP", "1")
        at = run_in_editor(ask(start_app(), "q"), "SELECT 1")
        at = run_in_editor(at, "SELECT 2")
        assert any("this session's SQL runs" in w.value for w in at.warning)
        assert at.session_state["sql_runs_used"] == 1
