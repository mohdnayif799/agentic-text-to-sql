"""Statistics over a result table (pandas, deterministic, no generated code).

Each function takes a DataFrame (a previous step's result) plus column names
and returns a new DataFrame; the input is never modified. Invalid input
raises ComputeError with a message the agent can act on.

  pct_change             row-over-row percentage change
  yoy_growth             change against the same period a year earlier
  compare_periods        one value in two periods (optionally per group)
  share_of_total         each row's share of the column total, in percent
  zscore_anomalies       rows whose z-score passes a threshold, measured
                         against the mean or against a fitted linear trend
  linear_trend_forecast  least-squares line over row order, extended ahead
  correlation            Pearson correlation of two columns
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd


class ComputeError(ValueError):
    """The inputs cannot be computed; the message says why."""


def _column(df: pd.DataFrame, name: str) -> str:
    for column in df.columns:
        if column.lower() == str(name).lower():
            return column
    raise ComputeError(f"no column {name!r}; available: {list(df.columns)}")


def _numbers(df: pd.DataFrame, name: str) -> pd.Series:
    column = _column(df, name)
    values = pd.to_numeric(df[column], errors="coerce")
    if values.isna().any():
        raise ComputeError(f"column {column!r} has non-numeric or missing values")
    return values.astype(float)


def _require_rows(df: pd.DataFrame, n: int) -> None:
    if len(df) < n:
        raise ComputeError(f"need at least {n} rows, got {len(df)}")


def pct_change(df: pd.DataFrame, value: str, order: str | None = None) -> pd.DataFrame:
    _require_rows(df, 2)
    data = df.sort_values(_column(df, order)) if order else df
    values = _numbers(data, value)
    out = data.copy()
    previous = values.shift(1)
    out["pct_change"] = (values - previous) / previous.abs() * 100
    return out.reset_index(drop=True)


_YEAR = re.compile(r"^(\d{4})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")


def _previous_year(period: str) -> str:
    text = str(period)
    for pattern in (_YEAR, _YEAR_MONTH, _YEAR_QUARTER):
        found = pattern.match(text)
        if found:
            return str(int(found.group(1)) - 1) + text[4:]
    raise ComputeError(
        f"period {period!r} is not 'YYYY', 'YYYY-MM' or 'YYYY-Qn'"
    )


def yoy_growth(df: pd.DataFrame, period: str, value: str) -> pd.DataFrame:
    p_col = _column(df, period)
    values = _numbers(df, value)
    lookup = dict(zip(df[p_col].astype(str), values, strict=True))
    rows = []
    for key, current in zip(df[p_col].astype(str), values, strict=True):
        prior_key = _previous_year(key)
        if prior_key in lookup:
            prior = lookup[prior_key]
            growth = (current - prior) / abs(prior) * 100 if prior else np.nan
            rows.append([key, current, prior_key, prior, current - prior, growth])
    if not rows:
        raise ComputeError("no period has a matching period one year earlier")
    return pd.DataFrame(rows, columns=[p_col, "value", "prior_period", "prior_value",
                                       "change", "yoy_growth_pct"])


def compare_periods(
    df: pd.DataFrame,
    period: str,
    value: str,
    period_a: str,
    period_b: str,
    by: str | None = None,
) -> pd.DataFrame:
    p_col = _column(df, period)
    v_col = _column(df, value)
    data = df.assign(**{v_col: _numbers(df, v_col)})
    keys = [_column(df, by)] if by else []
    a = data[data[p_col].astype(str) == str(period_a)]
    b = data[data[p_col].astype(str) == str(period_b)]
    if a.empty or b.empty:
        raise ComputeError(f"no rows for period {period_a!r} or {period_b!r}")
    if keys:
        left = a.groupby(keys)[v_col].sum().rename("value_a")
        right = b.groupby(keys)[v_col].sum().rename("value_b")
        out = pd.concat([left, right], axis=1).fillna(0.0).reset_index()
    else:
        out = pd.DataFrame({"value_a": [a[v_col].sum()], "value_b": [b[v_col].sum()]})
    out["change"] = out["value_b"] - out["value_a"]
    out["pct_change"] = np.where(out["value_a"] != 0,
                                 out["change"] / out["value_a"].abs() * 100, np.nan)
    out.insert(len(keys), "period_a", str(period_a))
    out.insert(len(keys) + 1, "period_b", str(period_b))
    return out


def share_of_total(df: pd.DataFrame, value: str, label: str) -> pd.DataFrame:
    values = _numbers(df, value)
    total = values.sum()
    if total == 0:
        raise ComputeError("the column total is zero")
    out = df[[_column(df, label), _column(df, value)]].copy()
    out["share_pct"] = values / total * 100
    return out


def _trend(values: pd.Series) -> tuple[np.ndarray, float, float]:
    x = np.arange(1, len(values) + 1, dtype=float)
    slope, intercept = np.polyfit(x, values.to_numpy(), 1)
    return x, float(slope), float(intercept)


def zscore_anomalies(
    df: pd.DataFrame,
    value: str,
    label: str,
    threshold: float = 2.0,
    direction: str = "both",
    against: str = "mean",
    ddof: int = 1,
) -> pd.DataFrame:
    """Rows whose z-score passes `threshold`.

    against="mean": z = (value - mean) / sd. against="trend": a least-squares
    line over row order (x = 1..n) is the expectation and z is the residual
    over the residuals' sd. direction: "both", "above" or "below".
    """
    _require_rows(df, 3)
    values = _numbers(df, value)
    if against == "mean":
        expected = np.full(len(values), values.mean())
    elif against == "trend":
        x, slope, intercept = _trend(values)
        expected = intercept + slope * x
    else:
        raise ComputeError("against must be 'mean' or 'trend'")
    residual = values.to_numpy() - expected
    sd = residual.std(ddof=ddof)
    if sd == 0:
        raise ComputeError("the values do not vary")
    z = residual / sd
    keep = {"both": np.abs(z) > threshold, "above": z > threshold,
            "below": z < -threshold}.get(direction)
    if keep is None:
        raise ComputeError("direction must be 'both', 'above' or 'below'")
    out = pd.DataFrame({
        _column(df, label): df[_column(df, label)].to_numpy(),
        "value": values.to_numpy(), "expected": expected,
        "residual": residual, "z_score": z,
    })
    return out[keep].reset_index(drop=True)


def linear_trend_forecast(
    df: pd.DataFrame, value: str, order: str, periods_ahead: int = 1
) -> pd.DataFrame:
    """Least-squares line with x = 1..n over rows sorted by `order`."""
    _require_rows(df, 3)
    if not 1 <= int(periods_ahead) <= 24:
        raise ComputeError("periods_ahead must be between 1 and 24")
    data = df.sort_values(_column(df, order)).reset_index(drop=True)
    values = _numbers(data, value)
    _, slope, intercept = _trend(values)
    n = len(values)
    steps = np.arange(1, int(periods_ahead) + 1)
    return pd.DataFrame({
        "steps_ahead": steps, "x": n + steps,
        "forecast": intercept + slope * (n + steps),
        "slope": slope, "intercept": intercept, "points": n,
    })


def correlation(df: pd.DataFrame, a: str, b: str) -> pd.DataFrame:
    _require_rows(df, 3)
    x, y = _numbers(df, a), _numbers(df, b)
    if x.std() == 0 or y.std() == 0:
        raise ComputeError("a column does not vary; correlation is undefined")
    r = float(np.corrcoef(x, y)[0, 1])
    return pd.DataFrame({"pearson_r": [r], "points": [len(x)]})


FUNCTIONS = {
    f.__name__: f
    for f in (pct_change, yoy_growth, compare_periods, share_of_total,
              zscore_anomalies, linear_trend_forecast, correlation)
}
