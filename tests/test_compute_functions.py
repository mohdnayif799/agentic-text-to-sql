"""Deterministic compute functions (agentcrew/compute/functions.py)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentcrew.compute.functions import (
    ComputeError,
    compare_periods,
    correlation,
    linear_trend_forecast,
    pct_change,
    share_of_total,
    yoy_growth,
    zscore_anomalies,
)

MONTHLY = pd.DataFrame({
    "month": ["2024-01", "2024-02", "2024-03", "2025-01", "2025-02", "2025-03"],
    "revenue": [100.0, 110.0, 121.0, 150.0, 99.0, 242.0],
})


def test_pct_change_sorts_and_does_not_mutate():
    df = pd.DataFrame({"m": ["b", "a"], "v": [150.0, 100.0]})
    out = pct_change(df, "v", order="m")
    assert list(out["m"]) == ["a", "b"]
    assert out["pct_change"].iloc[1] == pytest.approx(50.0)
    assert np.isnan(out["pct_change"].iloc[0])
    assert list(df["m"]) == ["b", "a"]


def test_yoy_growth_matches_the_same_month_a_year_earlier():
    out = yoy_growth(MONTHLY, "month", "revenue")
    assert list(out["month"]) == ["2025-01", "2025-02", "2025-03"]
    assert out["yoy_growth_pct"].tolist() == pytest.approx([50.0, -10.0, 100.0])


def test_yoy_growth_needs_a_prior_year():
    with pytest.raises(ComputeError):
        yoy_growth(MONTHLY.iloc[:3], "month", "revenue")


def test_compare_periods_total_and_by_group():
    df = pd.DataFrame({"q": ["Q2", "Q2", "Q3", "Q3"], "region": ["A", "B", "A", "B"],
                       "rev": [100.0, 50.0, 80.0, 70.0]})
    total = compare_periods(df, "q", "rev", "Q2", "Q3")
    assert total[["value_a", "value_b", "change"]].iloc[0].tolist() == [150.0, 150.0, 0.0]
    by = compare_periods(df, "q", "rev", "Q2", "Q3", by="region")
    assert by.set_index("region")["pct_change"].to_dict() == pytest.approx(
        {"A": -20.0, "B": 40.0})


def test_compare_periods_missing_period():
    with pytest.raises(ComputeError):
        compare_periods(MONTHLY, "month", "revenue", "2023-01", "2025-01")


def test_share_of_total():
    out = share_of_total(pd.DataFrame({"c": ["x", "y"], "v": [30.0, 10.0]}), "v", "c")
    assert out["share_pct"].tolist() == pytest.approx([75.0, 25.0])


def test_zscore_against_mean_and_trend():
    df = pd.DataFrame({"m": list("abcdefgh"),
                       "v": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 2.0, 17.0]})
    below = zscore_anomalies(df, "v", "m", threshold=1.5, direction="below")
    assert list(below["m"]) == ["g"]
    trend = zscore_anomalies(df, "v", "m", threshold=1.5, direction="below",
                             against="trend")
    assert list(trend["m"]) == ["g"]
    assert set(trend.columns) >= {"expected", "residual", "z_score"}


def test_zscore_rejects_bad_options():
    with pytest.raises(ComputeError):
        zscore_anomalies(MONTHLY, "revenue", "month", against="median")
    with pytest.raises(ComputeError):
        zscore_anomalies(MONTHLY, "revenue", "month", direction="sideways")


def test_linear_trend_forecast_exact_line():
    df = pd.DataFrame({"m": ["2024-03", "2024-01", "2024-02"], "v": [30.0, 10.0, 20.0]})
    out = linear_trend_forecast(df, "v", "m", periods_ahead=2)
    assert out["forecast"].tolist() == pytest.approx([40.0, 50.0])
    assert out["slope"].iloc[0] == pytest.approx(10.0)


def test_correlation():
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0], "b": [2.0, 4.0, 6.0, 8.5]})
    assert correlation(df, "a", "b")["pearson_r"].iloc[0] == pytest.approx(0.9986, abs=1e-3)
    with pytest.raises(ComputeError):
        correlation(pd.DataFrame({"a": [1.0, 1.0, 1.0], "b": [1.0, 2.0, 3.0]}), "a", "b")


def test_unknown_or_non_numeric_columns_are_explained():
    with pytest.raises(ComputeError, match="no column"):
        share_of_total(MONTHLY, "nope", "month")
    with pytest.raises(ComputeError, match="non-numeric"):
        share_of_total(pd.DataFrame({"c": ["x"], "v": ["n/a"]}), "v", "c")
