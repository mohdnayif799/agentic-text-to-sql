"""Verification checks (archs/verify.py) and exploration tools (archs/explore.py)."""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from agentcrew.archs.checks import schema_of
from agentcrew.archs.explore import NextAction, compute, lookup, value_index
from agentcrew.archs.verify import (
    date_format_problem,
    date_range_problem,
    percent_problem,
    question_periods,
    reconcile_problem,
    verify,
)
from agentcrew.context.values import profile_columns
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext, execute_readonly_sql
from evalkit.questions import SET_FILES, load_set


def table(columns, rows, sql="SELECT 1"):
    return ExecutionResult(ok=True, sql=sql, columns=columns, rows=rows,
                           row_count=len(rows))


@pytest.fixture(scope="module")
def tools(request):
    from agentcrew.db.engine import read_only_engine

    return ToolContext.create(read_only_engine(request.getfixturevalue("db_path")))


@pytest.fixture(scope="module")
def formats(tools):
    return {(d.table, d.column): d.fmt
            for d in profile_columns(tools.engine, tools.catalog).dates}


class TestReconcile:
    def test_average_that_does_not_match_total_over_count(self):
        bad = table(["channel", "total_revenue", "completed_order_count",
                     "avg_order_value"], [["Online", 10195563.46, 3458, 1189.96]])
        assert "should equal" in reconcile_problem(bad)

    def test_consistent_values_pass(self):
        good = table(["total_revenue", "order_count", "avg_order_value"],
                     [[10195563.46, 3458, 2948.40]])
        assert reconcile_problem(good) is None

    def test_unrelated_averages_are_not_checked(self):
        # a unit price times units is not revenue (discounts): not a triple
        assert reconcile_problem(table(["revenue", "units", "avg_unit_price"],
                                       [[95.0, 10, 10.0]])) is None

    def test_qualifiers_must_agree(self):
        mixed = table(["q2_revenue", "q3_orders", "q3_avg_order_value"],
                      [[100.0, 2, 10.0]])
        assert reconcile_problem(mixed) is None


class TestPercentAndDates:
    def test_percent_range(self):
        assert percent_problem(table(["share_pct"], [[120.0]]))
        assert percent_problem(table(["share_pct"], [[45.0]])) is None
        assert percent_problem(table(["pct_change"], [[-60.0]])) is None

    def test_month_column_against_day_dates(self, tools, formats):
        schema = schema_of(tools.catalog)
        sql = ("SELECT SUM(amount) FROM marketing_spend "
               "WHERE month BETWEEN '2025-07-01' AND '2025-09-30'")
        assert "Date format mismatch" in date_format_problem(sql, schema, formats)
        ok = ("SELECT SUM(amount) FROM marketing_spend "
              "WHERE month BETWEEN '2025-07' AND '2025-09'")
        assert date_format_problem(ok, schema, formats) is None

    def test_question_periods(self):
        spans = question_periods("Compare Q2 2025 with July 2025 and 2024")
        assert (date(2025, 4, 1), date(2025, 6, 30)) in spans
        assert (date(2025, 7, 1), date(2025, 7, 31)) in spans
        assert (date(2024, 1, 1), date(2024, 12, 31)) in spans

    def test_forecast_questions_skip_the_date_range_check(self, tools, formats):
        from agentcrew.archs.checks import schema_of

        sql = ("SELECT substr(order_date, 1, 7) AS m, COUNT(*) AS n FROM orders "
               "WHERE order_date BETWEEN '2024-01-01' AND '2025-12-31' GROUP BY m")
        question = "Forecast monthly orders for 2026."
        result = execute_readonly_sql(tools, sql)
        schema = schema_of(tools.catalog)
        assert verify(sql, result, question, schema, formats).kind == "date_range"
        assert verify(sql, result, question, schema, formats, forecast=True) is None

    def test_date_range(self):
        sql = "SELECT 1 FROM orders WHERE order_date LIKE '2024%'"
        assert date_range_problem(sql, "Revenue in 2025?")
        assert date_range_problem(sql, "Revenue in 2024?") is None
        assert date_range_problem(sql, "Total revenue?") is None


def test_no_false_alarms_on_gold_evidence_or_reference_queries(tools, formats):
    schema = schema_of(tools.catalog)
    cases = []
    for name in SET_FILES:
        for q in load_set(name):
            specs = [q.get("gold")] + [i["gold"] for i in q.get("interpretations", [])]
            cases += [(q["question"], s["sql"]) for s in specs if s and "sql" in s]
            cases += [(q["question"], sql) for sql in (q.get("evidence") or {}).values()]
    for path in ("eval/questions.yaml", "eval/questions_diagnostic.yaml",
                 "eval/questions_holdout.yaml", "eval/heldout.yaml"):
        with open(path, encoding="utf-8") as handle:
            cases += [(q["question"], q["reference_sql"])
                      for q in yaml.safe_load(handle) if q.get("reference_sql")]
    flagged = [(question, sql) for question, sql in cases
               if verify(sql, execute_readonly_sql(tools, sql), question, schema,
                         formats)]
    assert flagged == []


class TestExplore:
    def test_next_action_requires_its_fields(self):
        with pytest.raises(ValueError):
            NextAction(action="run_sql")
        with pytest.raises(ValueError):
            NextAction(action="compute")
        assert NextAction(action="answer").action == "answer"

    def test_search_values_matches_misspellings(self, tools):
        hits = value_index(tools).search("Cobolt Nimbus")
        assert hits[0][:3] == ["products", "product_name", "Cobalt Nimbus"]

    def test_lookups_validate_names(self, tools):
        bad = lookup(NextAction(action="sample_rows", table="nope"), tools)
        assert not bad.ok and "no table" in bad.error_message
        values = lookup(NextAction(action="get_distinct_values", table="orders",
                                   column="status"), tools)
        assert [r[0] for r in values.rows] == ["cancelled", "completed", "refunded"]

    def test_compute_on_an_earlier_step(self, tools):
        step = table(["channel", "revenue"], [["a", 30.0], ["b", 10.0]])
        act = NextAction(action="compute", function="share_of_total",
                         params={"step": 1, "value": "revenue", "label": "channel"})
        result, _ = compute(act, {1: step}, lambda sql: execute_readonly_sql(tools, sql))
        assert result.ok and result.rows[0][-1] == pytest.approx(75.0)

    def test_compute_rejects_unknown_params(self, tools):
        act = NextAction(action="compute", function="share_of_total",
                         params={"step": 1, "value": "revenue", "label": "channel",
                                 "colour": "red"})
        result, _ = compute(act, {1: table(["channel", "revenue"], [["a", 1.0]])},
                            lambda sql: execute_readonly_sql(tools, sql))
        assert not result.ok and "unknown params" in result.error_message
