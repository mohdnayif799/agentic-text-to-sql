"""Display rules for the Streamlit app (agentcrew/presentation.py).

Pure functions, so no page is rendered here: column headers and number
formats, the chart choice, step labels, the run summary line and the
plain-words reason a question escalated. tests/test_chat_ui.py checks that
app.py actually applies them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from agentcrew import presentation as p
from agentcrew.archs import ArchOutcome
from agentcrew.archs.explore import ACTIONS, COMPUTE_FUNCTIONS
from agentcrew.archs.outcome import RepairEvent
from agentcrew.compute.metrics import COLUMNS as METRIC_COLUMNS
from agentcrew.schemas import ExecutionResult

ROOT = Path(__file__).resolve().parents[1]


def frame(columns: list[str], rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def kinds(df: pd.DataFrame) -> dict[str, str]:
    return {c: f.kind for c, f in p.column_formats(df).items()}


# ---------------------------------------------------------------------------
# Column headers
# ---------------------------------------------------------------------------
class TestHumanize:
    @pytest.mark.parametrize(("name", "label"), [
        ("customer_id", "Customer ID"),
        ("completed_revenue", "Completed revenue"),
        ("total_completed_revenue_2024", "Total completed revenue 2024"),
        ("pct_change", "% change"),
        ("share_of_change_pct", "Share of change %"),
        ("value_a", "Value A"),
        ("avg_resolution_days", "Average resolution days"),
        ("q2_revenue", "Q2 revenue"),
        ("ym", "Year-month"),
        ("region", "Region"),
        ("COUNT(*)", "Count"),
        ("SUM(amount)", "Total amount"),
        ("AVG(unit_price)", "Average unit price"),
        ("count(DISTINCT customer_id)", "Count of distinct customer ID"),
    ])
    def test_snake_case_and_sql_names_become_plain_words(
        self, name: str, label: str
    ) -> None:
        assert p.humanize(name) == label


# ---------------------------------------------------------------------------
# Number formats
# ---------------------------------------------------------------------------
class TestColumnFormats:
    def test_the_screenshot_row(self) -> None:
        """The escalated fuzzy-search answer: an ID, a name and money."""
        formats = p.column_formats(frame(
            ["customer_id", "customer_name", "completed_revenue"],
            [[1133, "Idris Rossi", 33475.845]]))
        assert {c: (f.label, f.kind, f.number_format, f.align)
                for c, f in formats.items()} == {
            "customer_id": ("Customer ID", "identifier", "%d", "left"),
            "customer_name": ("Customer name", "text", None, None),
            "completed_revenue": ("Completed revenue", "money", "dollar", None),
        }

    @pytest.mark.parametrize("name", [
        "revenue", "amount", "marketing_spend", "unit_price", "unit_cost",
        "average_order_value", "gross_margin",
    ])
    def test_money_words_get_dollars(self, name: str) -> None:
        assert kinds(frame([name], [[1234.5], [10.0]])) == {name: "money"}

    @pytest.mark.parametrize(("name", "values", "kind"), [
        ("gross_margin_pct", [12.5, 30.1], "percent"),
        ("revenue_share", [0.25, 0.75], "fraction"),
        ("refund_rate", [16.29, 17.64], "percent"),
        ("churn_rate", [0.05, 0.12], "fraction"),
        ("pct_change", [-0.71, 0.2], "fraction"),
        ("pct_change", [-71.5, 20.0], "percent"),
    ])
    def test_rates_are_never_money_and_their_scale_comes_from_the_values(
        self, name: str, values: list[float], kind: str
    ) -> None:
        df = frame([name], [[v] for v in values])
        assert kinds(df) == {name: kind}
        expected = "percent" if kind == "fraction" else "%.2f%%"
        assert p.column_formats(df)[name].number_format == expected

    def test_counts_get_separators_and_other_numbers_two_decimals(self) -> None:
        formats = p.column_formats(frame(
            ["order_count", "avg_resolution_days"], [[12873, 3.4567]]))
        assert formats["order_count"].kind == "count"
        assert formats["order_count"].number_format == "%,d"
        assert formats["avg_resolution_days"].kind == "number"
        assert formats["avg_resolution_days"].number_format == "%,.2f"

    def test_ids_and_calendar_numbers_are_plain_left_aligned_integers(self) -> None:
        formats = p.column_formats(frame(["id", "product_id", "year"],
                                         [[7, 9, 2024]]))
        for name in ("id", "product_id", "year"):
            assert (formats[name].kind, formats[name].number_format,
                    formats[name].align) == ("identifier", "%d", "left")

    def test_text_and_flags_keep_their_default_rendering(self) -> None:
        formats = p.column_formats(frame(["segment", "largest_contributor"],
                                         [["SMB", True]]))
        assert [f.kind for f in formats.values()] == ["text", "text"]
        assert all(f.number_format is None for f in formats.values())

    def test_metric_analysis_values_mix_units_so_they_are_not_dollars(self) -> None:
        """value_a/value_b/change hold revenue, order counts and discount
        rates in one column (compute/metrics.py), so "value" in the name
        must not turn order counts into dollars."""
        rows = [["Q2", "Q3", "metric", "revenue", 2615311.92, 744611.11,
                 -1870700.82, -71.53, None, "lower", False],
                ["Q2", "Q3", "orders x average order value", "number of orders",
                 699.0, 238.0, -461.0, -65.95, 85.76, "lower", True]]
        found = kinds(frame(list(METRIC_COLUMNS), rows))
        assert found["value_a"] == found["value_b"] == found["change"] == "number"
        assert found["pct_change"] == found["share_of_change_pct"] == "percent"

    def test_metric_analysis_shows_items_and_numbers_first(self) -> None:
        """Its first columns are the compared periods, the same on every row;
        in the centered column they pushed the numbers out of view."""
        order = p.column_order(frame(list(METRIC_COLUMNS), []))
        assert sorted(order) == sorted(METRIC_COLUMNS)
        assert order[:4] == ["item", "value_a", "value_b", "change"]
        assert order[-2:] == ["side_a", "side_b"]
        assert p.column_order(frame(["region", "n"], [["APAC", 1]])) is None


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
class TestChartPlan:
    def test_an_id_is_never_the_charted_value(self) -> None:
        plan = p.chart_plan(frame(
            ["product_id", "product_name", "revenue"],
            [[9, "Lattice Ridge", 1308261.59], [1, "Cascade Beacon", 1168417.42]]))
        assert plan is not None
        assert (plan.label, plan.value, plan.value_kind) == (
            "product_name", "revenue", "money")
        assert plan.value_title == "Revenue"

    def test_no_chart_for_one_row(self) -> None:
        assert p.chart_plan(frame(
            ["customer_id", "customer_name", "completed_revenue"],
            [[1133, "Idris Rossi", 33475.845]])) is None

    def test_time_labels_get_a_line_categories_get_bars(self) -> None:
        months = frame(["month", "completed_orders"],
                       [["2025-03", 385], ["2025-01", 387], ["2025-02", 305]])
        regions = frame(["region_name", "refund_rate"],
                        [["APAC", 16.3], ["EMEA", 17.6], ["LATAM", 19.2]])
        assert p.chart_plan(months).kind == "line"
        assert p.chart_plan(regions).kind == "bar"

    def test_no_chart_when_labels_repeat(self) -> None:
        """A stacked metric table repeats side_a on every row; bars for it
        would pile up on one label."""
        df = frame(["side_a", "item", "value_a"],
                   [["Q2", "revenue", 10.0], ["Q2", "orders", 3.0]])
        assert p.chart_plan(df) is None

    def test_no_chart_without_a_label_or_a_value(self) -> None:
        assert p.chart_plan(frame(["year", "revenue"], [[2024, 1.0], [2025, 2.0]])) is None
        assert p.chart_plan(frame(["product_id", "product_name"],
                                  [[1, "a"], [2, "b"]])) is None

    def test_no_chart_for_long_results(self) -> None:
        df = frame(["label", "n"], [[f"item {i}", i] for i in range(26)])
        assert p.chart_plan(df) is None

    def test_bars_keep_the_standard_height_until_labels_would_crowd(self) -> None:
        few = frame(["label", "n"], [[f"item {i}", i] for i in range(12)])
        many = frame(["label", "n"], [[f"item {i}", i] for i in range(20)])
        assert p.chart_plan(few).height == p.CHART_HEIGHT == 260
        assert p.chart_plan(many).height > p.CHART_HEIGHT

    @pytest.mark.parametrize(("values", "is_time"), [
        (["2024", "2025"], True),
        (["2025-01", "2025-12"], True),
        (["2025-Q1", "2025-Q2"], True),
        (["2025-04-01", "2025-06-30"], True),
        (["2025-04-01 10:00:00", "2025-04-02 09:30:00"], True),
        (["2025-13", "2025-01"], False),
        (["APAC", "EMEA"], False),
        (["2025-04-01..2025-06-30"], False),
        (["2025-01", "APAC"], False),
    ])
    def test_time_labels(self, values: list[str], is_time: bool) -> None:
        assert p.is_time_labels(values) is is_time


# ---------------------------------------------------------------------------
# Steps, summary line and escalation reason
# ---------------------------------------------------------------------------
def repair(step: int, signal: str, accepted: bool = False) -> RepairEvent:
    before = ExecutionResult(ok=True, sql="SELECT 1")
    return RepairEvent(step=step, signal=signal, detail="", before=before,
                       after=None, accepted=accepted)


def outcome(path: str = "escalated", *, steps: tuple[dict, ...] = (),
            plan: tuple[str, ...] = (), repairs: tuple[RepairEvent, ...] = ()
            ) -> ArchOutcome:
    return ArchOutcome(arch="full", question="q", status="done", answer="a",
                       selected=None, path=path, plan=plan,
                       steps=steps or ({"step": 1, "tools": ["run_sql"]},),
                       repairs=repairs)


FUZZY_STEPS = (
    {"step": 1, "tools": ["run_sql", "repair"], "signal": "empty"},
    {"step": 2, "tools": ["search_values"]},
    {"step": 3, "tools": ["run_sql"]},
)


class TestStepLabels:
    def test_every_tool_in_the_code_has_plain_words(self) -> None:
        # A compute action is recorded as compute:<function>, never "compute".
        tools = (set(ACTIONS) - {"answer", "compute"}) | set(COMPUTE_FUNCTIONS)
        assert tools <= set(p.TOOL_LABELS)
        assert tools <= set(p.ACTION_WORDS)
        assert set(p.FINAL_TABLE) == {"stack", "assemble", "fallback"}

    @pytest.mark.parametrize(("step", "label"), [
        ({"step": 1, "tools": ["run_sql"]}, "Step 1: SQL query"),
        ({"step": 2, "tools": ["search_values"]}, "Step 2: Fuzzy value search"),
        ({"step": 2, "tools": ["get_distinct_values"]},
         "Step 2: Distinct values lookup"),
        ({"step": 2, "tools": ["sample_rows"]}, "Step 2: Sample rows"),
        ({"step": 2, "tools": ["compute:decompose_change"]},
         "Step 2: Metric decomposition"),
        ({"step": 3, "tools": ["compute:contribution_by_dimension"]},
         "Step 3: Contribution analysis"),
        ({"step": 3, "tools": ["compute:linear_trend_forecast"]},
         "Step 3: Trend forecast"),
        ({"step": 3, "tools": ["compute:zscore_anomalies"]}, "Step 3: Anomaly check"),
    ])
    def test_tool_ids_become_plain_words(self, step: dict, label: str) -> None:
        assert p.step_label(step) == label

    def test_a_repaired_step_counts_its_repairs(self) -> None:
        step = {"step": 1, "tools": ["run_sql", "repair"]}
        events = (repair(1, "empty"), repair(1, "empty"), repair(2, "error"))
        assert p.step_label(step, events) == "Step 1: SQL query, 2 repairs"
        assert p.step_label(step) == "Step 1: SQL query, 1 repair"


class TestSummaryLine:
    @pytest.mark.parametrize(("n", "word", "text"), [
        (0, "repair", "0 repairs"), (1, "step", "1 step"),
        (3, "step", "3 steps"), (1, "LLM call", "1 LLM call"),
        (6, "LLM call", "6 LLM calls"),
    ])
    def test_counts(self, n: int, word: str, text: str) -> None:
        assert p.count(n, word) == text

    def test_escalated_run(self) -> None:
        run = outcome(steps=(*FUZZY_STEPS, {"step": "final", "tools": ["assemble"]}),
                      repairs=(repair(1, "empty"), repair(1, "empty")))
        assert p.arch_summary(run, llm_calls=6, seconds=9.272) == (
            "Escalated · 3 steps · 2 repairs · 6 LLM calls · 9.3 s")

    def test_direct_run(self) -> None:
        assert p.arch_summary(outcome("direct"), llm_calls=3, seconds=3.136) == (
            "Direct · 1 step · 0 repairs · 3 LLM calls · 3.1 s")


class TestEscalationReason:
    def test_direct(self) -> None:
        assert p.escalation_reason(outcome("direct")) == (
            "One query was enough, so it answered directly.")

    def test_a_failed_check_names_the_check_and_what_followed(self) -> None:
        run = outcome(steps=FUZZY_STEPS,
                      repairs=(repair(1, "empty"), repair(1, "empty")))
        assert p.escalation_reason(run) == (
            "The first query returned no rows, so it searched for similar names "
            "and ran a follow-up query.")

    def test_a_forecast_question(self) -> None:
        run = outcome(
            plan=("The question asks for a forecast, a projection or a fitted trend "
                  "line: get the series it is fitted on (one row per period), fit "
                  "it from that series, and report each forecast with its period.",),
            steps=({"step": 1, "tools": ["run_sql"], "escalation_signal": "forecast"},
                   {"step": 2, "tools": ["run_sql"]},
                   {"step": 3, "tools": ["compute:linear_trend_forecast"]}))
        assert p.escalation_reason(run) == (
            "The question asks for a forecast or a fitted trend, so it ran a "
            "follow-up query and fitted a straight-line trend.")

    def test_the_follow_up_decision_is_quoted_then_what_followed(self) -> None:
        run = outcome(
            plan=("The query does not explain the cause of the drop.",),
            steps=({"step": 1, "tools": ["run_sql"]},
                   {"step": 2, "tools": ["compute:decompose_change"]},
                   {"step": 3, "tools": ["compute:contribution_by_dimension"]},
                   {"step": "final", "tools": ["stack"]}))
        assert p.escalation_reason(run) == (
            "The query does not explain the cause of the drop. It then split the "
            "change into its components and measured how much each group "
            "contributed.")

    def test_a_repair_alone(self) -> None:
        repaired = outcome(steps=({"step": 1, "tools": ["run_sql", "repair"]},),
                           repairs=(repair(1, "error", accepted=True),))
        tried = outcome(steps=({"step": 1, "tools": ["run_sql", "repair"]},),
                        repairs=(repair(1, "fanout"),))
        assert p.escalation_reason(repaired) == (
            "The first query failed, so it repaired the query.")
        assert p.escalation_reason(tried) == (
            "The first query's join would have counted rows more than once, so it "
            "tried to repair the query.")

    def test_every_signal_in_the_code_has_plain_words(self) -> None:
        """Signal kinds are string literals in checks.py and verify.py; a new
        one must get words here too."""
        checks = (ROOT / "agentcrew/archs/checks.py").read_text(encoding="utf-8")
        verify = (ROOT / "agentcrew/archs/verify.py").read_text(encoding="utf-8")
        found = set(re.findall(r'Signal\(\s*"(\w+)"', checks)) | set(
            re.findall(r'\("(\w+)",\s*(?:None if forecast else )?\w+_problem\(',
                       verify))
        assert len(found) >= 10  # the scan itself still finds them
        assert found <= set(p.SIGNAL_WORDS)


class TestMarkdown:
    def test_dollar_signs_are_not_read_as_math(self) -> None:
        text = "a residual of -$348,547.32, and -$425,182.00"
        assert p.escape_dollars(text) == (
            "a residual of -\\$348,547.32, and -\\$425,182.00")
