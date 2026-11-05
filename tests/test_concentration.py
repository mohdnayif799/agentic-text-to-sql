"""The concentration check: before a breakdown, prefer the dimension where one value
holds the largest share of the change. Offline: synthetic tables, then a scripted
model and the real database."""

from __future__ import annotations

import re
from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.compute.concentration import concentrating_dimension
from agentcrew.compute.metrics import Comparison
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext, execute_readonly_sql

Q1 = {"start": "2024-04-01", "end": "2024-06-30"}
Q2 = {"start": "2024-07-01", "end": "2024-09-30"}
BY_PERIOD = Comparison.model_validate({"period_a": Q1, "period_b": Q2})


def fake_tables(tables, seen=None):
    """A runner over {dimension: (earlier values, later values)} for `tickets`."""
    def run(sql: str) -> ExecutionResult:
        dimension = re.search(r" AS (\w+), COUNT", sql).group(1)
        if seen is not None:
            seen.append(dimension)
        earlier, later = tables[dimension]
        values = earlier if "'2024-04-01'" in sql else later
        rows = [[item, value] for item, value in values.items()]
        return ExecutionResult(ok=True, sql=sql, columns=[dimension, "tickets"],
                               rows=rows, row_count=len(rows))
    return run


#: The rise sits in one region, and is spread evenly across segments.
TABLES = {
    "region": ({"North": 10, "South": 10, "East": 10}, {"North": 90, "South": 12, "East": 12}),
    "country": ({"N": 10, "S": 10, "E": 10}, {"N": 90, "S": 12, "E": 12}),
    "segment": ({"A": 10, "B": 10, "C": 10}, {"A": 40, "B": 38, "C": 36}),
    "severity": ({"low": 15, "high": 15}, {"low": 60, "high": 54}),
}


class TestChoice:
    def test_the_dimension_with_the_largest_single_share_wins(self):
        best = concentrating_dimension(fake_tables(TABLES), "tickets", BY_PERIOD)
        assert (best.dimension, best.item) == ("region", "North")
        assert best.share_pct == pytest.approx(80 / 84 * 100)

    def test_a_tie_keeps_the_dimension_the_plan_chose(self):
        best = concentrating_dimension(fake_tables(TABLES), "tickets", BY_PERIOD,
                                       preferred="country")
        assert best.dimension == "country"

    def test_the_plan_loses_to_a_clearly_more_concentrated_dimension(self):
        best = concentrating_dimension(fake_tables(TABLES), "tickets", BY_PERIOD,
                                       preferred="segment")
        assert best.dimension == "region"

    def test_time_dimensions_are_not_candidates(self):
        """Two periods split a time dimension trivially, so it explains nothing."""
        seen: list[str] = []
        concentrating_dimension(fake_tables(TABLES, seen), "tickets", BY_PERIOD)
        assert not {"month", "quarter", "year"} & set(seen)

    def test_a_filtered_dimension_is_not_a_candidate(self):
        """Filtered to one region, that region trivially holds the whole change."""
        seen: list[str] = []
        comparison = Comparison.model_validate(
            {"period_a": Q1, "period_b": Q2, "filters": {"region": "North"}})
        concentrating_dimension(fake_tables(TABLES, seen), "tickets", comparison)
        assert "region" not in seen

    def test_a_dimension_with_one_value_cannot_concentrate_anything(self):
        tables = {**TABLES, "country": ({"N": 10}, {"N": 200})}
        best = concentrating_dimension(fake_tables(tables), "tickets", BY_PERIOD)
        assert best.dimension == "region"

    def test_no_change_means_no_choice(self):
        flat = {d: (values[0], values[0]) for d, values in TABLES.items()}
        assert concentrating_dimension(fake_tables(flat), "tickets", BY_PERIOD) is None

    def test_a_dimension_that_cannot_be_read_is_skipped(self):
        def run(sql: str) -> ExecutionResult:
            if " AS segment," in sql:
                return ExecutionResult(ok=False, sql=sql, error_message="boom")
            return fake_tables(TABLES)(sql)

        assert concentrating_dimension(run, "tickets", BY_PERIOD).dimension == "region"

    def test_a_metric_with_no_breakdown_gives_no_choice(self):
        """A ratio cannot be broken down, so there is nothing to compare."""
        assert concentrating_dimension(fake_tables(TABLES), "refund_rate",
                                       BY_PERIOD) is None


COUNT = "SELECT COUNT(*) AS n FROM marketing_spend"


def plan(dimension):
    return {"metric": "marketing_spend", "dimension": dimension, "reason": "by value",
            "comparison": {"period_a": Q1, "period_b": Q2}}


