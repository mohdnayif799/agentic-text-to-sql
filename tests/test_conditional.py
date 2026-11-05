"""Condition before branch: a conditional question evaluates its condition on
its own, keeps the values, and answers only the branch that applies.
Offline: a scripted model and the real database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.checks import conditional_signal, question_signal
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.presentation import escalation_reason
from agentcrew.tools import ToolContext

QUESTION = ("Compare the 2025 order counts of the Online and Direct channels. If "
            "Online's is more than twice Direct's, break Online's 2025 orders down "
            "by status; otherwise, break Direct's 2025 orders down by status.")
FIRST = "SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel"
CONDITION = (
    "SELECT SUM(channel = 'Online') AS online_orders, "
    "SUM(channel = 'Direct') AS direct_orders, "
    "SUM(channel = 'Online') > 2 * SUM(channel = 'Direct') AS condition_met "
    "FROM orders WHERE order_date BETWEEN '2025-01-01' AND '2025-12-31'"
)
BRANCH = ("SELECT status, COUNT(*) AS orders FROM orders "
          "WHERE channel = 'Online' AND order_date BETWEEN '2025-01-01' AND "
          "'2025-12-31' GROUP BY status ORDER BY status")
PLAN = {"condition_sql": CONDITION, "reason": "evaluate the condition"}
TAKEN = {"condition_met": True, "branch": "Online is more than twice Direct, so "
         "the Online branch applies.", "sql": BRANCH, "reason": "the Online branch"}


class TestSignal:
    @pytest.mark.parametrize("question", [
        QUESTION,
        "Did Partner grow faster than Online? If yes, break Partner down by "
        "segment; if not, break Online down by segment.",
        "If the gap is more than 5 points, show the rate by region.",
        "Compare the rates. if the gap is above 2, show them by month.",
        "Compare the rates. If the gap is more than 1.5 points, show them by month.",
        "If it rose by 2.5% or 3.75%, show it by region.",
        "If the average is above $1,000.50, show it by segment.",
    ])
    def test_a_condition_on_the_answer(self, question):
        signal = conditional_signal(question)
        assert signal is not None and signal.kind == "conditional"

    @pytest.mark.parametrize("question", [
        "How many customers signed up in 2025?",
        "What if we only count completed orders in 2025?",
        "Which region, if any, lost customers in 2025?",
        "Show revenue by region for orders that have a discount.",
        "If revenue rose. Then, show it by region.",
    ])
    def test_other_questions_do_not_signal(self, question):
        assert conditional_signal(question) is None

    def test_a_forecast_keeps_its_own_signal(self):
        found = question_signal("Forecast revenue. If it falls, show it by region.")
        assert found.kind == "forecast"


class Script:
    def __init__(self, *, plans=(PLAN,), branches=(TAKEN,), repairs=(), actions=()):
        self.plans, self.branches = list(plans), list(branches)
        self.repairs, self.actions = list(repairs), list(actions)
        self.calls: list[str] = []
        self.answer_prompt = ""

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": False, "reason": ""}
        if "depends on a condition" in system:
            self.calls.append("condition_plan")
            return self.plans.pop(0) if self.plans else {}
        if "condition of a question has been evaluated" in system:
            self.calls.append("branch_plan")
            return self.branches.pop(0) if self.branches else {}
        if "continuing an analysis" in system:
            self.calls.append("followup")
            return {"done": True, "sql": "", "reason": ""}
        if "You plan the next step" in system:
            self.calls.append("decide")
            return self.actions.pop(0) if self.actions else {"action": "answer"}
        if "You fix one SQLite query" in system:
            self.calls.append("repair")
            return {"sql": self.repairs.pop(0), "rationale": "x", "expected_shape": ""}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            return {"base_step": 1, "columns": []}
        if "final answer" in system:
            self.calls.append("answer")
            self.answer_prompt = user
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            self.calls.append("step0")
            return {"sql": FIRST, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(arch, script, question=QUESTION, **overrides):
        cfg = pinned.model_copy(update=overrides) if overrides else pinned
        return run_architecture(arch, question, settings=cfg,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


class TestArchitectureD:
    def test_condition_first_then_only_the_branch(self, run):
        script = Script()
        out = run("full", script)
        assert script.calls == ["step0", "condition_plan", "branch_plan", "answer"]
        assert out.path == "escalated"
        assert out.steps[0]["escalation_signal"] == "conditional"
        assert [s["sql"].split(" FROM")[0][:15] for s in out.steps] == [
            "SELECT channel,", "SELECT SUM(chan", "SELECT status, "]

    def test_the_final_table_is_the_branch_alone(self, run):
        out = run("full", Script())
        assert out.selected_step == 3
        assert out.selected.columns == ["status", "orders"]
        assert not any(s.get("step") == "final" for s in out.steps)

    def test_the_answer_sees_the_condition_values_and_the_branch(self, run):
        script = Script()
        run("full", script)
        prompt = script.answer_prompt
        guidance = prompt.split("## The condition, evaluated first", 1)[1]
        assert "online_orders" in guidance and "condition_met" in guidance
        assert "the Online branch applies" in guidance
        assert "naming the group" in guidance
        assert "SELECT status" in prompt.split("## The condition", 1)[0]

    def test_the_condition_query_is_checked_like_any_query(self, run):
        wrong_year = CONDITION.replace("2025", "2024")
        script = Script(plans=[{"condition_sql": wrong_year}], repairs=[CONDITION])
        out = run("full", script)
        assert any(r.signal == "date_range" and r.accepted for r in out.repairs)

    def test_a_hollow_row_in_the_branch_is_repaired(self, run):
        hollow = (BRANCH.replace(" ORDER BY status", "")
                  + " UNION ALL SELECT 'all', SUM(CASE WHEN 1 = 0 THEN 1 END) "
                    "FROM orders WHERE 1 = 0")
        script = Script(branches=[{**TAKEN, "sql": hollow}], repairs=[BRANCH])
        out = run("full", script)
        assert any(r.signal == "null_row" and r.accepted for r in out.repairs)
        assert out.selected.columns == ["status", "orders"]
        assert all(row[1] is not None for row in out.selected.rows)


def test_a_condition_with_a_decimal_takes_the_conditional_path(run):
    """Added after the pattern stopped at the '.' in '1.5', which sent such
    questions down the ordinary path without any error."""
    question = QUESTION.replace("more than twice Direct's", "more than 1.5 times Direct's")
    script = Script()
    out = run("full", script, question=question)
    assert out.steps[0]["escalation_signal"] == "conditional"
    assert script.calls == ["step0", "condition_plan", "branch_plan", "answer"]


class TestFallbacks:
    def test_no_valid_condition_plan_continues_with_the_ordinary_loop(self, run):
        script = Script(plans=[])
        out = run("full", script)
        assert "decide" in script.calls and out.status == "done"
        assert out.selected_step == 1

    def test_a_failing_condition_query_never_reaches_a_branch(self, run):
        broken = {"condition_sql": "SELECT nope FROM orders"}
        fix = "SELECT nope2 FROM orders"
        script = Script(plans=[broken], repairs=[fix, fix + " "])
        out = run("full", script)
        assert "branch_plan" not in script.calls
        assert out.status == "done"
        assert out.selected.columns == ["channel", "n"]

    def test_no_valid_branch_plan_answers_without_condition_guidance(self, run):
        script = Script(branches=[])
        out = run("full", script)
        assert out.status == "done"
        assert "## The condition" not in script.answer_prompt

    def test_a_failing_branch_query_is_not_the_final_table(self, run):
        bad = {**TAKEN, "sql": "SELECT nope FROM orders"}
        fix = "SELECT nope2 FROM orders"
        script = Script(branches=[bad], repairs=[fix, fix + " "])
        out = run("full", script)
        assert out.status == "done"
        assert "nope" not in out.selected.sql
        assert "## The condition" not in script.answer_prompt

    def test_two_steps_are_needed_for_condition_and_branch(self, run):
        script = Script()
        out = run("full", script, max_steps=2)
        assert "condition_plan" not in script.calls and out.status == "done"


class TestOtherArchitectures:
    def test_c_escalates_on_the_same_signal_with_its_own_follow_ups(self, run):
        script = Script()
        out = run("router", script)
        assert out.path == "escalated"
        assert out.steps[0]["escalation_signal"] == "conditional"
        assert "decision" not in script.calls and "followup" in script.calls
        assert "condition_plan" not in script.calls

    def test_a_is_not_changed(self, run):
        script = Script()
        out = run("baseline", script)
        assert script.calls == ["step0", "answer"]
        assert out.selected.columns == ["channel", "n"]


def test_a_conditional_question_keeps_the_date_range_check(run):
    """Only a forecast's fit window may lie outside the period it names."""
    script = Script(plans=[{"condition_sql": CONDITION.replace("2025", "2024")}],
                    repairs=[CONDITION])
    out = run("full", script)
    assert any(r.signal == "date_range" for r in out.repairs)


def test_the_app_can_say_why_it_escalated(run):
    out = run("full", Script())
    assert "condition" in escalation_reason(out)
