"""Rule 3 of protocol v2: a ranked result can answer a which/top-N question."""

from __future__ import annotations

from evalkit.grader import Gold, contains_gold, grade_top_n
from evalkit.ranking import ranks_by_metric

ROWS = [["APAC", 44], ["EMEA", 11], ["North America", 9], ["LATAM", 3]]
COLS = ["region_name", "churned"]


class TestRanksByMetric:
    def test_order_by_result_alias(self):
        sql = "SELECT region_name, COUNT(*) AS churned FROM t GROUP BY 1 ORDER BY churned DESC"
        assert ranks_by_metric(sql, COLS, ROWS)[0]

    def test_order_by_aggregate_expression(self):
        sql = "SELECT region_name FROM t GROUP BY region_name ORDER BY SUM(x) DESC"
        assert ranks_by_metric(sql, ["region_name"], [["a"], ["b"]])[0]

    def test_order_by_position(self):
        sql = "SELECT region_name, COUNT(*) FROM t GROUP BY 1 ORDER BY 2 DESC LIMIT 500"
        assert ranks_by_metric(sql, COLS, ROWS)[0]

    def test_order_by_arithmetic(self):
        sql = "SELECT c, q3, q2 FROM t ORDER BY (q3 - q2) / q2"
        assert ranks_by_metric(sql, ["c", "q3", "q2"], [["a", 1, 2]])[0]

    def test_order_by_a_label_is_not_a_ranking(self):
        sql = "SELECT region_name, COUNT(*) AS churned FROM t GROUP BY 1 ORDER BY region_name"
        ok, why = ranks_by_metric(sql, COLS, ROWS)
        assert not ok
        assert "non-numeric" in why

    def test_no_outer_order_by(self):
        sql = "SELECT * FROM (SELECT region_name, n FROM t ORDER BY n DESC)"
        ok, why = ranks_by_metric(sql, COLS, ROWS)
        assert not ok
        assert "no ORDER BY" in why

    def test_cte_with_outer_order_by(self):
        sql = ("WITH x AS (SELECT region_name, COUNT(*) AS n FROM t GROUP BY 1) "
               "SELECT region_name, n FROM x ORDER BY n DESC")
        assert ranks_by_metric(sql, ["region_name", "n"], ROWS)[0]

    def test_unparseable_sql(self):
        assert not ranks_by_metric("SELEC nonsense ((", COLS, ROWS)[0]


class TestGradeTopN:
    gold = Gold(rows=(("APAC",),))

    def test_exact_n_rows(self):
        v = grade_top_n(self.gold, [["APAC", 44]], top_n=1, sql=None, columns=None)
        assert v.passed and v.mode == "exact"

    def test_ranked_result_with_right_first_row(self):
        sql = "SELECT region_name, COUNT(*) AS churned FROM t GROUP BY 1 ORDER BY churned DESC"
        v = grade_top_n(self.gold, ROWS, top_n=1, sql=sql, columns=COLS)
        assert v.passed and v.mode == "ranked_prefix"

    def test_ranked_result_with_wrong_first_row(self):
        sql = "SELECT region_name, n FROM t ORDER BY n DESC"
        rows = [["EMEA", 50], ["APAC", 44]]
        v = grade_top_n(self.gold, rows, top_n=1, sql=sql, columns=["region_name", "n"])
        assert not v.passed
        assert "first 1 row" in v.reason

    def test_extra_rows_without_a_ranking_are_rejected(self):
        sql = "SELECT region_name, COUNT(*) AS churned FROM t GROUP BY 1 ORDER BY region_name"
        rows = sorted(ROWS)
        v = grade_top_n(Gold(rows=(("APAC",),)), rows, top_n=1, sql=sql, columns=COLS)
        assert not v.passed

    def test_extra_rows_without_sql_are_rejected(self):
        v = grade_top_n(self.gold, ROWS, top_n=1, sql=None, columns=COLS)
        assert not v.passed
        assert "no SQL" in v.reason

    def test_top_three_prefix_is_unordered_within_the_prefix(self):
        gold = Gold(rows=(("a",), ("b",), ("c",)))
        rows = [["b", 9], ["a", 8], ["c", 7], ["d", 1]]
        sql = "SELECT name, v FROM t ORDER BY v DESC"
        assert grade_top_n(gold, rows, top_n=3, sql=sql, columns=["name", "v"]).passed

    def test_transposed_result_stays_rejected(self):
        gold = Gold(rows=((3863671.109,), (2127780.358,)))
        v = grade_top_n(gold, [[3863671.109, 2127780.358]], top_n=2, sql=None,
                        columns=["q2", "q3"])
        assert not v.passed


class TestContainsGold:
    def test_gold_rows_inside_a_larger_result(self):
        assert contains_gold(Gold(rows=(("APAC",),)), ROWS)

    def test_wrong_values_are_not_contained(self):
        assert not contains_gold(Gold(rows=(("Liam Mensah", 66160.15),)),
                                 [["Liam Mensah", 74524.60]])
