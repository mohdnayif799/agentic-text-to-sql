"""Baseline periods: a spike or dip question gets the periods before it as
evidence, so the answer can say what is normal. Offline: a scripted model and
the real database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.full import is_spike_question
from agentcrew.compute.functions import ComputeError
from agentcrew.compute.history import period_history, period_label, preceding_periods
from agentcrew.compute.metrics import Comparison, Period
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext, execute_readonly_sql


def period(start, end):
    return Period(start=start, end=end)


class TestPrecedingPeriods:
    def test_a_quarter_is_preceded_by_quarters_oldest_first(self):
        earlier = preceding_periods(period("2025-07-01", "2025-09-30"), 4)
        assert [(str(p.start), str(p.end)) for p in earlier] == [
            ("2024-07-01", "2024-09-30"), ("2024-10-01", "2024-12-31"),
            ("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30")]

    def test_a_month_is_preceded_by_months_of_their_own_length(self):
        earlier = preceding_periods(period("2025-03-01", "2025-03-31"), 2)
        assert [(str(p.start), str(p.end)) for p in earlier] == [
            ("2025-01-01", "2025-01-31"), ("2025-02-01", "2025-02-28")]

    def test_a_year_is_preceded_by_years(self):
        earlier = preceding_periods(period("2025-01-01", "2025-12-31"), 1)
        assert [(str(p.start), str(p.end)) for p in earlier] == [
            ("2024-01-01", "2024-12-31")]

    def test_a_span_that_is_not_whole_months_keeps_its_length_in_days(self):
        earlier = preceding_periods(period("2025-03-10", "2025-03-19"), 1)
        assert [(str(p.start), str(p.end)) for p in earlier] == [
            ("2025-02-28", "2025-03-09")]


@pytest.mark.parametrize(("start", "end", "label"), [
    ("2025-07-01", "2025-09-30", "2025-Q3"),
    ("2025-03-01", "2025-03-31", "2025-03"),
    ("2025-01-01", "2025-12-31", "2025"),
    ("2025-02-01", "2025-03-31", "2025-02-01..2025-03-31"),
])
def test_period_labels(start, end, label):
    assert period_label(period(start, end)) == label


@pytest.fixture
def run_sql(engine):
    tools = ToolContext.create(engine)
    return lambda sql: execute_readonly_sql(tools, sql)


Q2_2025 = period("2025-04-01", "2025-06-30")


def tickets_by_region(run_sql, start, end):
    rows = run_sql(
        "SELECT r.region_name, COUNT(*) FROM support_tickets t "
        "JOIN customers c ON c.customer_id = t.customer_id "
        "JOIN regions r ON r.region_id = c.region_id "
        f"WHERE t.created_date BETWEEN '{start}' AND '{end}' GROUP BY 1").rows
    return {name: n for name, n in rows}


class TestPeriodHistory:
    def test_values_match_the_data_and_the_range_is_of_earlier_periods(self, run_sql):
        df, queries = period_history(run_sql, "tickets", Q2_2025, "region")
        assert list(df.columns) == ["region", "2024-Q2", "2024-Q3", "2024-Q4",
                                    "2025-Q1", "earlier_low", "earlier_high", "2025-Q2"]
        assert len(queries) == 5
        now = tickets_by_region(run_sql, "2025-04-01", "2025-06-30")
        before = tickets_by_region(run_sql, "2025-01-01", "2025-03-31")
        row = df[df["region"] == "EMEA"].iloc[0]
        assert row["2025-Q2"] == now["EMEA"] and row["2025-Q1"] == before["EMEA"]
        earlier = row[["2024-Q2", "2024-Q3", "2024-Q4", "2025-Q1"]]
        assert row["earlier_low"] == earlier.min()
        assert row["earlier_high"] == earlier.max()

    def test_largest_first_with_a_total_row_last(self, run_sql):
        df, _ = period_history(run_sql, "tickets", Q2_2025, "region")
        items = df[df["region"] != "total"]["2025-Q2"].tolist()
        assert items == sorted(items, reverse=True)
        assert df.iloc[-1]["region"] == "total"
        assert df.iloc[-1]["2025-Q2"] == sum(items)

    def test_a_ratio_has_no_total_row(self, run_sql):
        df, _ = period_history(run_sql, "average_order_value", Q2_2025, "region")
        assert "total" not in df["region"].tolist()

    def test_without_a_dimension_it_is_one_row_for_the_metric(self, run_sql):
        df, _ = period_history(run_sql, "tickets", Q2_2025)
        assert df.shape[0] == 1 and df.iloc[0]["metric"] == "support tickets"

    def test_filters_apply_to_every_period(self, run_sql):
        all_regions, _ = period_history(run_sql, "tickets", Q2_2025)
        emea, _ = period_history(run_sql, "tickets", Q2_2025, filters={"region": "EMEA"})
        assert emea.iloc[0]["2025-Q2"] < all_regions.iloc[0]["2025-Q2"]

    def test_periods_before_the_data_starts_are_left_out(self, run_sql):
        start = run_sql("SELECT MIN(created_date) FROM support_tickets").rows[0][0]
        year = int(start[:4])
        df, _ = period_history(
            run_sql, "tickets", period(f"{year}-07-01", f"{year}-09-30"), "region")
        assert not any(c.startswith(str(year - 1)) for c in df.columns)

    def test_no_earlier_data_at_all_is_refused(self, run_sql):
        with pytest.raises(ComputeError, match="no earlier data"):
            period_history(run_sql, "tickets", period("1999-01-01", "1999-03-31"))

    def test_a_metric_that_cannot_compare_periods_is_refused(self, run_sql):
        with pytest.raises(ComputeError, match="cannot be split by period"):
            period_history(run_sql, "churn_rate", Q2_2025, "region")

    def test_many_values_are_capped_but_the_total_covers_all(self, run_sql):
        df, _ = period_history(run_sql, "revenue", Q2_2025, "product")
        full = run_sql(
            "SELECT SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = "
            "'completed' AND o.order_date BETWEEN '2025-04-01' AND '2025-06-30'"
        ).rows[0][0]
        assert df.shape[0] == 9
        assert df.iloc[-1]["2025-Q2"] == pytest.approx(full)

    def test_a_filter_value_is_a_literal_not_sql(self, run_sql):
        with pytest.raises(ComputeError, match="no earlier data"):
            period_history(run_sql, "tickets", Q2_2025,
                           filters={"region": "x' OR '1'='1"})

    def test_a_comparison_gives_the_explained_period(self):
        comparison = Comparison.model_validate(
            {"period_a": {"start": "2025-04-01", "end": "2025-06-30"},
             "period_b": {"start": "2025-07-01", "end": "2025-09-30"}})
        assert period_label(comparison.explained_period) == "2025-Q3"
        grouped = Comparison.model_validate(
            {"group_dimension": "region", "groups": ["APAC", "EMEA"],
             "period": {"start": "2025-01-01", "end": "2025-12-31"}})
        assert period_label(grouped.explained_period) == "2025"
        no_period = Comparison.model_validate(
            {"group_dimension": "region", "groups": ["APAC", "EMEA"]})
        assert no_period.explained_period is None


class TestSpikeQuestions:
    @pytest.mark.parametrize("question", [
        "Why did tickets spike in March 2025?",
        "Why did signups surge last quarter?",
        "Why did refunds jump in 2024?",
        "Why was there a dip in orders in May?",
        "Why did margin drop so sharply?",
        "Why did units plunge from Q1 to Q2?",
        "What caused the slump in sales?",
        "Why did revenue fall in Q4?",
        "Why has spend declined this year?",
    ])
    def test_sudden_changes(self, question):
        assert is_spike_question(question)

    @pytest.mark.parametrize("question", [
        "Why does Online bring in more revenue than Direct?",
        "How did tickets develop in 2025?",
        "Why is EMEA our largest region?",
    ])
    def test_other_questions(self, question):
        assert not is_spike_question(question)


COUNT = "SELECT COUNT(*) AS n FROM support_tickets"
Q1 = {"start": "2025-01-01", "end": "2025-03-31"}
Q2 = {"start": "2025-04-01", "end": "2025-06-30"}
PLAN = {"metric": "tickets", "dimension": "region", "reason": "by region",
        "comparison": {"period_a": Q1, "period_b": Q2}}
SPIKE = "Why did support tickets spike in Q2 2025?"


class Script:
    def __init__(self, plans=(PLAN,), actions=()):
        self.plans, self.actions = list(plans), list(actions)
        self.calls: list[str] = []
        self.answer_prompt = ""

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": True, "reason": "explain the cause"}
        if "You start the analysis of a question that asks why" in system:
            self.calls.append("metric_plan")
            return self.plans.pop(0) if self.plans else {}
        if "continuing an analysis" in system:
            self.calls.append("followup")
            return {"done": True, "sql": "", "reason": ""}
        if "You plan the next step" in system:
            self.calls.append("decide")
            return self.actions.pop(0) if self.actions else {"action": "answer"}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            return {"base_step": 1, "columns": []}
        if "final answer" in system:
            self.calls.append("answer")
            self.answer_prompt = user
            return {"answer": "APAC drove it.", "key_numbers": [], "caveats": []}
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

    def go(arch, script, question=SPIKE, **overrides):
        cfg = pinned.model_copy(update=overrides) if overrides else pinned
        return run_architecture(arch, question, settings=cfg,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


class TestInTheRun:
    def test_the_history_is_one_step_and_no_llm_call(self, run):
        script = Script()
        out = run("full", script)
        assert script.calls == ["step0", "decision", "metric_plan", "answer"]
        steps = [s for s in out.steps if s["step"] != "final"]
        assert [s["tools"][0] for s in steps] == [
            "run_sql", "compute:contribution_by_dimension", "compute:period_history"]

    def test_the_answer_sees_what_is_normal(self, run):
        script = Script()
        run("full", script)
        guidance = script.answer_prompt.split("## What is normal", 1)[1]
        assert "earlier_low" in guidance and "2025-Q2" in guidance
        assert "total" in guidance

    def test_the_final_table_stays_the_metric_analysis(self, run):
        out = run("full", Script())
        assert "side_a" in out.selected.columns
        final = next(s for s in out.steps if s["step"] == "final")
        assert final["plan"] == {"stacked_steps": [2]}

    def test_a_why_question_that_is_no_spike_gets_no_history(self, run):
        script = Script(plans=[PLAN])
        out = run("full", script, question="Why are there so many tickets in Q2 2025?")
        assert not any("compute:period_history" in s["tools"] for s in out.steps)
        assert "## What is normal" not in script.answer_prompt

    def test_the_step_budget_holds(self, run):
        script = Script()
        out = run("full", script, max_steps=2)
        assert not any("compute:period_history" in s["tools"] for s in out.steps)
        assert out.status == "done"

    def test_no_history_when_the_analysis_failed(self, run):
        no_data = {**PLAN, "comparison": {
            "period_a": Q1, "period_b": Q2, "filters": {"region": "Atlantis"}}}
        script = Script(plans=[no_data])
        out = run("full", script)
        assert not any("compute:period_history" in s["tools"] for s in out.steps)
        assert "## What is normal" not in script.answer_prompt

    def test_a_dimension_the_source_lacks_falls_back_to_the_metric(self, run):
        decomposed = {"metric": "revenue", "dimension": "severity", "reason": "x",
                      "comparison": {"period_a": Q1, "period_b": Q2}}
        script = Script(plans=[decomposed])
        out = run("full", script, question="Why did revenue drop in Q2 2025?")
        assert any("compute:period_history" in s["tools"] for s in out.steps)
        assert "metric" in script.answer_prompt.split("## What is normal", 1)[1]

    def test_c_and_a_have_no_history_step(self, run):
        for arch in ("router", "baseline"):
            out = run(arch, Script())
            assert not any("compute:period_history" in s.get("tools", [])
                           for s in out.steps)
