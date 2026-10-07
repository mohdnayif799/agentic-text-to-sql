"""A failed escalation never makes C or D worse than A.

When every escalated step fails, is rejected, or produces no usable result,
C's and D's final table and answer are exactly A's: the same rows and the
same answer text (the scripted answer depends only on the answer prompt, so
equal answers mean the same answer call was made on the same table).
Offline, scripted; the database is the real one.
"""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext

COUNT = "SELECT COUNT(*) AS n FROM customers"
EMPTY = "SELECT customer_id FROM customers WHERE customer_name = 'Nobody'"
BROKEN = "SELECT nope FROM customers"
NOT_JSON = "this is not JSON"

BAD_STEPS = [
    {"action": "run_sql", "sql": "SELECT missing_column FROM orders"},
    {"action": "compute", "function": "pct_change", "params": {"step": 9}},
    {"action": "get_distinct_values", "table": "invoices", "column": "id"},
]


def scripted(*, step0=COUNT, retry=COUNT, repair=BROKEN, decision=True,
             actions=(), plans_valid=False, followup_sql=BROKEN,
             escalated_answer_ok=True, decision_ok=True):
    actions = list(actions)

    def respond(system: str, user: str):
        if "SQL analyst" in system:
            sql = retry if "Your previous query failed" in user else step0
            return {"sql": sql, "rationale": "x", "expected_shape": ""}
        if "You check whether one query result" in system:
            if not decision_ok:
                return NOT_JSON
            return {"needs_followup": decision, "reason": "check more"}
        if "You fix one SQLite query" in system:
            return {"sql": repair, "rationale": "x", "expected_shape": ""}
        if "continuing an analysis" in system:
            return {"done": False, "sql": followup_sql, "reason": "more"}
        if "You start the analysis of a question that asks" in system:
            return {} if not plans_valid else NOT_JSON
        if "You plan the next step" in system:
            return actions.pop(0) if actions else {"action": "answer"}
        if "Several queries were run" in system:
            return {"base_step": 2, "columns": []}
        if "final answer" in system:
            escalated_table = "-- assembled" in user or "-- compute" in user
            if escalated_table and not escalated_answer_ok:
                return NOT_JSON
            digest = hashlib.sha256(user.encode()).hexdigest()[:12]
            return {"answer": f"answer {digest}", "key_numbers": [], "caveats": []}
        raise AssertionError(system[:80])
    return respond


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(arch, question, **script):
        return run_architecture(arch, question, settings=pinned,
                                llm=FakeClient(responder=scripted(**script)),
                                tools=ToolContext.create(engine),
                                context_builder=builder)
    return go


def same_as_a(run, question, archs=("router", "full"), **script):
    a = run("baseline", question, **script)
    for arch in archs:
        out = run(arch, question, **script)
        assert out.status == a.status, arch
        assert (out.selected.rows if out.selected else None) == \
            (a.selected.rows if a.selected else None), arch
        assert out.answer == a.answer, arch
        if out.selected is not None:
            assert out.selected_step == 1, arch
    return a


def test_every_exploration_step_fails(run):
    """D escalates; a bad query, a bad compute and a bad lookup all fail."""
    same_as_a(run, "How many customers do we have?", archs=("full",),
              actions=BAD_STEPS)


def test_every_follow_up_query_fails(run):
    same_as_a(run, "How many customers do we have?", archs=("router",))


def test_failed_metric_plan_on_a_why_question(run):
    same_as_a(run, "Why do we have so many customers?", actions=BAD_STEPS[:2])


def test_failed_trend_plan_on_a_forecast_question(run):
    same_as_a(run, "Forecast the customer count for next month.",
              actions=BAD_STEPS[:1])


def test_invalid_follow_up_decision(run):
    same_as_a(run, "How many customers do we have?", decision_ok=False)


def test_answer_call_failing_on_the_escalated_table(run):
    """D assembles a second table but its answer call fails: the run answers
    from A's result with A's answer call."""
    out = same_as_a(run, "How many customers do we have?", archs=("full",),
                    actions=[{"action": "run_sql", "sql": "SELECT 1 AS one"}],
                    escalated_answer_ok=False)
    assert out.status == "done"


def test_step0_error_is_retried_the_way_a_retries(run):
    """A fixes its own SQL error with its retry; C's repair would break it.
    C and D take A's retry first, so they end where A ends."""
    same_as_a(run, "How many customers do we have?", step0=BROKEN, retry=COUNT,
              repair="SELECT also_broken FROM customers", actions=BAD_STEPS)


def test_empty_result_with_failing_repairs_and_steps(run):
    a = same_as_a(run, "Which customer is called Nobody?", step0=EMPTY,
                  repair=EMPTY + " AND 1 = 1", actions=BAD_STEPS)
    assert a.selected.rows == []


def test_a_successful_escalation_is_not_forced_back(run):
    """The fallback applies only when nothing escalated is usable."""
    out = run("full", "How many customers do we have?",
              actions=[{"action": "run_sql", "sql": "SELECT 1 AS one"}])
    assert out.selected_step == 2 and out.selected.rows == [[1]]
