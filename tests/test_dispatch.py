"""Every tool of architecture D through D's own dispatcher (Full._act).

The arguments are model-style: the JSON the live planner emits (in a code
fence, with explicit nulls and extra fields), slightly wrong forms (wrong
case, a single string where a list is expected), and bad values. A bad
argument must come back as a failed step (an observation the planner can
read), never as an exception. Offline; the database is the real one.

Live coverage so far (eval/results/phase4_report.md): run_sql, search_values
and decompose_change have run with model-chosen arguments; the tools below
marked "offline only" have not.
"""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.explore import NextAction
from agentcrew.archs.full import Full
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient, extract_json
from agentcrew.tools import ToolContext

SERIES = ("SELECT substr(signup_date, 1, 7) AS month, COUNT(*) AS signups "
          "FROM customers WHERE signup_date BETWEEN '2023-01-01' AND '2024-12-31' "
          "GROUP BY month ORDER BY month")
CHANNELS = "SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel"
MONTHLY = ("SELECT substr(o.order_date, 1, 7) AS month, COUNT(*) AS orders, "
           "SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS revenue "
           "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
           "WHERE o.status = 'completed' GROUP BY month")
H1_H2 = {"period_a": {"start": "2024-01-01", "end": "2024-06-30"},
         "period_b": {"start": "2024-07-01", "end": "2024-12-31"}}


@pytest.fixture
def setup(engine, settings):
    settings = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))
    question = "How did signups develop?"
    full = Full(question, llm=FakeClient(), tools=tools,
                context=builder.build(question), settings=settings)
    steps = [full.run_step(1, SERIES), full.run_step(2, CHANNELS),
             full.run_step(3, MONTHLY)]
    assert all(s.result.ok and s.result.rows for s in steps)
    return full, steps


def act(setup, reply: str):
    """Parse a planner reply as the structured-output path does, then run it."""
    full, steps = setup
    action = NextAction.model_validate_json(extract_json(reply))
    return full._act(len(steps) + 1, action, steps)


def ok(setup, reply: str):
    step = act(setup, reply)
    assert step.result.ok, step.result.error_message
    return step.result


FENCED = ('```json\n{"action": "get_distinct_values", "table": "Orders", '
          '"column": "STATUS", "reason": "check the labels", "sql": null, '
          '"term": null, "function": null, "params": {}, "confidence": 0.9}\n```')


class TestLookups:
    def test_get_distinct_values_offline_only(self, setup):
        result = ok(setup, FENCED)  # fenced, nulls, extra field, wrong case
        assert [r[0] for r in result.rows] == ["cancelled", "completed", "refunded"]

    def test_search_values_with_wrong_case_filters(self, setup):
        result = ok(setup, '{"action": "search_values", "term": "idirs rosis", '
                           '"table": "Customers", "column": "Customer_Name"}')
        assert result.rows[0][2] == "Idris Rossi"

    def test_sample_rows_offline_only(self, setup):
        assert ok(setup, '{"action": "sample_rows", "table": "PRODUCTS"}').rows

    def test_run_sql_with_a_trailing_semicolon(self, setup):
        result = ok(setup, '{"action": "run_sql", "sql": "SELECT channel, '
                           'COUNT(*) AS n FROM orders GROUP BY channel;"}')
        assert result.row_count == 3


def compute(function: str, params: str) -> str:
    return (f'{{"action": "compute", "function": "{function}", '
            f'"params": {params}, "table": null, "sql": null}}')


class TestComputeFunctions:
    """Offline only: none of these has run live with model-chosen arguments."""

    def test_pct_change(self, setup):
        result = ok(setup, compute("pct_change",
                                   '{"step": 1, "value": "Signups", "order": "MONTH"}'))
        assert result.row_count == 24

    def test_yoy_growth(self, setup):
        result = ok(setup, compute("yoy_growth",
                                   '{"step": 1, "period": "month", "value": "signups"}'))
        assert result.row_count == 12  # the 2024 months, which have a prior year

    def test_compare_periods(self, setup):
        result = ok(setup, compute("compare_periods", '{"step": 1, "period": "month", '
                                   '"value": "signups", "period_a": "2023-06", '
                                   '"period_b": "2024-06"}'))
        assert result.rows

    def test_share_of_total(self, setup):
        result = ok(setup, compute("share_of_total",
                                   '{"step": 2, "value": "N", "label": "Channel"}'))
        shares = [r[-1] for r in result.rows]
        assert sum(shares) == pytest.approx(100.0)

    def test_zscore_anomalies_against_the_trend(self, setup):
        result = ok(setup, compute("zscore_anomalies", '{"step": 1, "value": "signups", '
                                   '"label": "month", "threshold": 2, '
                                   '"direction": "Both", "against": "TREND"}'))
        assert "x_convention" in result.columns

    def test_linear_trend_forecast_named_period(self, setup):
        result = ok(setup, compute("linear_trend_forecast", '{"step": 1, '
                                   '"value": "signups", "order": "month", '
                                   '"periods": "2025-03"}'))
        assert result.rows[0][0] == "2025-03" and result.row_count == 1

    def test_correlation(self, setup):
        result = ok(setup, compute("correlation",
                                   '{"step": 3, "a": "ORDERS", "b": "Revenue"}'))
        assert -1 <= result.rows[0][0] <= 1

    def test_decompose_change_with_wrong_case_names(self, setup):
        result = ok(setup, compute("decompose_change", '{"metric": "Revenue", '
                                   '"comparison": {"group_dimension": "Channel", '
                                   '"groups": ["Online", "Direct"]}}'))
        assert result.rows

    def test_contribution_by_dimension_offline_only(self, setup):
        result = ok(setup, compute("contribution_by_dimension",
                                   '{"metric": "revenue", "dimension": "REGION", '
                                   f'"comparison": {__import__("json").dumps(H1_H2)}}}'))
        assert result.row_count == 4


