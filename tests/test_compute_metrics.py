"""decompose_change and contribution_by_dimension (agentcrew/compute/metrics.py).

Comparisons here are generic (halves of 2024, channels in 2024); none is an
evaluation question.
"""

from __future__ import annotations

import re

import pytest

from agentcrew.compute.functions import ComputeError
from agentcrew.compute.metrics import (
    AGGREGATES,
    COLUMNS,
    FACTORS,
    LABELS,
    METRICS,
    SIMILAR_PCT,
    Comparison,
    contribution_by_dimension,
    decompose_change,
)
from agentcrew.context.dictionary import load_dictionary
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext, execute_readonly_sql

H1_VS_H2 = Comparison(period_a={"start": "2024-01-01", "end": "2024-06-30"},
                      period_b={"start": "2024-07-01", "end": "2024-12-31"})
CHANNELS = Comparison(group_dimension="channel", group_a="Partner", group_b="Direct",
                      period={"start": "2024-01-01", "end": "2024-12-31"})
THREE_CHANNELS = Comparison(group_dimension="channel",
                            groups=["Direct", "Partner", "Online"],
                            period={"start": "2024-01-01", "end": "2024-12-31"})


@pytest.fixture(scope="module")
def run(request):
    from agentcrew.db.engine import read_only_engine

    tools = ToolContext.create(read_only_engine(request.getfixturevalue("db_path")))
    return lambda sql: execute_readonly_sql(tools, sql)


def _rows(df, analysis, side_a=None):
    part = df[df["analysis"] == analysis]
    if side_a is not None:
        part = part[part["side_a"] == side_a]
    return part.set_index("item")


def fake_runner(by_side: dict[str, dict[str, float]]):
    """A runner that answers decompose_change's one-row queries per side.

    The side is recognised from the quoted literal in the WHERE clause, so
    the label logic can be tested on exact numbers without the database.
    """
    def run(sql: str) -> ExecutionResult:
        side = next(s for s in by_side if f"'{s}'" in sql)
        values = by_side[side]
        names = re.findall(r" AS (\w+)", sql)
        return ExecutionResult(ok=True, sql=sql, columns=names,
                               rows=[[values.get(n, 0.0) for n in names]],
                               row_count=1)
    return run


class TestDictionaryFormulas:
    def test_aggregate_formulas_match_the_data_dictionary(self):
        metrics = {k: " ".join(v.split()) for k, v in load_dictionary()["metrics"].items()}

        def fold(sql: str) -> str:
            return re.sub(r"\b(o|oi|p|m|t|c|order_items|products|marketing_spend)\.",
                          "", sql.replace(" ", "")).lower()
        assert fold(AGGREGATES["revenue"][1]) in fold(metrics["revenue"])
        assert fold(AGGREGATES["units_sold"][1]) in fold(metrics["units_sold"])
        assert fold(AGGREGATES["gross_margin"][1]) in fold(metrics["gross_margin"])
        assert fold(AGGREGATES["marketing_spend"][1]) in fold(metrics["marketing_spend"])
        assert "julianday(resolved_date)-julianday(created_date)" in fold(
            metrics["ticket_resolution_days"])

    def test_every_metric_name_is_a_dictionary_metric_or_a_table_count(self):
        names = set(load_dictionary()["metrics"])
        assert set(METRICS) - names == {"tickets"}


class TestDecompose:
    def test_revenue_components_reconcile(self, run):
        df, queries = decompose_change(run, "revenue", H1_VS_H2)
        revenue = df[df["analysis"] == "metric"].iloc[0]
        assert revenue["item"] == "revenue"
        for analysis in ("orders x average order value",
                         "units x average price x (1 - average discount)"):
            parts = _rows(df, analysis)
            for side in ("value_a", "value_b"):
                product = parts[side].prod()
                assert product == pytest.approx(revenue[side], rel=1e-9)
            # log shares of one decomposition add up to 100%
            assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)
        assert all("o.status = 'completed'" in q for q in queries)
        assert all("oi.quantity * oi.unit_price * (1 - oi.discount)" in q for q in queries)

    def test_group_comparison_and_ratio_metric(self, run):
        df, _ = decompose_change(run, "refund_rate", CHANNELS)
        rate = df[df["item"] == "refund rate"].iloc[0]
        parts = _rows(df, "refunded orders / all orders")
        assert parts.loc["refunded orders", "value_a"] / \
            parts.loc["all orders (divides)", "value_a"] == pytest.approx(rate["value_a"])
        assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)

    def test_additive_decomposition(self, run):
        df, _ = decompose_change(run, "gross_margin", H1_VS_H2)
        parts = _rows(df, "revenue - cost")
        margin = df[df["item"] == "gross margin"].iloc[0]
        # raw values; the subtracted part is named as such
        assert parts.loc["revenue", "value_b"] - parts.loc["cost (subtracted)", "value_b"] \
            == pytest.approx(margin["value_b"])
        assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)

    def test_metrics_without_components_are_refused(self, run):
        with pytest.raises(ComputeError, match="no component decomposition"):
            decompose_change(run, "tickets", H1_VS_H2)

    def test_empty_side_is_reported(self, run):
        empty = Comparison(period_a={"start": "2019-01-01", "end": "2019-12-31"},
                           period_b={"start": "2024-01-01", "end": "2024-12-31"})
        with pytest.raises(ComputeError, match="no data"):
            decompose_change(run, "revenue", empty)