class Script:
    def __init__(self, plans):
        self.plans = list(plans)
        self.calls: list[str] = []
        self.answer_prompt = ""

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": True, "reason": "explain the cause"}
        if "You start the analysis of a question that asks why" in system:
            self.calls.append("metric_plan")
            return self.plans.pop(0)
        if "You plan the next step" in system:
            self.calls.append("decide")
            return {"action": "answer"}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            return {"base_step": 1, "columns": []}
        if "final answer" in system:
            self.calls.append("answer")
            self.answer_prompt = user
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            self.calls.append("step0")
            return {"sql": COUNT, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def run_sql(engine):
    tools = ToolContext.create(engine)
    return lambda sql: execute_readonly_sql(tools, sql)


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(script, question="Why did marketing spend jump in Q3 2024?", **overrides):
        cfg = pinned.model_copy(update=overrides) if overrides else pinned
        return run_architecture("full", question, settings=cfg,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


def analyses(out):
    index = out.selected.columns.index("analysis")
    return list(dict.fromkeys(row[index] for row in out.selected.rows))


class TestInTheRun:
    def best_and_other(self, run_sql):
        comparison = Comparison.model_validate({"period_a": Q1, "period_b": Q2})
        best = concentrating_dimension(run_sql, "marketing_spend", comparison)
        other = next(d for d in ("channel", "region") if d != best.dimension)
        return best, other

    def test_the_most_concentrated_breakdown_comes_first_and_the_plan_stays(
        self, run, run_sql
    ):
        best, other = self.best_and_other(run_sql)
        out = run(Script([plan(other)]))
        assert analyses(out) == [f"marketing_spend by {best.dimension}",
                                 f"marketing_spend by {other}"]

    def test_it_adds_no_llm_call_and_no_step(self, run, run_sql):
        _, other = self.best_and_other(run_sql)
        script = Script([plan(other)])
        out = run(script)
        assert script.calls == ["step0", "decision", "metric_plan", "answer"]
        assert [s["tools"][0] for s in out.steps if s["step"] != "final"] == [
            "run_sql", "compute:contribution_by_dimension", "compute:period_history"]

    def test_the_history_follows_the_chosen_dimension(self, run, run_sql):
        best, other = self.best_and_other(run_sql)
        script = Script([plan(other)])
        run(script)
        guidance = script.answer_prompt.split("## What is normal", 1)[1]
        assert f"\n{best.dimension} | " in guidance

    def test_the_reason_says_why_the_dimension_changed(self, run, run_sql):
        best, other = self.best_and_other(run_sql)
        out = run(Script([plan(other)]))
        step = next(s for s in out.steps if s["step"] == 2)
        assert best.dimension in step["reason"] and "largest share" in step["reason"]

    def test_the_answer_step_is_told_which_breakdown_leads(self, run, run_sql):
        _, other = self.best_and_other(run_sql)
        script = Script([plan(other)])
        run(script)
        assert "the first breakdown concentrates the change most" in script.answer_prompt

    def test_the_main_driver_comes_from_the_leading_breakdown(self, run, run_sql):
        best, other = self.best_and_other(run_sql)
        out = run(Script([plan(other)]))
        assert out.answer.startswith("Main driver (largest contributor): ")
        assert f"({'marketing_spend'} by {best.dimension})" in out.answer

    def test_a_plan_that_already_chose_it_is_left_alone(self, run, run_sql):
        best, _ = self.best_and_other(run_sql)
        script = Script([plan(best.dimension)])
        out = run(script)
        assert analyses(out) == [f"marketing_spend by {best.dimension}"]
        assert "the first breakdown" not in script.answer_prompt

    def test_a_metric_with_components_is_left_alone(self, run):
        decomposed = {"metric": "revenue", "reason": "x", "dimension": "channel",
                      "comparison": {"period_a": Q1, "period_b": Q2}}
        out = run(Script([decomposed]), question="Why did revenue jump in Q3 2024?")
        assert "orders x average order value" in analyses(out)
        assert not any(" by " in name for name in analyses(out))

    def test_a_is_not_changed(self, settings, engine):
        pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
        tools = ToolContext.create(engine)
        builder = ContextBuilder.from_database(engine, tools.catalog,
                                               today=date(2026, 10, 4))
        script = Script([])
        run_architecture("baseline", "Why did marketing spend jump in Q3 2024?",
                         settings=pinned, llm=FakeClient(responder=script), tools=tools,
                         context_builder=builder)
        assert script.calls == ["step0", "answer"]