@pytest.mark.parametrize(("reply", "message"), [
    (compute("pct_change", '{"step": 9, "value": "signups"}'), "params.step"),
    (compute("pct_change", '{"step": "one", "value": "signups"}'), "params.step"),
    (compute("pct_change", '{"step": 1, "value": "signups", "smoothing": 3}'),
     "unknown params"),
    (compute("pct_change", '{"step": 1, "value": "month"}'), "non-numeric"),
    (compute("pct_change", '{"step": 1, "value": ["signups"]}'), "no column"),
    (compute("zscore_anomalies", '{"step": 1, "value": "signups", "label": "month", '
             '"direction": "sideways"}'), "direction"),
    (compute("linear_trend_forecast", '{"step": 1, "value": "signups", '
             '"order": "month", "periods": ["2023-05"]}'), "not one of"),
    (compute("linear_trend_forecast", '{"step": 1, "value": "signups", '
             '"order": "month", "periods_ahead": 99}'), "between 1 and 24"),
    (compute("decompose_change", '{"metric": "profit", "comparison": '
             '{"group_dimension": "channel", "groups": ["Online", "Direct"]}}'),
     "unknown metric"),
    (compute("decompose_change", '{"metric": "revenue", "comparison": '
             '{"group_dimension": "colour", "groups": ["Online", "Direct"]}}'),
     "not available"),
    (compute("decompose_change", '{"metric": "revenue", "comparison": '
             '{"group_dimension": "channel", "groups": ["online", "Direct"]}}'),
     "no data"),  # data values are case-sensitive: a clean miss, not a guess
    (compute("contribution_by_dimension", '{"metric": "revenue", "comparison": {}}'),
     "period_a"),
    ('{"action": "get_distinct_values", "table": "invoices", "column": "id"}',
     "no table"),
    ('{"action": "get_distinct_values", "table": "orders", "column": "colour"}',
     "no column"),
    ('{"action": "sample_rows", "table": "orders; DROP TABLE orders"}', "no table"),
])
def test_bad_arguments_fail_cleanly(setup, reply, message):
    step = act(setup, reply)
    assert not step.result.ok
    assert message in step.result.error_message


@pytest.mark.parametrize("reply", [
    '{"action": "compute"}',                       # no function
    '{"action": "run_sql"}',                       # no sql
    '{"action": "delete_rows", "table": "orders"}',  # not an action
    '{"action": "compute", "function": "median"}',   # not a function
    '{"action": "compute", "function": "pct_change", "params": "step=1"}',
])
def test_invalid_replies_are_rejected_before_dispatch(reply):
    """These raise at validation; the structured-output path then asks the
    model once to fix its reply (llm.complete_structured)."""
    with pytest.raises(ValueError):
        NextAction.model_validate_json(reply)


def test_conditional_branch_path_offline_only(settings, engine):
    """A conditional question: step 1 evaluates the condition, the planner
    runs the branch query, and the branch table is the final result."""
    growth = ("SELECT channel, SUM(CASE WHEN order_date LIKE '2025%' THEN 1 END) * 1.0 "
              "/ SUM(CASE WHEN order_date LIKE '2024%' THEN 1 END) AS growth "
              "FROM orders GROUP BY channel")
    branch = ("SELECT c.segment, COUNT(*) AS orders_2025 FROM orders o JOIN customers c "
              "ON c.customer_id = o.customer_id WHERE o.channel = 'Partner' "
              "AND o.order_date LIKE '2025%' GROUP BY c.segment")

    def respond(system, user):
        if "SQL analyst" in system:
            return {"sql": growth, "rationale": "x", "expected_shape": ""}
        if "You check whether one query result" in system:
            return {"needs_followup": True, "reason": "the branch query is missing"}
        if "You plan the next step" in system:
            return ({"action": "run_sql", "sql": branch, "reason": "Partner branch"}
                    if "Step 2" not in user else {"action": "answer"})
        if "Several queries were run" in system:
            return {"base_step": 2, "columns": []}
        return {"answer": "Partner grew faster; by segment ...", "key_numbers": [],
                "caveats": []}

    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))
    out = run_architecture(
        "full", "Did Partner orders grow faster than Online orders from 2024 to 2025? "
        "If yes, break Partner's 2025 orders down by segment.",
        settings=settings.model_copy(update={"today": date(2026, 10, 4)}),
        llm=FakeClient(responder=respond), tools=tools, context_builder=builder)
    assert [s["tools"] for s in out.steps] == [["run_sql"], ["run_sql"], ["assemble"]]
    assert out.selected_step == 2 and out.selected.columns == ["segment", "orders_2025"]