class TestSeveralGroups:
    def test_the_first_group_is_compared_with_each_other_group(self, run):
        df, queries = decompose_change(run, "revenue", THREE_CHANNELS)
        assert len(queries) == 3  # one query per group, not per pair
        pairs = df[["side_a", "side_b"]].drop_duplicates().values.tolist()
        assert pairs == [["Partner", "Direct"], ["Online", "Direct"]]
        for reference in ("Partner", "Online"):
            parts = _rows(df, "orders x average order value", reference)
            assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)

    def test_matches_the_two_group_comparison(self, run):
        three, _ = decompose_change(run, "revenue", THREE_CHANNELS)
        two, _ = decompose_change(run, "revenue", CHANNELS)
        pair = three[three["side_a"] == "Partner"].reset_index(drop=True)
        assert pair.drop(columns=["comparison"]).equals(
            two.drop(columns=["comparison"]))

    def test_contribution_for_several_groups(self, run):
        df, _ = contribution_by_dimension(run, "revenue", "segment", THREE_CHANNELS)
        assert set(df["side_a"]) == {"Partner", "Online"}
        for reference in ("Partner", "Online"):
            part = df[df["side_a"] == reference]
            assert part["share_of_change_pct"].sum() == pytest.approx(100.0)
            assert part["largest_contributor"].tolist().count(True) == 1

    @pytest.mark.parametrize("groups", [["Direct"], ["Direct", "Direct"],
                                        ["a", "b", "c", "d", "e", "f", "g"]])
    def test_group_list_is_validated(self, groups):
        with pytest.raises(ValueError):
            Comparison(group_dimension="channel", groups=groups)

    def test_groups_and_pair_cannot_be_mixed(self):
        with pytest.raises(ValueError):
            Comparison(group_dimension="channel", groups=["Direct", "Online"],
                       group_a="Partner", group_b="Direct")


class TestComparisonLabels:
    """Every component gets a label, including the ones that barely move."""

    SIDES = {
        # A has more orders than B at almost the same order value.
        "A": {"revenue": 3000.0 * 300, "order_count": 300, "gross_sales": 1e6,
              "units_sold": 2000},
        "B": {"revenue": 3050.0 * 200, "order_count": 200, "gross_sales": 677_777.0,
              "units_sold": 1350},
    }

    def _df(self):
        comparison = Comparison(group_dimension="channel", groups=["A", "B"])
        df, _ = decompose_change(fake_runner(self.SIDES), "revenue", comparison)
        return df

    def test_columns_are_shared_by_both_tools(self):
        assert list(self._df().columns) == list(COLUMNS)

    def test_about_the_same_within_the_threshold(self):
        parts = _rows(self._df(), "orders x average order value")
        aov = parts.loc["average order value"]
        assert abs(aov["pct_change"]) <= SIMILAR_PCT
        assert aov["comparison"] == "about the same"
        assert parts.loc["number of orders", "comparison"] == "higher"
        assert parts.loc["number of orders", "largest_contributor"]
        assert not aov["largest_contributor"]

    def test_metric_row_is_labelled_but_never_a_contributor(self):
        df = self._df()
        metric = df[df["analysis"] == "metric"].iloc[0]
        assert metric["comparison"] == "higher"
        assert not metric["largest_contributor"]


