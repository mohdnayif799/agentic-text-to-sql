"""A metric plan the tools would refuse is rejected when it is parsed, with the
reason, so the one structured-output retry fixes it and no step is spent.
Offline: a scripted model and the real database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.explore import MetricPlan, NextAction, compute
from agentcrew.compute.metrics import METRICS, Comparison, catalog, validate_call
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext

Q2 = {"start": "2025-04-01", "end": "2025-06-30"}
Q3 = {"start": "2025-07-01", "end": "2025-09-30"}
BY_PERIOD = {"period_a": Q2, "period_b": Q3}
BY_GROUP = {"group_dimension": "region", "groups": ["APAC", "EMEA"]}


class TestMetricPlan:
    def test_a_rate_that_cannot_be_split_by_period_is_rejected_with_the_way_out(self):
        with pytest.raises(ValueError, match="churn_rate cannot be split by period"
                                             r".*churned_customers"):
            MetricPlan(metric="churn_rate", comparison=BY_PERIOD, dimension="region")

    def test_a_common_period_is_a_period_too(self):
        with pytest.raises(ValueError, match="cannot be split by period"):
            MetricPlan(metric="churn_rate",
                       comparison={**BY_GROUP, "period": Q3})

    def test_the_same_rate_between_groups_is_fine(self):
        plan = MetricPlan(metric="churn_rate", comparison=BY_GROUP)
        assert plan.to_action().function == "decompose_change"

    @pytest.mark.parametrize("comparison", [
        {**BY_PERIOD, "filters": {"region_name": "APAC"}},
        {"group_dimension": "colour", "groups": ["a", "b"]},
    ])
    def test_a_dimension_the_source_does_not_have_is_rejected(self, comparison):
        with pytest.raises(ValueError, match="is not available for"):
            MetricPlan(metric="revenue", comparison=comparison)

    def test_a_breakdown_needs_a_dimension_of_its_source(self):
        with pytest.raises(ValueError, match="is not available for"):
            MetricPlan(metric="churned_customers", comparison=BY_PERIOD,
                       dimension="category")


SAME = {"period_a": Q3, "period_b": Q3}
GROUPS = {"group_dimension": "region", "groups": ["APAC", "EMEA"]}
Y2025 = {"start": "2025-01-01", "end": "2025-12-31"}


class TestOverlappingPeriods:
    """A period compared with itself, or with part of itself, is not a change.
    Found by the dev check: the planner gave sc03 the same period twice and the
    tool returned a table of zeros."""

    @pytest.mark.parametrize("periods", [
        SAME,
        {"period_a": Y2025, "period_b": Q3},
        {"period_a": Q3, "period_b": Y2025},
        {"period_a": {"start": "2025-01-01", "end": "2025-07-01"}, "period_b": Q3},
    ])
    @pytest.mark.parametrize("extra", [{}, GROUPS])
    def test_rejected_with_or_without_groups(self, periods, extra):
        with pytest.raises(ValueError, match="overlap"):
            Comparison.model_validate({**periods, **extra})

    def test_the_reason_says_what_to_do_instead(self):
        with pytest.raises(ValueError, match="to compare groups over one period"):
            MetricPlan(metric="revenue", comparison={**SAME, **GROUPS})

    @pytest.mark.parametrize("periods", [
        {"period_a": Q2, "period_b": Q3},
        {"period_a": Q3, "period_b": Q2},
    ])
    @pytest.mark.parametrize("extra", [{}, GROUPS])
    def test_periods_that_only_touch_are_fine(self, periods, extra):
        assert Comparison.model_validate({**periods, **extra}).by_period

    def test_groups_over_one_period_stay_fine(self):
        comparison = Comparison.model_validate({**GROUPS, "period": Q3})
        assert not comparison.by_period and not comparison.across_groups


def action(function, **params):
    return {"action": "compute", "function": function, "params": params}


class TestNextAction:
    @pytest.mark.parametrize(("reply", "message"), [
        (action("decompose_change", metric="churn_rate", comparison=BY_PERIOD),
         "cannot be split by period"),
        (action("decompose_change", metric="tickets", comparison=BY_PERIOD),
         "no component decomposition"),
        (action("contribution_by_dimension", metric="refund_rate",
                dimension="region", comparison=BY_PERIOD), "is a ratio"),
        (action("contribution_by_dimension", metric="revenue",
                dimension="category",
                comparison={**BY_PERIOD, "filters": {"region_name": "APAC"}}),
         "dimension 'region_name' is not available"),
        (action("contribution_by_dimension", metric="revenue",
                comparison=BY_PERIOD), "is not available"),
        (action("decompose_change", metric="profit", comparison=BY_PERIOD),
         "unknown metric"),
        (action("decompose_change", metric="revenue", comparison={}), "period_a"),
    ])
    def test_rejected_at_parse_time(self, reply, message):
        with pytest.raises(ValueError, match=message):
            NextAction.model_validate(reply)

    def test_a_valid_call_parses(self):
        parsed = NextAction.model_validate(
            action("contribution_by_dimension", metric="churned_customers",
                   dimension="region", comparison=BY_PERIOD))
        assert parsed.function == "contribution_by_dimension"

    def test_other_compute_functions_are_not_checked_here(self):
        parsed = NextAction.model_validate(action("pct_change", step=1, value="n"))
        assert parsed.function == "pct_change"

    def test_the_dispatcher_keeps_its_own_guard(self):
        """Parsing is the early check; the tool still refuses on its own."""
        unchecked = NextAction.model_construct(
            action="compute", function="decompose_change", reason="",
            params={"metric": "churn_rate", "comparison": BY_PERIOD})

        def no_query(sql: str) -> ExecutionResult:
            raise AssertionError("refused before any SQL")

        result, _ = compute(unchecked, {}, no_query)
        assert not result.ok and "cannot be split by period" in result.error_message


class TestCatalog:
    def test_it_says_which_rate_cannot_compare_periods_and_what_to_use(self):
        line = next(ln for ln in catalog().splitlines() if ln.startswith("- churn_rate"))
        assert "between groups only" in line and "churned_customers" in line

    @pytest.mark.parametrize("name", list(METRICS))
    def test_every_tool_it_lists_for_periods_is_really_allowed(self, name):
        """The catalog and the validator cannot disagree: a tool listed with
        no restriction accepts a period comparison."""
        line = next(ln for ln in catalog().splitlines() if ln.startswith(f"- {name} "))
        listed = line.split("tools: ", 1)[1]
        comparison = Comparison.model_validate(BY_PERIOD)
        for tool in ("decompose_change", "contribution_by_dimension"):
            if tool not in listed:
                continue
            restricted = f"{tool} (between groups only" in listed
            dimension = "region" if tool == "contribution_by_dimension" else None
            if restricted:
                with pytest.raises(ValueError):
                    validate_call(tool, name, comparison, dimension)
            else:
                validate_call(tool, name, comparison, dimension)


COUNT = "SELECT COUNT(*) AS n FROM customers"
GOOD_PLAN = {"metric": "churned_customers", "comparison": BY_PERIOD,
             "dimension": "region", "reason": "by region"}
BAD_PLAN = {"metric": "churn_rate", "comparison": BY_PERIOD, "dimension": "region",
            "reason": "rate by period"}


class Script:
    def __init__(self, plans=(), actions=()):
        self.plans, self.actions = list(plans), list(actions)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": True, "reason": "explain the cause"}
        if "You start the analysis of a question that asks why" in system:
            self.calls.append("metric_plan")
            self.prompts.append(user)
            return self.plans.pop(0)
        if "You plan the next step" in system:
            self.calls.append("decide")
            self.prompts.append(user)
            return self.actions.pop(0) if self.actions else {"action": "answer"}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            return {"base_step": 1, "columns": []}
        if "final answer" in system:
            self.calls.append("answer")
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            self.calls.append("step0")
            return {"sql": COUNT, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(script, question):
        return run_architecture("full", question, settings=pinned,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


WHY = "Why did customer churn rise in Q3 2025?"


class TestTheRetryCostsNoStep:
    def test_a_period_compared_with_itself_is_retried_with_the_reason(self, run):
        same = {"metric": "revenue", "reason": "x", "comparison": {**SAME, **GROUPS}}
        fixed = {"metric": "revenue", "reason": "x",
                 "comparison": {**GROUPS, "period": Q3}}
        script = Script(plans=[same, fixed])
        out = run(script, "Why does APAC bring in more revenue than EMEA in Q3 2025?")
        assert script.calls.count("metric_plan") == 2
        assert "overlap" in script.prompts[1]
        assert out.selected.rows[0][0] == "EMEA"  # side_a: the groups, not a period
        assert not all(row[6] == 0 for row in out.selected.rows)  # change

    def test_a_rejected_metric_plan_is_retried_with_the_reason(self, run):
        script = Script(plans=[BAD_PLAN, GOOD_PLAN])
        out = run(script, WHY)
        assert script.calls.count("metric_plan") == 2
        assert "cannot be split by period" in script.prompts[1]
        assert [s["tools"][0] for s in out.steps if s["step"] != "final"] == [
            "run_sql", "compute:contribution_by_dimension"]
        assert all(a.error is None for a in out.attempts)

    def test_two_rejected_plans_use_no_step_and_the_loop_goes_on(self, run):
        script = Script(plans=[BAD_PLAN, BAD_PLAN])
        out = run(script, WHY)
        assert [s["tools"][0] for s in out.steps if s["step"] != "final"] == ["run_sql"]
        assert "decide" in script.calls and out.status == "done"

    def test_a_rejected_next_action_is_retried_with_the_reason(self, run):
        bad = action("contribution_by_dimension", metric="churned_customers",
                     dimension="region",
                     comparison={**BY_PERIOD, "filters": {"region_name": "APAC"}})
        good = action("contribution_by_dimension", metric="churned_customers",
                      dimension="region",
                      comparison={**BY_PERIOD, "filters": {"region": "APAC"}})
        script = Script(plans=[GOOD_PLAN], actions=[bad, good])
        out = run(script, WHY)
        assert script.calls.count("decide") == 2
        assert "region_name" in script.prompts[-1]
        assert all(a.error is None for a in out.attempts)
        assert len([s for s in out.steps if s["step"] != "final"]) == 3
