"""Four fixes from the 50-question evaluation, offline on scripted models:
a LEFT JOIN filter that restricts nothing (hv08), fuzzy name search (hv04), a write
statement ending the run (sw06), and a figure the data cannot hold (sa01)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from test_full import COUNT, Script, run_d

from agentcrew.archs import run_architecture
from agentcrew.archs.explore import MAX_TIED_MATCHES, ValueIndex, value_index
from agentcrew.archs.sqlfacts import join_filter_problem, reads_no_table, text_filters
from agentcrew.context import ContextBuilder
from agentcrew.db.safety import UnsafeSQLError, guard_sql, is_write_sql
from agentcrew.llm import FakeClient
from agentcrew.refusal import WRITE_REFUSAL, is_write_request
from agentcrew.tools import ToolContext

SCHEMA = {
    "orders": {"order_id": "INTEGER", "status": "TEXT", "customer_id": "INTEGER"},
    "order_items": {"order_id": "INTEGER", "product_id": "INTEGER",
                    "quantity": "INTEGER"},
    "products": {"product_id": "INTEGER", "category": "TEXT"},
    "customers": {"customer_id": "INTEGER", "segment": "TEXT"},
}
#: The holdout failure: the category filter on p restricts nothing.
HV08_SERIES = (
    "WITH quarters AS (SELECT '2024-Q1' AS q_label, '2024-01-01' AS start_date, "
    "'2024-03-31' AS end_date UNION ALL SELECT '2024-Q2', '2024-04-01', '2024-06-30' "
    "UNION ALL SELECT '2024-Q3', '2024-07-01', '2024-09-30' "
    "UNION ALL SELECT '2024-Q4', '2024-10-01', '2024-12-31' "
    "UNION ALL SELECT '2025-Q1', '2025-01-01', '2025-03-31' "
    "UNION ALL SELECT '2025-Q2', '2025-04-01', '2025-06-30' "
    "UNION ALL SELECT '2025-Q3', '2025-07-01', '2025-09-30' "
    "UNION ALL SELECT '2025-Q4', '2025-10-01', '2025-12-31') "
    "SELECT q.q_label AS period, SUM(oi.quantity) AS units_sold FROM quarters AS q "
    "LEFT JOIN orders AS o ON o.order_date >= q.start_date "
    "AND o.order_date <= q.end_date AND o.status = 'completed' "
    "LEFT JOIN order_items AS oi ON oi.order_id = o.order_id "
    "LEFT JOIN products AS p ON p.product_id = oi.product_id "
    "AND p.category = 'Accessories' "
    "GROUP BY q.q_label ORDER BY q.q_label"
)
FIXED_SERIES = (
    "SELECT substr(o.order_date, 1, 4) || '-Q' || "
    "((CAST(substr(o.order_date, 6, 2) AS INTEGER) + 2) / 3) AS period, "
    "SUM(oi.quantity) AS units_sold FROM orders AS o "
    "JOIN order_items AS oi ON oi.order_id = o.order_id "
    "JOIN products AS p ON p.product_id = oi.product_id "
    "WHERE o.status = 'completed' AND p.category = 'Accessories' "
    "GROUP BY period ORDER BY period"
)
FORECAST_Q = ("Fit a least-squares line to Accessories units per quarter over 2024 and "
              "2025. What does it project for Q1 2026?")
TREND_PLAN = {"series_sql": HV08_SERIES, "period": "period", "value": "units_sold",
              "periods": ["2026-Q1"], "reason": "fit the series"}
SW06 = "We no longer need the LATAM customers, so please get them out of the database."
SW06_SQL = ("DELETE FROM customers WHERE region_id IN "
            "(SELECT region_id FROM regions WHERE region_name = 'LATAM')")
NAME_Q = "How much revenue did Nadia Popesku bring in during 2025?"
NAME_SQL = ("SELECT c.customer_id, COUNT(o.order_id) AS n FROM customers c "
            "JOIN orders o ON o.customer_id = c.customer_id "
            "WHERE c.customer_name = '{}' GROUP BY c.customer_id")


@pytest.fixture
def pinned(settings):
    return settings.model_copy(update={"today": date(2026, 10, 4)})


@pytest.fixture
def builder(engine):
    tools = ToolContext.create(engine)
    return ContextBuilder.from_database(engine, tools.catalog, today=date(2026, 10, 4))


def base_join(on_extra: str, *, select: str = "SUM(oi.quantity) AS u",
              tail: str = "") -> str:
    return (f"SELECT {select} FROM order_items oi "
            f"LEFT JOIN products p ON p.product_id = oi.product_id{on_extra}{tail}")


class TestJoinFilter:
    def test_the_holdout_query_is_caught(self):
        problem = join_filter_problem(HV08_SERIES, SCHEMA)
        assert problem and "p.category = 'Accessories'" in problem
        assert "restricts no rows" in problem

    def test_a_filter_on_a_table_that_later_joins_depend_on_is_real(self):
        # o's filter decides which order lines survive, through oi's join condition.
        sql = ("SELECT SUM(oi.quantity) FROM customers c "
               "LEFT JOIN orders o ON o.customer_id = c.customer_id AND o.status = 'done' "
               "LEFT JOIN order_items oi ON oi.order_id = o.order_id")
        assert join_filter_problem(sql, SCHEMA) is None

    @pytest.mark.parametrize("sql", [
        "SELECT SUM(oi.quantity) FROM order_items oi JOIN products p "
        "ON p.product_id = oi.product_id AND p.category = 'A'",
        "SELECT SUM(oi.quantity) FROM order_items oi LEFT JOIN products p "
        "ON p.product_id = oi.product_id WHERE p.category = 'A'",
        base_join(" AND p.category = 'A'",
                  select="SUM(CASE WHEN p.product_id IS NOT NULL "
                         "THEN oi.quantity END) AS u"),
        base_join(" AND p.category = 'A'", select="p.category, SUM(oi.quantity) AS u",
                  tail=" GROUP BY p.category"),
        base_join(" AND p.category = 'A'", tail=" WHERE p.product_id IS NOT NULL"),
        base_join(""),
    ], ids=["inner", "where", "read-in-case", "read-in-select", "read-in-where",
            "no-filter"])
    def test_a_filter_that_does_restrict_is_left_alone(self, sql):
        assert join_filter_problem(sql, SCHEMA) is None

    def test_an_unqualified_filter_column_is_resolved(self):
        sql = base_join(" AND category = 'A'")
        assert join_filter_problem(sql, SCHEMA) is not None

    def test_sql_that_does_not_parse_is_for_the_database_to_report(self):
        assert join_filter_problem("SELECT FROM WHERE (", SCHEMA) is None


class TestForecastPath:
    def test_the_series_is_repaired_before_it_is_fitted(self, pinned, engine, builder):
        script = Script(needs_followup=True, trend_plans=[TREND_PLAN],
                        repairs=[FIXED_SERIES])
        out, _ = run_d(script, pinned, engine, builder, question=FORECAST_Q)
        assert "repair" in script.calls
        assert "restricts no rows" in script.prompts["repair"]
        forecast = out.selected.columns.index("forecast")
        assert out.selected.rows[0][forecast] == pytest.approx(2642.607, abs=0.01)

    def test_a_series_known_to_be_wrong_is_never_fitted(self, pinned, engine, builder):
        script = Script(needs_followup=True, trend_plans=[TREND_PLAN],
                        repairs=[HV08_SERIES, HV08_SERIES])
        out, _ = run_d(script, pinned, engine, builder, question=FORECAST_Q)
        assert not any(t.startswith("compute:") for s in out.steps
                       for t in s.get("tools", []))
        assert out.selected.rows == [[1400]]  # step 1, not the wrong series

    def test_the_planner_cannot_fit_it_either(self, pinned, engine, builder):
        fit = {"action": "compute", "function": "linear_trend_forecast",
               "params": {"step": 2, "value": "units_sold", "order": "period",
                          "periods": ["2026-Q1"]}}
        script = Script(needs_followup=True, trend_plans=[TREND_PLAN], actions=[fit],
                        repairs=[HV08_SERIES, HV08_SERIES])
        out, _ = run_d(script, pinned, engine, builder, question=FORECAST_Q)
        assert "forecast" not in out.selected.columns


def index_of(values: list[str]) -> ValueIndex:
    index = ValueIndex.__new__(ValueIndex)
    index.values = [("customers", "customer_name", v) for v in values]
    return index


class TestSearch:
    def test_the_whole_phrase_finds_the_name(self, engine):
        hits = value_index(ToolContext.create(engine)).search("Nadia Popesku")
        assert hits[0][2] == "Nadia Popescu"

    @pytest.mark.parametrize("term", ["Popesku", "Nadia"])
    def test_a_part_of_the_name_still_returns_it(self, engine, term):
        # These two searches each cut Nadia Popescu off at 5 rows in the holdout run.
        hits = value_index(ToolContext.create(engine)).search(term)
        assert "Nadia Popescu" in [row[2] for row in hits]

    def test_every_row_tied_with_the_best_is_returned(self):
        names = [f"Person{i} Smith" for i in range(12)]
        hits = index_of(names).search("Smith", limit=5)
        assert len(hits) == 12

    def test_ties_are_capped(self):
        names = [f"Person{i} Smith" for i in range(MAX_TIED_MATCHES + 9)]
        assert len(index_of(names).search("Smith")) == MAX_TIED_MATCHES

    def test_below_the_best_the_limit_still_applies(self):
        names = ["Ann Lee", "Ann Leed", "Ann Leen", "Ann Leer", "Ann Lees", "Ann Leet"]
        hits = index_of(names).search("Ann Lee", limit=3)
        assert hits[0][2] == "Ann Lee" and len(hits) == 3

    def test_parts_are_used_only_when_the_phrase_finds_nothing(self):
        index = index_of(["Zo", "Kai Popescu"])
        assert index.search("xqwvjkz popescu")[0][2] == "Kai Popescu"
        assert index.search("kai popescu")[0][3] == 1.0

    def test_a_longer_value_is_not_outranked_for_holding_the_same_word(self):
        hits = index_of(["Kai Popescu", "Nadia Popescu"]).search("Popesku")
        assert hits[0][3] == hits[1][3]


class TestValueHint:
    def test_the_repair_is_told_the_stored_spelling(self, pinned, engine, builder):
        script = Script(step0=NAME_SQL.format("Nadia Popesku"),
                        repairs=[NAME_SQL.format("Nadia Popescu")])
        out, _ = run_d(script, pinned, engine, builder, question=NAME_Q)
        assert "Closest stored values: 'Nadia Popescu'" in script.prompts["repair"]
        assert out.selected.rows and out.selected.rows[0][0] == 836

    def test_a_zero_count_from_a_misspelling_is_repaired_too(
            self, pinned, engine, builder):
        script = Script(
            step0="SELECT COUNT(*) AS n FROM customers "
                  "WHERE customer_name = 'Nadia Popesku'",
            repairs=["SELECT COUNT(*) AS n FROM customers "
                     "WHERE customer_name = 'Nadia Popescu'"])
        out, _ = run_d(script, pinned, engine, builder, question=NAME_Q)
        assert "Nadia Popescu" in script.prompts["repair"]
        assert out.selected.rows == [[1]]

    def test_a_lookup_shows_every_row_it_found(self, pinned, engine, builder):
        search = {"action": "search_values", "term": "Nadia"}
        script = Script(needs_followup=True, actions=[search])
        run_d(script, pinned, engine, builder)
        assert "Nadia Popescu" in script.prompts["decide"]

    def test_text_filters_are_read_with_their_real_table(self):
        sql = ("SELECT 1 FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
               "WHERE o.status = 'x' AND c.segment IN ('y', 'z') AND o.status LIKE 'w%'")
        assert set(text_filters(sql, SCHEMA)) == {
            ("orders", "status", "x"), ("customers", "segment", "y"),
            ("customers", "segment", "z"), ("orders", "status", "w%")}


class TestWriteSql:
    @pytest.mark.parametrize("sql", [
        "DELETE FROM customers", "delete from customers where 1=1",
        "UPDATE orders SET status = 'x'", "INSERT INTO regions VALUES (9, 'x')",
        "DROP TABLE support_tickets", "ALTER TABLE orders ADD COLUMN x TEXT",
        "TRUNCATE TABLE orders", "CREATE TABLE t (a INTEGER)",
        "REPLACE INTO regions VALUES (1, 'x')",
        "  -- tidy up\n  DELETE FROM orders",
        "/* tidy */ DROP TABLE orders",
        "SELECT 1; DROP TABLE orders",
        "WITH x AS (SELECT 1) DELETE FROM orders",
        "DELETE customers WHERE garbled (",
    ])
    def test_a_write_is_recognised(self, sql):
        assert is_write_sql(sql)

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM orders", "SELECT 'delete' AS word",
        "SELECT update_date, created_at FROM t", "SELECT 0 AS deleted_orders",
        "WITH d AS (SELECT 1 AS drop_count) SELECT * FROM d",
        "SELECT REPLACE(name, 'a', 'b') FROM t", "", "SELECT FROM WHERE (",
    ])
    def test_a_read_is_not(self, sql):
        assert not is_write_sql(sql)


class TestWriteEndsTheRun:
    def test_the_first_layer_does_miss_this_wording(self):
        assert not is_write_request(SW06)

    @pytest.mark.parametrize("arch", ["full", "router"])
    def test_sw06_is_refused_with_the_same_message(self, pinned, engine, builder, arch):
        script = Script(step0=SW06_SQL)
        out = run_architecture(arch, SW06, settings=pinned,
                               llm=FakeClient(responder=script),
                               tools=ToolContext.create(engine),
                               context_builder=builder)
        assert out.status == "refused" and out.answer == WRITE_REFUSAL
        assert out.selected is None and out.attempts == ()
        assert script.calls == ["step0"]  # nothing runs after the write

    def test_the_write_stays_on_record_and_never_ran(self, pinned, engine, builder):
        script = Script(step0=SW06_SQL)
        with engine.connect() as conn:
            before = conn.execute(text("SELECT COUNT(*) FROM customers")).scalar_one()
        out, _ = run_d(script, pinned, engine, builder, question=SW06)
        with engine.connect() as conn:
            after = conn.execute(text("SELECT COUNT(*) FROM customers")).scalar_one()
        assert before == after
        assert out.steps[-1]["sql"] == SW06_SQL and out.stop_reason == "refused"

    def test_a_later_step_can_end_the_run_too(self, pinned, engine, builder):
        action = {"action": "run_sql", "sql": "DROP TABLE orders"}
        script = Script(needs_followup=True, actions=[action])
        out, _ = run_d(script, pinned, engine, builder)
        assert out.status == "refused" and out.answer == WRITE_REFUSAL
        assert [a.sql for a in out.attempts] == [COUNT]  # the read that did run

    def test_a_repair_can_end_the_run_too(self, pinned, engine, builder):
        script = Script(step0="SELECT nope FROM customers",
                        repairs=["DELETE FROM customers"])
        out, _ = run_d(script, pinned, engine, builder)
        assert out.status == "refused"

    def test_the_guard_is_still_the_final_layer(self):
        with pytest.raises(UnsafeSQLError):
            guard_sql(SW06_SQL, max_rows=10)

    def test_a_keeps_its_own_behaviour(self, pinned, engine, builder):
        script = Script(step0=SW06_SQL)
        out = run_architecture("baseline", SW06, settings=pinned,
                               llm=FakeClient(responder=script),
                               tools=ToolContext.create(engine), context_builder=builder)
        assert out.status != "refused"


class TestDataDoesNotTrackIt:
    @pytest.mark.parametrize("arch", ["full", "router"])
    def test_a_constant_is_not_reported_as_a_figure(self, pinned, engine, builder, arch):
        script = Script(step0="SELECT 0 AS deleted_orders")
        out = run_architecture(arch, "How many orders were deleted in 2025?",
                               settings=pinned, llm=FakeClient(responder=script),
                               tools=ToolContext.create(engine), context_builder=builder)
        assert out.status == "done"
        assert "doesn't track" in out.answer and "0" not in out.answer
        assert out.selected is None
        assert "answer" not in script.calls

    def test_a_value_the_column_does_not_hold(self, pinned, engine, builder):
        script = Script(step0="SELECT COUNT(*) FROM orders WHERE status = 'deleted'")
        out, _ = run_d(script, pinned, engine, builder)
        assert out.status == "done" and "doesn't track" in out.answer
        assert "'deleted' is not a value of orders.status" in out.answer
        assert "cancelled, completed, refunded" in out.answer

    def test_like_is_read_the_same_way(self, pinned, engine, builder):
        script = Script(step0="SELECT COUNT(*) FROM orders WHERE status LIKE '%delete%'")
        out, _ = run_d(script, pinned, engine, builder)
        assert "doesn't track" in out.answer

    def test_a_real_zero_is_still_a_figure(self, pinned, engine, builder):
        script = Script(step0="SELECT COUNT(*) AS n FROM orders "
                              "WHERE status = 'cancelled' "
                              "AND order_date < '1999-01-01'")
        out, _ = run_d(script, pinned, engine, builder)
        assert "answer" in script.calls and out.selected.rows == [[0]]

    def test_arithmetic_on_earlier_results_is_still_a_figure(
            self, pinned, engine, builder):
        action = {"action": "run_sql", "sql": "SELECT 1400 * 2 AS doubled"}
        script = Script(needs_followup=True, actions=[action],
                        plan={"base_step": 2, "columns": []})
        out, _ = run_d(script, pinned, engine, builder)
        assert "answer" in script.calls and "doesn't track" not in out.answer

    def test_a_value_that_exists_with_rows_is_left_alone(self, pinned, engine, builder):
        script = Script(
            step0="SELECT COUNT(*) AS n FROM orders WHERE status = 'refunded'")
        out, _ = run_d(script, pinned, engine, builder)
        assert "answer" in script.calls and out.selected.rows[0][0] > 0

    @pytest.mark.parametrize(("sql", "expected"), [
        ("SELECT 0", True), ("SELECT 1 + 1 AS two", True),
        ("WITH a AS (SELECT 1 AS x) SELECT x FROM a", False),
        ("SELECT COUNT(*) FROM orders", False), ("-- compute: x", False),
    ])
    def test_reads_no_table(self, sql, expected):
        assert reads_no_table(sql) is expected
