"""A metric over the periods before the one a question asks about (architecture D).

period_history reads one metric in a period and in the periods of the same length
just before it, per dimension value, with the lowest and highest earlier value, so
a spike or dip can be set against what is normal. Its SQL is built from the same
metric specs as compute/metrics.py, never written by a model.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

import pandas as pd

from agentcrew.compute.functions import ComputeError
from agentcrew.compute.metrics import (
    LABELS,
    Metric,
    Period,
    Runner,
    Source,
    _aggregates_for,
    _check_periods,
    _dimension,
    _key,
    _metric,
    _run,
    _source_of,
    _value,
    build_sql,
)

#: Earlier periods set beside the one a spike or dip question asks about.
BASELINE_PERIODS = 4
#: Largest values kept per dimension, so the table stays readable in a prompt.
MAX_HISTORY_ROWS = 8
TOTAL_LABEL = "total"
_QUARTER_MONTHS = {1: 1, 4: 2, 7: 3, 10: 4}


def _months_back(day: date, months: int) -> date:
    """The first of the month `months` before `day`'s month."""
    index = day.year * 12 + day.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


def _whole_months(period: Period) -> int | None:
    """How many calendar months the period covers, if it is exactly that."""
    last = calendar.monthrange(period.end.year, period.end.month)[1]
    if period.start.day != 1 or period.end.day != last:
        return None
    return (period.end.year * 12 + period.end.month
            - period.start.year * 12 - period.start.month + 1)


def preceding_periods(period: Period, count: int = BASELINE_PERIODS) -> list[Period]:
    """The `count` periods of the same length just before `period`, oldest
    first. Whole months stay whole months (Q2 before Q3, whatever their days)."""
    months = _whole_months(period)
    found = []
    for k in range(count, 0, -1):
        if months is None:
            length = (period.end - period.start).days + 1
            start = period.start - timedelta(days=k * length)
            found.append(Period(start=start, end=start + timedelta(days=length - 1)))
        else:
            start = _months_back(period.start, k * months)
            after = _months_back(period.start, (k - 1) * months)
            found.append(Period(start=start, end=after - timedelta(days=1)))
    return found


def period_label(period: Period) -> str:
    """A calendar period's usual name ('2025-Q3'), otherwise its start and end."""
    months, start = _whole_months(period), period.start
    if months == 1:
        return f"{start.year}-{start.month:02d}"
    if months == 3 and start.month in _QUARTER_MONTHS:
        return f"{start.year}-Q{_QUARTER_MONTHS[start.month]}"
    if months == 12 and start.month == 1:
        return str(start.year)
    return period.label()


def has_dimension(metric_name: str, dimension: str | None) -> bool:
    """True if the metric's source can be broken down by this dimension."""
    source = _source_of(_metric(metric_name))
    return bool(dimension) and _key(dimension) in source.dimensions


def _period_values(run: Runner, metric: Metric, source: Source, period: Period,
                   dimension: str | None, filters: Mapping[str, str],
                   queries: list[str]) -> dict[object, float | None]:
    """{value of the dimension (or the metric's name): the metric}. Rows with no
    data at all are left out: an aggregate over no rows still returns a row."""
    aggregates = _aggregates_for([metric.factor])
    sql = build_sql(source, aggregates, period, filters, dimension=dimension)
    queries.append(sql)
    values: dict[object, float | None] = {}
    for row in _run(run, sql):
        if not any(row.get(name) for name in aggregates):
            continue
        item = row[dimension] if dimension else LABELS[metric.factor]
        try:
            values[item] = _value(metric.factor, row)
        except ComputeError:  # a ratio whose denominator is zero has no value
            values[item] = None
    return values


def _history_row(item: object, tables: Sequence[Mapping[object, float | None]],
                 now: Mapping[object, float | None], missing: float | None
                 ) -> list[object]:
    earlier = [table.get(item, missing) for table in tables]
    known = [v for v in earlier if v is not None]
    return [item, *earlier, min(known, default=None), max(known, default=None),
            now.get(item, missing)]


def period_history(
    run: Runner, metric_name: str, period: Period, dimension: str | None = None,
    filters: Mapping[str, str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """The metric in `period` (last column) and the periods before it, largest
    first, with the lowest and highest earlier value per row. An additive metric
    broken down by a dimension also gets a total row, over every value even when
    only the largest are listed. Periods before the data starts are left out."""
    metric = _metric(metric_name)
    _check_periods(metric)
    source = _source_of(metric)
    dimension = _key(dimension) if dimension else None
    if dimension:
        _dimension(source, dimension)
    filters = dict(filters or {})
    queries: list[str] = []
    now = _period_values(run, metric, source, period, dimension, filters, queries)
    earlier = []
    for previous in preceding_periods(period):
        values = _period_values(run, metric, source, previous, dimension, filters,
                                queries)
        if values:
            earlier.append((period_label(previous), values))
    if not earlier:
        raise ComputeError(f"no earlier data before {period.label()}")

    tables = [values for _, values in earlier]
    missing = 0.0 if metric.additive else None
    items = sorted({*now, *(item for table in tables for item in table)},
                   key=lambda i: (-(now.get(i) or 0.0), str(i)))
    rows = [_history_row(item, tables, now, missing)
            for item in items[:MAX_HISTORY_ROWS]]
    if metric.additive and dimension:
        def total(table: Mapping[object, float | None]) -> dict[object, float]:
            return {TOTAL_LABEL: sum(v or 0.0 for v in table.values())}

        rows.append(_history_row(TOTAL_LABEL, [total(t) for t in tables], total(now),
                                 missing))
    columns = [dimension or "metric", *(label for label, _ in earlier),
               "earlier_low", "earlier_high", period_label(period)]
    return pd.DataFrame(rows, columns=columns), queries
