"""Architectures A (baseline) and B (v1 adapter), offline on scripted models."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import ScriptedAnalyst

from agentcrew.archs import run_architecture
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext


class Scripted:
    """Responder for architecture A: queued SQL drafts, then an answer."""

    def __init__(self, *sqls: str) -> None:
        self.sqls = list(sqls)
        self.calls: list[str] = []

    def __call__(self, system: str, user: str):
        if "SQL analyst" in system:
            self.calls.append("sql")
            return {"sql": self.sqls.pop(0), "rationale": "scripted",
                    "expected_shape": ""}
        if "final answer" in system:
            self.calls.append("answer")
            return {"answer": "Assuming completed orders, there are 1400.",
                    "key_numbers": [], "caveats": []}
        raise AssertionError(system[:80])


@pytest.fixture
def pinned(settings):
    return settings.model_copy(update={"today": date(2026, 10, 4)})


@pytest.fixture
def builder(engine):
    tools = ToolContext.create(engine)
    return ContextBuilder.from_database(engine, tools.catalog, today=date(2026, 10, 4))


def run_a(responder, pinned, engine, builder):
    client = FakeClient(responder=responder)
    outcome = run_architecture(
        "baseline", "How many customers do we have?", settings=pinned, llm=client,
        tools=ToolContext.create(engine), context_builder=builder,
    )
    return outcome, client


class TestBaseline:
    def test_one_query_then_one_answer(self, pinned, engine, builder):
        script = Scripted("SELECT COUNT(*) AS n FROM customers")
        outcome, client = run_a(script, pinned, engine, builder)
        assert outcome.status == "done"
        assert outcome.selected.rows == [[1400]]
        assert script.calls == ["sql", "answer"]
        assert outcome.path == "direct" and len(outcome.attempts) == 1
        sql_prompt = client.prompts[0][1]
        assert "## Data dictionary" in sql_prompt and "2026-10-04" in sql_prompt
        assert "How many customers do we have?" in sql_prompt

    def test_retries_once_on_an_sql_error(self, pinned, engine, builder):
        script = Scripted("SELECT nope FROM customers", "SELECT COUNT(*) FROM customers")
        outcome, client = run_a(script, pinned, engine, builder)
        assert script.calls == ["sql", "sql", "answer"]
        assert [a.ok for a in outcome.attempts] == [False, True]
        assert "no such column" in client.prompts[1][1]

    def test_guard_rejection_counts_as_an_error(self, pinned, engine, builder):
        script = Scripted("DELETE FROM customers", "SELECT COUNT(*) FROM customers")
        outcome, _ = run_a(script, pinned, engine, builder)
        assert outcome.attempts[0].failure == "blocked"
        assert outcome.status == "done"

    def test_no_retry_on_an_empty_result(self, pinned, engine, builder):
        script = Scripted("SELECT * FROM customers WHERE segment = 'Nope'")
        outcome, _ = run_a(script, pinned, engine, builder)
        assert script.calls == ["sql", "answer"]
        assert outcome.selected.rows == []

    def test_two_errors_fail_without_an_answer_call(self, pinned, engine, builder):
        script = Scripted("SELECT nope FROM x", "SELECT still_nope FROM x")
        outcome, _ = run_a(script, pinned, engine, builder)
        assert outcome.status == "failed" and outcome.selected is None
        assert script.calls == ["sql", "sql"]


class TestV1Adapter:
    def test_selected_result_plan_and_attempts(self, pinned, engine, builder):
        analyst = ScriptedAnalyst(
            plan_steps=["count customers"],
            default_sql="SELECT COUNT(*) AS n FROM customers",
        )
        outcome = run_architecture(
            "v1", "How many customers?", settings=pinned,
            llm=FakeClient(responder=analyst), tools=ToolContext.create(engine),
            context_builder=builder,
        )
        assert outcome.status == "done" and outcome.path == "v1"
        assert outcome.plan == ("count customers",)
        assert outcome.selected.rows == [[1400]]
        assert outcome.attempts[0].sql.startswith("SELECT COUNT(*)")
        assert outcome.attempts[0].preview

    def test_planner_sees_the_pinned_date(self, pinned, engine, builder):
        analyst = ScriptedAnalyst(default_sql="SELECT 1 AS answer")
        client = FakeClient(responder=analyst)
        run_architecture("v1", "Anything?", settings=pinned, llm=client,
                         tools=ToolContext.create(engine), context_builder=builder)
        assert "2026-10-04" in client.prompts[0][1]


def test_unknown_architecture_is_rejected(pinned, engine, builder):
    kwargs = dict(settings=pinned, llm=FakeClient(), tools=ToolContext.create(engine),
                  context_builder=builder)
    with pytest.raises(ValueError):
        run_architecture("nope", "q", **kwargs)
