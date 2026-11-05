"""Why one group changed more than the others: decompose_change compares each
component's change across groups and ranks what differs most. Offline: synthetic
tables, then a scripted model and the real database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.explore import MetricPlan, NextAction
from agentcrew.compute.functions import ComputeError
from agentcrew.compute.metrics import (
    COLUMNS,
    GROUP_GAP_PREFIX,
    Comparison,
    decompose_change,
    validate_call,
)
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext, execute_readonly_sql

H1 = {"start": "2024-01-01", "end": "2024-06-30"}
H2 = {"start": "2024-07-01", "end": "2024-12-31"}
ACROSS = {"period_a": H1, "period_b": H2, "group_dimension": "channel",
          "groups": ["Online", "Direct", "Partner"]}


class TestComparisonMode:
    def test_periods_with_groups_is_the_across_groups_mode(self):
        comparison = Comparison.model_validate(ACROSS)
        assert comparison.across_groups

    @pytest.mark.parametrize("mode", [
        {"period_a": H1, "period_b": H2},
        {"group_dimension": "channel", "groups": ["Online", "Direct"]},
        {"group_dimension": "channel", "group_a": "Online", "group_b": "Direct"},
    ])
    def test_the_other_modes_are_not_it(self, mode):
        assert not Comparison.model_validate(mode).across_groups

    @pytest.mark.parametrize("bad", [
        {"period_a": H1, "period_b": H2, "group_dimension": "channel",
         "group_a": "Online", "group_b": "Direct"},
        {"period_a": H1, "group_dimension": "channel", "groups": ["Online", "Direct"]},
        {"period_a": H1, "period_b": H2, "groups": ["Online", "Direct"]},
    ])
    def test_half_formed_modes_are_rejected(self, bad):
        with pytest.raises(ValueError):
            Comparison.model_validate(bad)

    def test_a_breakdown_has_no_across_groups_mode(self):
        with pytest.raises(ComputeError, match="decompose_change"):
            validate_call("contribution_by_dimension", "revenue",
                          Comparison.model_validate(ACROSS), "category")

    def test_a_plan_keeps_the_mode_through_to_the_tool(self):
        plan = MetricPlan(metric="revenue", comparison=ACROSS)
        action = plan.to_action()
        assert action.function == "decompose_change"
        assert Comparison.model_validate(action.params["comparison"]).across_groups

    def test_a_next_action_may_use_it(self):
        parsed = NextAction.model_validate({
            "action": "compute", "function": "decompose_change",
            "params": {"metric": "revenue", "comparison": ACROSS}})
        assert parsed.function == "decompose_change"


def table(orders_a, value_a, orders_b, value_b):
    """Two periods of one group: orders and average order value."""
    return ((orders_a, value_a), (orders_b, value_b))


#: The subject's orders fall much more; order value falls about the same in every
#: group and is the larger part of each group's own fall, so a decomposition of
#: the subject alone names the wrong thing.
GROUPS = {
    "North": table(100, 100.0, 70, 60.0),
    "South": table(200, 100.0, 190, 62.0),
    "East": table(300, 100.0, 280, 58.0),
}


def fake_run(sql: str) -> ExecutionResult:
    group = next(name for name in GROUPS if f"'{name}'" in sql)
    side = 0 if "'2024-01-01'" in sql else 1
    orders, value = GROUPS[group][side]
    revenue = orders * value
    columns = ["revenue", "order_count", "units_sold", "gross_sales"]
    return ExecutionResult(ok=True, sql=sql, columns=columns,
                           rows=[[revenue, orders, orders * 2.0, revenue * 1.1]],
                           row_count=1)


def synthetic(metric="revenue"):
    comparison = Comparison.model_validate(
        {"period_a": H1, "period_b": H2, "group_dimension": "region",
         "groups": ["North", "South", "East"]})
    df, queries = decompose_change(fake_run, metric, comparison)
    return df, queries


class TestSyntheticRanking:
    def test_the_gap_rows_come_first_in_the_shared_schema(self):
        df, _ = synthetic()
        assert tuple(df.columns) == COLUMNS
        assert df.iloc[0]["analysis"].startswith(GROUP_GAP_PREFIX)

    def test_the_component_that_differs_most_is_marked_not_the_largest_fall(self):
        df, _ = synthetic()
        gap = df[df["analysis"] == f"{GROUP_GAP_PREFIX}orders x average order value"]
        marked = gap[gap["largest_contributor"]]
        assert marked["item"].tolist() == ["number of orders"]
        own = df[df["analysis"] == "North: orders x average order value"]
        by_item = own.set_index("item")["share_of_change_pct"]
        assert by_item["average order value"] > by_item["number of orders"]

    def test_values_are_percent_changes_against_the_others_average(self):
        df, _ = synthetic()
        row = df[(df["item"] == "number of orders")
                 & df["analysis"].str.startswith(GROUP_GAP_PREFIX)].iloc[0]
        others = ((190 - 200) / 200 * 100 + (280 - 300) / 300 * 100) / 2
        assert row["value_b"] == pytest.approx(-30.0)
        assert row["value_a"] == pytest.approx(others)
        assert row["change"] == pytest.approx(-30.0 - others)
        assert row["side_b"] == "North" and "region" in row["side_a"]
        assert row["comparison"] == "lower"

    def test_a_component_that_moved_alike_is_about_the_same(self):
        df, _ = synthetic()
        row = df[(df["item"] == "average order value")
                 & df["analysis"].str.startswith(GROUP_GAP_PREFIX)].iloc[0]
        assert row["comparison"] == "about the same"

    def test_shares_of_the_extra_change_add_up_to_100(self):
        df, _ = synthetic()
        gap = df[df["analysis"] == f"{GROUP_GAP_PREFIX}orders x average order value"]
        assert gap["share_of_change_pct"].sum() == pytest.approx(100.0)

    def test_every_group_keeps_its_own_figures_and_no_mark(self):
        df, queries = synthetic()
        assert len(queries) == 6  # two periods per group
        for group in GROUPS:
            own = df[df["analysis"].str.startswith(f"{group}: ")]
            assert not own.empty and not own["largest_contributor"].any()
        north = df[df["analysis"] == "North: orders x average order value"]
        orders = north[north["item"] == "number of orders"].iloc[0]
        assert (orders["value_a"], orders["value_b"]) == (100.0, 70.0)
        assert orders["pct_change"] == pytest.approx(-30.0)


class TestOnTheDatabase:
    @pytest.fixture
    def run_sql(self, engine):
        tools = ToolContext.create(engine)
        return lambda sql: execute_readonly_sql(tools, sql)

    def orders(self, run_sql, channel, period):
        return run_sql(
            f"SELECT COUNT(*) FROM orders WHERE status = 'completed' AND channel = "
            f"'{channel}' AND order_date BETWEEN '{period['start']}' AND "
            f"'{period['end']}'").rows[0][0]

    def test_figures_match_the_data(self, run_sql):
        df, _ = decompose_change(run_sql, "revenue", Comparison.model_validate(ACROSS))
        own = df[(df["analysis"] == "Direct: orders x average order value")
                 & (df["item"] == "number of orders")].iloc[0]
        assert own["value_a"] == self.orders(run_sql, "Direct", H1)
        assert own["value_b"] == self.orders(run_sql, "Direct", H2)
        gap = df[df["analysis"].str.startswith(GROUP_GAP_PREFIX)
                 & (df["item"] == "number of orders")].iloc[0]
        online = df[(df["analysis"] == "Online: orders x average order value")
                    & (df["item"] == "number of orders")].iloc[0]
        assert gap["value_b"] == pytest.approx(online["pct_change"])

    def test_one_mark_per_decomposition(self, run_sql):
        df, _ = decompose_change(run_sql, "revenue", Comparison.model_validate(ACROSS))
        gap = df[df["analysis"].str.startswith(GROUP_GAP_PREFIX)]
        assert gap.groupby("analysis")["largest_contributor"].sum().tolist() == [1, 1]

    @pytest.mark.parametrize("metric", ["gross_margin", "refund_rate", "order_count"])
    def test_other_decompositions_work_too(self, run_sql, metric):
        df, _ = decompose_change(run_sql, metric, Comparison.model_validate(ACROSS))
        gap = df[df["analysis"].str.startswith(GROUP_GAP_PREFIX)]
        assert not gap.empty
        assert gap.groupby("analysis")["share_of_change_pct"].sum().round(6).eq(
            100).all()

    def test_a_group_with_no_data_is_refused(self, run_sql):
        missing = {**ACROSS, "groups": ["Online", "Fax"]}
        with pytest.raises(ComputeError, match="no data"):
            decompose_change(run_sql, "revenue", Comparison.model_validate(missing))

    def test_a_rate_that_cannot_compare_periods_stays_refused(self, run_sql):
        with pytest.raises(ComputeError, match="cannot be split by period"):
            decompose_change(run_sql, "churn_rate", Comparison.model_validate(
                {"period_a": H1, "period_b": H2, "group_dimension": "region",
                 "groups": ["APAC", "EMEA"]}))


COUNT = "SELECT COUNT(*) AS n FROM orders"
PLAN = {"metric": "revenue", "reason": "compare the change", "comparison": ACROSS}
WHY = "Why did Online's revenue fall so much more than the other channels' in 2024?"


class Script:
    def __init__(self, plans=(PLAN,), answer="Fewer orders. The number of orders fell."):
        self.plans = list(plans)
        self.answer = answer
        self.calls: list[str] = []
        self.plan_prompt = ""
        self.answer_prompt = ""

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": True, "reason": "explain the cause"}
        if "You start the analysis of a question that asks why" in system:
            self.calls.append("metric_plan")
            self.plan_prompt = system + user
            return self.plans.pop(0) if self.plans else {}
        if "continuing an analysis" in system:
            self.calls.append("followup")
            return {"done": True, "sql": "", "reason": ""}
        if "You plan the next step" in system:
            self.calls.append("decide")
            return {"action": "answer"}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            return {"base_step": 1, "columns": []}
        if "final answer" in system:
            self.calls.append("answer")
            self.answer_prompt = user
            return {"answer": self.answer, "key_numbers": [], "caveats": []}
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

    def go(arch, script, question=WHY):
        return run_architecture(arch, question, settings=pinned,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


class TestInTheRun:
    def test_the_table_and_guidance_are_the_across_groups_ones(self, run):
        script = Script()
        out = run("full", script)
        assert out.selected.rows[0][2].startswith(GROUP_GAP_PREFIX)
        assert "the group being explained" in script.answer_prompt
        assert "changed by a similar amount in every group" in script.answer_prompt
        assert "First sentence: the main driver, which" not in script.answer_prompt

    def test_it_replaces_the_baseline_periods_even_on_a_fall(self, run):
        script = Script()
        out = run("full", script)
        assert not any("compute:period_history" in s["tools"] for s in out.steps)
        assert "## What is normal" not in script.answer_prompt

    def test_the_planner_gets_the_same_prompt_whatever_the_wording(self, run):
        """The comparison is offered on every why-question and chosen from the
        question itself: no wording adds to the prompt or decides the mode."""
        prompts = []
        for question in (WHY, "Why did revenue fall in 2024?"):
            script = Script()
            run("full", script, question=question)
            prompts.append(script.plan_prompt.split("## Question")[0])
            assert "how a change differs between groups" in script.plan_prompt
            assert "compares a change across groups" not in script.plan_prompt
        assert prompts[0].split("## ")[0] == prompts[1].split("## ")[0]

    def test_a_plan_without_groups_keeps_the_ordinary_guidance(self, run):
        single = {"metric": "revenue", "reason": "x",
                  "comparison": {"period_a": H1, "period_b": H2}}
        script = Script(plans=[single])
        run("full", script, question="Why did revenue fall in 2024?")
        assert "the group being explained" not in script.answer_prompt

    def test_the_driver_sentence_names_the_component_that_differs(self, run):
        out = run("full", Script(answer="Scripted without the component."))
        assert out.answer.startswith("Main driver (largest contributor): ")

    def test_c_and_a_are_not_changed(self, run):
        for arch in ("router", "baseline"):
            script = Script()
            run(arch, script)
            assert "metric_plan" not in script.calls
