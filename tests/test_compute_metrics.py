"""decompose_change and contribution_by_dimension (agentcrew/compute/metrics.py).

Comparisons here are generic (halves of 2024, two channels in 2024); none is
an evaluation question.
"""

from __future__ import annotations

import re

import pytest

from agentcrew.compute.functions import ComputeError
from agentcrew.compute.metrics import (
    AGGREGATES,
    METRICS,
    Comparison,
    contribution_by_dimension,
    decompose_change,
)
from agentcrew.context.dictionary import load_dictionary
from agentcrew.tools import ToolContext, execute_readonly_sql

H1_VS_H2 = Comparison(period_a={"start": "2024-01-01", "end": "2024-06-30"},
                      period_b={"start": "2024-07-01", "end": "2024-12-31"})
CHANNELS = Comparison(group_dimension="channel", group_a="Partner", group_b="Direct",
                      period={"start": "2024-01-01", "end": "2024-12-31"})


@pytest.fixture(scope="module")
def run(request):
    from agentcrew.db.engine import read_only_engine

    tools = ToolContext.create(read_only_engine(request.getfixturevalue("db_path")))
    return lambda sql: execute_readonly_sql(tools, sql)


def _rows(df, deco):
    return df[df["decomposition"] == deco].set_index("component")


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
        revenue = df[df["component"] == "revenue"].iloc[0]
        for deco in ("orders x average order value",
                     "units x average price x (1 - average discount)"):
            parts = _rows(df, deco)
            for side in ("value_a", "value_b"):
                product = parts[side].prod()
                assert product == pytest.approx(revenue[side], rel=1e-9)
            # log shares of one decomposition add up to 100%
            assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)
        assert all("o.status = 'completed'" in q for q in queries)
        assert all("oi.quantity * oi.unit_price * (1 - oi.discount)" in q for q in queries)

    def test_group_comparison_and_ratio_metric(self, run):
        df, _ = decompose_change(run, "refund_rate", CHANNELS)
        rate = df[df["component"] == "refund_rate"].iloc[0]
        parts = _rows(df, "refunded orders / all orders")
        assert parts.loc["refunded_orders", "value_a"] / \
            parts.loc["all_orders (divides)", "value_a"] == pytest.approx(rate["value_a"])
        assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)

    def test_additive_decomposition(self, run):
        df, _ = decompose_change(run, "gross_margin", H1_VS_H2)
        parts = _rows(df, "revenue - cost")
        margin = df[df["component"] == "gross_margin"].iloc[0]
        assert parts["value_b"].sum() == pytest.approx(margin["value_b"])
        assert parts["share_of_change_pct"].sum() == pytest.approx(100.0)

    def test_metrics_without_components_are_refused(self, run):
        with pytest.raises(ComputeError, match="no component decomposition"):
            decompose_change(run, "tickets", H1_VS_H2)

    def test_empty_side_is_reported(self, run):
        empty = Comparison(period_a={"start": "2019-01-01", "end": "2019-12-31"},
                           period_b={"start": "2024-01-01", "end": "2024-12-31"})
        with pytest.raises(ComputeError, match="no data"):
            decompose_change(run, "revenue", empty)


class TestContribution:
    def test_shares_add_up_and_the_largest_is_named(self, run):
        df, _ = contribution_by_dimension(run, "revenue", "segment", CHANNELS)
        assert df["share_of_change_pct"].sum() == pytest.approx(100.0)
        assert df["largest_contributor"].tolist().count(True) == 1
        top = df[df["largest_contributor"]].iloc[0]
        assert top["change"] == df["change"].max()

    def test_month_format_source_uses_month_bounds(self, run):
        df, queries = contribution_by_dimension(run, "marketing_spend", "channel", H1_VS_H2)
        assert "BETWEEN '2024-01' AND '2024-06'" in queries[0]
        assert set(df["channel"]) == {"Direct", "Online", "Partner"}

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