class TestContribution:
    def test_shares_add_up_and_the_largest_is_named(self, run):
        df, _ = contribution_by_dimension(run, "revenue", "segment", CHANNELS)
        assert df["share_of_change_pct"].sum() == pytest.approx(100.0)
        assert df["largest_contributor"].tolist().count(True) == 1
        top = df[df["largest_contributor"]].iloc[0]
        assert top["change"] == df["change"].max()
        assert set(df["analysis"]) == {"revenue by segment"}
        assert list(df.columns) == list(COLUMNS)

    def test_month_format_source_uses_month_bounds(self, run):
        df, queries = contribution_by_dimension(run, "marketing_spend", "channel", H1_VS_H2)
        assert "BETWEEN '2024-01' AND '2024-06'" in queries[0]
        assert set(df["item"]) == {"Direct", "Online", "Partner"}

    def test_ratio_metrics_are_refused(self, run):
        with pytest.raises(ComputeError, match="ratio"):
            contribution_by_dimension(run, "refund_rate", "region", CHANNELS)

    def test_unknown_dimension_and_metric(self, run):
        with pytest.raises(ComputeError, match="not available"):
            contribution_by_dimension(run, "marketing_spend", "category", H1_VS_H2)
        with pytest.raises(ComputeError, match="unknown metric"):
            contribution_by_dimension(run, "profit", "region", H1_VS_H2)

    def test_values_are_quoted_not_spliced(self, run):
        hostile = Comparison(group_dimension="channel", group_a="x' OR '1'='1",
                             group_b="Direct")
        with pytest.raises(ComputeError, match="no data"):
            contribution_by_dimension(run, "revenue", "region", hostile)


class TestComparisonValidation:
    def test_needs_exactly_one_mode(self):
        with pytest.raises(ValueError):
            Comparison()
        with pytest.raises(ValueError):
            Comparison(period_a={"start": "2024-01-01", "end": "2024-02-01"},
                       period_b={"start": "2024-03-01", "end": "2024-04-01"},
                       group_dimension="channel", group_a="a", group_b="b")

    def test_period_order(self):
        with pytest.raises(ValueError):
            Comparison(period_a={"start": "2024-02-01", "end": "2024-01-01"},
                       period_b={"start": "2024-03-01", "end": "2024-04-01"})

    def test_churn_rate_rejects_periods(self, run):
        with pytest.raises(ComputeError, match="cannot be split by period"):
            decompose_change(run, "churn_rate", H1_VS_H2)


class TestSafety:
    """Model-supplied names are allowlisted and values quoted; every SQL the
    metric layer builds passes the same read-only guard as model SQL."""

    @staticmethod
    def _all_queries():
        from agentcrew.compute.metrics import _aggregates_for, _source_of, build_sql

        period = H1_VS_H2.period_a
        for metric in METRICS.values():
            source = _source_of(metric)
            aggregates = _aggregates_for({metric.factor})
            yield build_sql(source, aggregates, period, {})
            for dim in source.dimensions:
                yield build_sql(source, aggregates, period, {dim: "x'; DROP TABLE t; --"})
                yield build_sql(source, aggregates, None, {}, dimension=dim)

    def test_every_generated_query_passes_the_read_only_guard(self, run):
        from agentcrew.db.safety import guard_sql

        queries = list(self._all_queries())
        assert len(queries) > 100
        for sql in queries:
            guarded = guard_sql(sql, max_rows=500)  # raises if not read-only
            assert run(guarded.sql).ok, sql

    @pytest.mark.parametrize("call", [
        lambda r: decompose_change(r, "revenue; DROP TABLE orders", H1_VS_H2),
        lambda r: contribution_by_dimension(r, "revenue", "region) OR (1=1", H1_VS_H2),
        lambda r: contribution_by_dimension(r, "revenue", "o.status", H1_VS_H2),
        lambda r: decompose_change(r, "revenue", Comparison(
            group_dimension="channel; --", group_a="a", group_b="b")),
        lambda r: decompose_change(r, "revenue", Comparison(
            period_a=H1_VS_H2.period_a, period_b=H1_VS_H2.period_b,
            filters={"1=1 OR channel": "Direct"})),
    ])
    def test_unknown_names_are_refused_before_any_sql_runs(self, call):
        executed: list[str] = []

        def spy(sql):
            executed.append(sql)
            raise AssertionError("no SQL may run")

        with pytest.raises(ComputeError):
            call(spy)
        assert executed == []

    def test_dimensions_only_use_dictionary_columns(self):
        import sqlglot
        from sqlglot import exp

        from agentcrew.compute.metrics import AGGREGATES

        tables = {t: set(spec["columns"])
                  for t, spec in load_dictionary()["tables"].items()}
        sources = {id(src): src for src, _ in AGGREGATES.values()}
        for source in sources.values():
            from_tree = sqlglot.parse_one(f"SELECT 1 {source.from_sql}", read="sqlite")
            alias = {t.alias_or_name: t.name for t in from_tree.find_all(exp.Table)}
            for name, sql in source.dimensions.items():
                for col in sqlglot.parse_one(sql, read="sqlite").find_all(exp.Column):
                    assert col.name in tables[alias[col.table]], (source.name, name)


def test_every_factor_has_a_plain_label():
    assert set(LABELS) == set(FACTORS)
    assert all("_" not in label for label in LABELS.values())
