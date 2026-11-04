"""Metric decomposition and contribution analysis (architecture D).

The SQL here is generated from structured metric specs that mirror the
data dictionary's metric definitions (tests check each formula against the
dictionary text), never written by a model. Values supplied by the model
(periods, group and filter values) are validated or quoted as SQL literals.

  decompose_change           splits a metric's change between two periods or
                             two groups into multiplicative components (for
                             example revenue = orders x average order value,
                             or units x average price x (1 - average
                             discount)), reports each component's change and
                             its share of the change, and checks that the
                             components reconcile with the metric
  contribution_by_dimension  for an additive metric, how much each value of a
                             dimension contributes to the change, and which
                             one contributes most

Comparisons: two periods (period_a, period_b), or groups of one dimension
(group_dimension with group_a and group_b, or with `groups`: the group being
explained, then each group it is compared with) over an optional common
period. Two periods together with `groups` ask how a change differs between
groups (decompose_change only): why one group changed more than the others.
Optional `filters` restrict both sides to dimension values.

  period_history             the metric in one period and the periods before
                             it, per dimension value, with the lowest and
                             highest earlier value, so a spike or dip can be
                             set against what is normal

decompose_change and contribution_by_dimension return the same columns
(COLUMNS): one row per item compared
between side_a (the reference) and side_b (what is explained), with the
change, its share of the metric's change, a comparison label ("higher",
"lower", or "about the same" within SIMILAR_PCT percent) and the largest
contributor marked, so every component's comparison is stated by the tool,
including the components that do not move.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from pydantic import BaseModel, model_validator
from sqlglot import exp

from agentcrew.compute.functions import ComputeError
from agentcrew.schemas import ExecutionResult

RECONCILE_TOLERANCE = 1e-6
REVENUE_SQL = "oi.quantity * oi.unit_price * (1 - oi.discount)"

_QUARTER = ("substr({d}, 1, 4) || '-Q' || "
            "((CAST(substr({d}, 6, 2) AS INTEGER) + 2) / 3)")


def _time_dims(column: str, month_only: bool = False) -> dict[str, str]:
    if month_only:
        return {"month": column, "year": f"substr({column}, 1, 4)"}
    return {"month": f"substr({column}, 1, 7)", "year": f"substr({column}, 1, 4)",
            "quarter": _QUARTER.format(d=column)}


@dataclass(frozen=True)
class Source:
    name: str
    from_sql: str
    date_column: str
    date_format: str
    dimensions: Mapping[str, str]
    base_filter: str = ""


_GEO = {"region": "r.region_name", "country": "r.country"}

ORDER_LINES = Source(
    "completed order lines",
    "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "JOIN products p ON p.product_id = oi.product_id "
    "JOIN customers c ON c.customer_id = o.customer_id "
    "JOIN regions r ON r.region_id = c.region_id",
    "o.order_date", "YYYY-MM-DD",
    {**_GEO, "segment": "c.segment", "channel": "o.channel",
     "category": "p.category",
     "product": "p.product_name || ' (id ' || p.product_id || ')'",
     **_time_dims("o.order_date")},
    base_filter="o.status = 'completed'",
)
ALL_ORDERS = Source(
    "orders (any status)",
    "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
    "JOIN regions r ON r.region_id = c.region_id",
    "o.order_date", "YYYY-MM-DD",
    {**_GEO, "segment": "c.segment", "channel": "o.channel",
     **_time_dims("o.order_date")},
)
CUSTOMERS = Source(
    "customers",
    "FROM customers c JOIN regions r ON r.region_id = c.region_id",
    "c.churn_date", "YYYY-MM-DD",
    {**_GEO, "segment": "c.segment", **_time_dims("c.churn_date")},
)
TICKETS = Source(
    "support tickets",
    "FROM support_tickets t JOIN customers c ON c.customer_id = t.customer_id "
    "JOIN regions r ON r.region_id = c.region_id",
    "t.created_date", "YYYY-MM-DD",
    {**_GEO, "segment": "c.segment", "severity": "t.severity",
     **_time_dims("t.created_date")},
)
MARKETING = Source(
    "marketing spend",
    "FROM marketing_spend m JOIN regions r ON r.region_id = m.region_id",
    "m.month", "YYYY-MM",
    {**_GEO, "channel": "m.channel", **_time_dims("m.month", month_only=True)},
)

#: Base aggregates: (source, SQL). Formulas follow the data dictionary.
AGGREGATES: dict[str, tuple[Source, str]] = {
    "revenue": (ORDER_LINES, f"SUM({REVENUE_SQL})"),
    "gross_sales": (ORDER_LINES, "SUM(oi.quantity * oi.unit_price)"),
    "units_sold": (ORDER_LINES, "SUM(oi.quantity)"),
    "order_count": (ORDER_LINES, "COUNT(DISTINCT o.order_id)"),
    "buying_customers": (ORDER_LINES, "COUNT(DISTINCT o.customer_id)"),
    "cost": (ORDER_LINES, "SUM(oi.quantity * p.unit_cost)"),
    "gross_margin": (
        ORDER_LINES,
        "SUM(oi.quantity * (oi.unit_price * (1 - oi.discount) - p.unit_cost))"),
    "all_orders": (ALL_ORDERS, "COUNT(*)"),
    "refunded_orders": (ALL_ORDERS, "SUM(o.status = 'refunded')"),
    "churned_customers": (CUSTOMERS, "COUNT(c.churn_date)"),
    "customers": (CUSTOMERS, "COUNT(*)"),
    "tickets": (TICKETS, "COUNT(*)"),
    "resolved_tickets": (TICKETS, "COUNT(t.resolved_date)"),
    "resolution_days_total": (
        TICKETS, "SUM(julianday(t.resolved_date) - julianday(t.created_date))"),
    "marketing_spend": (MARKETING, "SUM(m.amount)"),
}

#: Derived factors: name -> (numerator aggregate, denominator aggregate).
FACTORS: dict[str, tuple[str, str | None]] = {
    "revenue": ("revenue", None),
    "order_count": ("order_count", None),
    "units_sold": ("units_sold", None),
    "buying_customers": ("buying_customers", None),
    "cost": ("cost", None),
    "average_order_value": ("revenue", "order_count"),
    "average_unit_price": ("gross_sales", "units_sold"),
    "one_minus_average_discount": ("revenue", "gross_sales"),
    "orders_per_customer": ("order_count", "buying_customers"),
    "units_per_order": ("units_sold", "order_count"),
    "refunded_orders": ("refunded_orders", None),
    "all_orders": ("all_orders", None),
    "gross_margin": ("gross_margin", None),
    "resolved_tickets": ("resolved_tickets", None),
    "resolution_days_total": ("resolution_days_total", None),
    "churned_customers": ("churned_customers", None),
    "customers": ("customers", None),
}


#: Plain-language names of the factors. Decomposition rows use them as their
#: `item`, so an answer written from the table names each component in words
#: rather than as a column name (a why-answer that said "order_count" named
#: the driver in a form no reader or check expects).
LABELS: dict[str, str] = {
    "revenue": "revenue",
    "order_count": "number of orders",
    "units_sold": "units sold",
    "buying_customers": "number of buying customers",
    "cost": "cost",
    "average_order_value": "average order value",
    "average_unit_price": "average unit price",
    "one_minus_average_discount": "1 - average discount",
    "orders_per_customer": "orders per customer",
    "units_per_order": "units per order",
    "refunded_orders": "refunded orders",
    "all_orders": "all orders",
    "gross_margin": "gross margin",
    "resolved_tickets": "resolved tickets",
    "resolution_days_total": "total resolution days",
    "churned_customers": "churned customers",
    "customers": "customers",
    "refund_rate": "refund rate",
    "churn_rate": "churn rate",
    "ticket_resolution_days": "average resolution days",
    "tickets": "support tickets",
    "marketing_spend": "marketing spend",
}


@dataclass(frozen=True)
class Metric:
    name: str
    factor: str
    additive: bool
    definition: str
    products: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    """Multiplicative decompositions: label -> ((factor, exponent), ...)."""
    sums: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    """Additive decompositions: label -> ((factor, sign), ...)."""
    periods_allowed: bool = True
    over_time: str = ""
    """When periods are not allowed: the metric that can compare periods."""


METRICS: dict[str, Metric] = {m.name: m for m in (
    Metric("revenue", "revenue", True,
           "completed revenue: SUM(quantity * unit_price * (1 - discount))",
           products={
               "orders x average order value":
                   (("order_count", 1), ("average_order_value", 1)),
               "units x average price x (1 - average discount)":
                   (("units_sold", 1), ("average_unit_price", 1),
                    ("one_minus_average_discount", 1)),
           }),
    Metric("order_count", "order_count", True, "completed orders",
           products={"buying customers x orders per customer":
                     (("buying_customers", 1), ("orders_per_customer", 1))}),
    Metric("units_sold", "units_sold", True, "units in completed orders",
           products={"orders x units per order":
                     (("order_count", 1), ("units_per_order", 1))}),
    Metric("average_order_value", "average_order_value", False,
           "revenue / completed orders",
           products={
               "units per order x average price x (1 - average discount)":
                   (("units_per_order", 1), ("average_unit_price", 1),
                    ("one_minus_average_discount", 1)),
           }),
    Metric("gross_margin", "gross_margin", True,
           "SUM(quantity * (unit_price * (1 - discount) - unit_cost))",
           sums={"revenue - cost": (("revenue", 1), ("cost", -1))}),
    Metric("refund_rate", "refund_rate", False,
           "refunded orders / all orders (any status)",
           products={"refunded orders / all orders":
                     (("refunded_orders", 1), ("all_orders", -1))}),
    Metric("churned_customers", "churned_customers", True,
           "customers whose churn_date falls in the period"),
    Metric("churn_rate", "churn_rate", False, "churned customers / all customers",
           products={"churned / all customers":
                     (("churned_customers", 1), ("customers", -1))},
           periods_allowed=False, over_time="churned_customers"),
    Metric("tickets", "tickets", True, "support tickets opened"),
    Metric("ticket_resolution_days", "ticket_resolution_days", False,
           "average days from created to resolved, resolved tickets only",
           products={"total resolution days / resolved tickets":
                     (("resolution_days_total", 1), ("resolved_tickets", -1))}),
    Metric("marketing_spend", "marketing_spend", True, "SUM(marketing_spend.amount)"),
)}

FACTORS.update({
    "refund_rate": ("refunded_orders", "all_orders"),
    "churn_rate": ("churned_customers", "customers"),
    "ticket_resolution_days": ("resolution_days_total", "resolved_tickets"),
    "tickets": ("tickets", None),
    "marketing_spend": ("marketing_spend", None),
})


class Period(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Period:
        if self.end < self.start:
            raise ValueError("period end is before its start")
        return self

    def label(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


#: Most groups one comparison may list (the first and up to five others), so
#: the result stays small enough for the answer step to read in full.
MAX_GROUPS = 6


class Comparison(BaseModel):
    """What is compared: side_a is the reference, side_b what is explained.

    Modes: two periods (period_a, then the period_b being explained); two
    groups of one dimension (group_a, then the group_b being explained); or
    `groups`, the group being explained first and then each group it is
    compared with. Group modes take an optional common `period`. Two periods
    together with `groups` compare the change between them across the groups.
    """

    period_a: Period | None = None
    period_b: Period | None = None
    group_dimension: str | None = None
    group_a: str | None = None
    group_b: str | None = None
    groups: list[str] | None = None
    period: Period | None = None
    filters: dict[str, str] = {}

    @model_validator(mode="before")
    @classmethod
    def _dimension_keys(cls, data: object) -> object:
        """Dimension names are allowlist keys: match them case-insensitively."""
        if isinstance(data, dict):
            data = dict(data)
            if isinstance(data.get("group_dimension"), str):
                data["group_dimension"] = _key(data["group_dimension"])
            if isinstance(data.get("filters"), dict):
                data["filters"] = {_key(k): v for k, v in data["filters"].items()}
        return data

    @model_validator(mode="after")
    def _one_mode(self) -> Comparison:
        periods = self.period_a is not None and self.period_b is not None
        if (self.period_a is None) != (self.period_b is None):
            raise ValueError("period_a and period_b go together")
        if self.groups and not self.group_dimension:
            raise ValueError("groups needs group_dimension")
        pair = bool(self.group_a and self.group_b)
        if pair and self.groups:
            raise ValueError("give group_a and group_b, or groups, not both")
        grouped = bool(self.group_dimension and (pair or self.groups))
        across = bool(periods and self.group_dimension and self.groups)
        if periods == grouped and not across:
            raise ValueError("give either period_a and period_b, or "
                             "group_dimension with group_a and group_b (or groups), "
                             "or periods with group_dimension and groups")
        if self.groups is not None and not (
                2 <= len(self.groups) <= MAX_GROUPS
                and len(set(self.groups)) == len(self.groups)):
            raise ValueError(f"groups needs 2 to {MAX_GROUPS} different values")
        return self

    @property
    def by_period(self) -> bool:
        return self.period_a is not None

    @property
    def across_groups(self) -> bool:
        """The change between two periods, compared across groups."""
        return self.period_a is not None and bool(self.groups)

    @property
    def explained_period(self) -> Period | None:
        """The period the comparison asks about, if it names one."""
        return self.period_b or self.period

    def sides(self) -> list[tuple[str, Period | None, dict[str, str]]]:
        """Every distinct side once: (label, period, filters)."""
        if self.by_period:
            return [(self.period_a.label(), self.period_a, dict(self.filters)),
                    (self.period_b.label(), self.period_b, dict(self.filters))]
        dim = self.group_dimension
        names = self.groups or [self.group_a, self.group_b]
        return [(g, self.period, {**self.filters, dim: g}) for g in names]

    def pairs(self) -> list[tuple[int, int]]:
        """(reference, explained) indexes into sides()."""
        if self.groups:
            return [(i, 0) for i in range(1, len(self.groups))]
        return [(0, 1)]


Runner = Callable[[str], ExecutionResult]


def _literal(value: str) -> str:
    return exp.Literal.string(str(value)).sql(dialect="sqlite")


def _bounds(source: Source, period: Period) -> tuple[str, str]:
    if source.date_format == "YYYY-MM":
        return period.start.isoformat()[:7], period.end.isoformat()[:7]
    return period.start.isoformat(), period.end.isoformat()


def _key(name: object) -> str:
    """Allowlist key of a model-supplied metric or dimension name: case and
    surrounding spaces do not matter; the key must still be listed."""
    return str(name).strip().lower()


def _dimension(source: Source, name: str) -> str:
    name = _key(name)
    if name not in source.dimensions:
        raise ComputeError(
            f"dimension {name!r} is not available for {source.name}; "
            f"choose from {sorted(source.dimensions)}")
    return source.dimensions[name]


def _where(source: Source, period: Period | None, filters: Mapping[str, str]) -> str:
    clauses = [source.base_filter] if source.base_filter else []
    if period is not None:
        lo, hi = _bounds(source, period)
        clauses.append(
            f"{source.date_column} BETWEEN {_literal(lo)} AND {_literal(hi)}")
    for dim, value in filters.items():
        clauses.append(f"{_dimension(source, dim)} = {_literal(value)}")
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def _aggregates_for(names: Sequence[str]) -> dict[str, str]:
    """Aggregates the factors need, in the factors' order. A sequence, never a
    set: set order follows string hashing, which changes per process, and the
    SQL text (shown to the model) must be the same in every run."""
    needed: dict[str, str] = {}
    for name in names:
        numerator, denominator = FACTORS[name]
        for agg in (numerator, denominator):
            if agg:
                needed[agg] = AGGREGATES[agg][1]
    return needed


def _source_of(metric: Metric) -> Source:
    numerator, _ = FACTORS[metric.factor]
    return AGGREGATES[numerator][0]


def build_sql(source: Source, aggregates: Mapping[str, str], period: Period | None,
              filters: Mapping[str, str], dimension: str | None = None) -> str:
    select = ", ".join(f"{sql} AS {name}" for name, sql in aggregates.items())
    where = _where(source, period, filters)
    if dimension:
        dim = _dimension(source, dimension)
        return (f"SELECT {dim} AS {dimension}, {select} {source.from_sql} {where} "
                f"GROUP BY 1 ORDER BY 1")
    return f"SELECT {select} {source.from_sql} {where}"


def _value(name: str, row: Mapping[str, float]) -> float:
    numerator, denominator = FACTORS[name]
    top = float(row.get(numerator) or 0.0)
    if denominator is None:
        return top
    bottom = float(row.get(denominator) or 0.0)
    if bottom == 0:
        raise ComputeError(f"{name} is undefined: {denominator} is zero")
    return top / bottom


def _run(run: Runner, sql: str) -> list[dict[str, float]]:
    result = run(sql)
    if not result.ok:
        raise ComputeError(f"query failed: {result.error_message}")
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def _metric(name: str) -> Metric:
    name = _key(name)
    if name not in METRICS:
        raise ComputeError(f"unknown metric {name!r}; choose from {sorted(METRICS)}")
    return METRICS[name]


def _check_periods(metric: Metric) -> None:
    if metric.periods_allowed:
        return
    instead = (f", or use {metric.over_time} with contribution_by_dimension to "
               "compare periods" if metric.over_time else "")
    raise ComputeError(f"{metric.name} cannot be split by period; "
                       f"compare groups{instead}")


def _check(metric: Metric, comparison: Comparison) -> Source:
    source = _source_of(metric)
    if comparison.by_period or comparison.period:
        _check_periods(metric)
    for dim in [*comparison.filters, comparison.group_dimension]:
        if dim:
            _dimension(source, dim)
    return source


#: Output columns of both metric tools. One row compares one item between
#: side_a (the reference) and side_b (what is explained); a shared schema lets
#: several analyses be stacked into one final table.
COLUMNS = ("side_a", "side_b", "analysis", "item", "value_a", "value_b", "change",
           "pct_change", "share_of_change_pct", "comparison", "largest_contributor")

#: An item that differs by at most this many percent between the two sides is
#: labelled "about the same": it cannot explain a gap much larger than that.
#: Labelling it here means the answer step reports it instead of judging
#: closeness (or skipping the item) itself.
SIMILAR_PCT = 5.0


def _pct(a: float, b: float) -> float | None:
    return (b - a) / abs(a) * 100 if a else None


def _label(a: float, b: float) -> str:
    pct = _pct(a, b)
    close = abs(pct) <= SIMILAR_PCT if pct is not None else a == b
    if close:
        return "about the same"
    return "higher" if b > a else "lower"


def _record(labels: tuple[str, str], analysis: str, item: object, a: float,
            b: float, share: float | None) -> dict[str, object]:
    return {"side_a": labels[0], "side_b": labels[1], "analysis": analysis,
            "item": item, "value_a": a, "value_b": b, "change": b - a,
            "pct_change": _pct(a, b), "share_of_change_pct": share,
            "comparison": _label(a, b), "largest_contributor": False}


def _with_largest(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Mark the item with the largest share of the change (if any is known)."""
    known = [i for i, r in enumerate(records) if r["share_of_change_pct"] is not None]
    top = max(known, key=lambda i: records[i]["share_of_change_pct"]) if known else None
    return [{**r, "largest_contributor": i == top} for i, r in enumerate(records)]


def _reconcile(metric: Metric, side: Mapping[str, float]) -> None:
    """The components of every decomposition must rebuild the metric."""
    target = side[metric.factor]
    tolerance = RECONCILE_TOLERANCE * max(1.0, abs(target))
    for label, parts in metric.products.items():
        if abs(math.prod(side[f] ** e for f, e in parts) - target) > tolerance:
            raise ComputeError(f"components of {label!r} do not reconcile")
    for label, parts in metric.sums.items():
        if abs(sum(s * side[f] for f, s in parts) - target) > tolerance:
            raise ComputeError(f"components of {label!r} do not reconcile")


def _decomposition_rows(metric: Metric, labels: tuple[str, str],
                        a: Mapping[str, float], b: Mapping[str, float]
                        ) -> list[dict[str, object]]:
    total_log = _log_ratio(a[metric.factor], b[metric.factor])
    total_change = b[metric.factor] - a[metric.factor]
    records = [_record(labels, "metric", LABELS[metric.factor], a[metric.factor],
                       b[metric.factor], None)]
    for analysis, parts in metric.products.items():
        group = []
        for f, e in parts:
            # Log contributions: exponent x ln(b/a) over ln(metric_b/metric_a);
            # they add up to 100% across one decomposition's components.
            ratio = _log_ratio(a[f], b[f])
            share = (e * ratio / total_log * 100
                     if ratio is not None and total_log else None)
            item = LABELS[f] if e == 1 else f"{LABELS[f]} (divides)"
            group.append(_record(labels, analysis, item, a[f], b[f], share))
        records += _with_largest(group)
    for analysis, parts in metric.sums.items():
        group = [
            _record(labels, analysis,
                    LABELS[f] if s == 1 else f"{LABELS[f]} (subtracted)", a[f], b[f],
                    s * (b[f] - a[f]) / total_change * 100 if total_change else None)
            for f, s in parts
        ]
        records += _with_largest(group)
    return records


def _check_decompose(metric_name: str, comparison: Comparison
                     ) -> tuple[Metric, Source]:
    metric = _metric(metric_name)
    if not (metric.products or metric.sums):
        raise ComputeError(f"{metric.name} has no component decomposition; "
                           "use contribution_by_dimension")
    return metric, _check(metric, comparison)


def _check_contribution(metric_name: str, dimension: str, comparison: Comparison
                        ) -> tuple[Metric, Source]:
    metric = _metric(metric_name)
    if not metric.additive:
        raise ComputeError(f"{metric.name} is a ratio; contributions are only "
                           "defined for additive metrics (sums and counts)")
    if comparison.across_groups:
        raise ComputeError("contribution_by_dimension compares two sides; use "
                           "decompose_change to compare a change across groups")
    source = _check(metric, comparison)
    _dimension(source, dimension)
    return metric, source


def validate_call(function: str, metric_name: str, comparison: Comparison,
                  dimension: str | None = None) -> None:
    """Raise ComputeError if a metric tool would refuse this call. Plans run it
    when they are parsed, so the structured-output retry can fix a refused call
    before it costs a step; the tools run the same checks themselves."""
    if function == "decompose_change":
        _check_decompose(metric_name, comparison)
    else:
        _check_contribution(metric_name, dimension or "", comparison)


#: Rows comparing one component's change between the group being explained and
#: the others start their `analysis` with this.
GROUP_GAP_PREFIX = "change against other groups: "


#: One group's figures in the two periods.
Change = tuple[Mapping[str, float], Mapping[str, float]]


def _change_part(kind: str, metric: Metric, change: Change, factor: str,
                 sign: int = 1) -> float | None:
    """A factor's part of the metric's change, on a scale where a decomposition's
    parts add up to the metric's own: log ratios for a product, changes relative
    to the metric's starting value for a sum."""
    a, b = change
    if kind == "products":
        ratio = _log_ratio(a[factor], b[factor])
        return None if ratio is None else sign * ratio
    start = abs(a[metric.factor])
    return sign * (b[factor] - a[factor]) / start if start else None


def _mean(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def _excess(of: Callable[[Change], float | None], changes: Mapping[str, Change]
            ) -> float | None:
    """`of` the first group minus its mean over the others."""
    subject, *others = changes
    mine, theirs = of(changes[subject]), _mean([of(changes[g]) for g in others])
    return None if mine is None or theirs is None else mine - theirs


def _gap_record(metric: Metric, kind: str, analysis: str, part: tuple[str, int],
                dimension: str, changes: Mapping[str, Change]) -> dict[str, object]:
    """One component: its percent change in the first group against the average
    of the others, and its share of the first group's extra change in the metric."""
    factor, sign = part
    subject, *others = changes

    def pct(change: Change) -> float | None:
        return _pct(change[0][factor], change[1][factor])

    gap = _excess(pct, changes)
    whole = _excess(lambda c: _change_part(kind, metric, c, metric.factor), changes)
    own = _excess(lambda c: _change_part(kind, metric, c, factor, sign), changes)
    suffix = {("products", -1): " (divides)", ("sums", -1): " (subtracted)"}
    return {"side_a": f"other {dimension} values (average)", "side_b": subject,
            "analysis": f"{GROUP_GAP_PREFIX}{analysis}",
            "item": LABELS[factor] + suffix.get((kind, sign), ""),
            "value_a": _mean([pct(changes[g]) for g in others]),
            "value_b": pct(changes[subject]),
            "change": gap, "pct_change": None,
            "share_of_change_pct": (own / whole * 100
                                    if own is not None and whole else None),
            "comparison": ("about the same" if gap is None or abs(gap) <= SIMILAR_PCT
                           else "higher" if gap > 0 else "lower"),
            "largest_contributor": False}


def _across_groups_rows(metric: Metric, dimension: str, changes: Mapping[str, Change],
                        labels: tuple[str, str]) -> list[dict[str, object]]:
    """What differs most between the first group's change and the others',
    component by component, then each group's own figures. Added after a holdout
    failure (ha07): a decomposition of one group says why it fell, and the same
    component can be the largest part of every group's fall, so it cannot be why
    one group fell more."""
    records: list[dict[str, object]] = []
    for kind, decompositions in (("products", metric.products), ("sums", metric.sums)):
        for analysis, parts in decompositions.items():
            records += _with_largest([
                _gap_record(metric, kind, analysis, part, dimension, changes)
                for part in parts])
    for group, (a, b) in changes.items():
        records += [{**row, "analysis": f"{group}: {row['analysis']}",
                     "largest_contributor": False}
                    for row in _decomposition_rows(metric, labels, a, b)]
    return records


def decompose_change(
    run: Runner, metric_name: str, comparison: Comparison
) -> tuple[pd.DataFrame, list[str]]:
    metric, source = _check_decompose(metric_name, comparison)
    # Declaration order (the metric, then each decomposition's components).
    factors = list(dict.fromkeys(
        [metric.factor] + [name for parts in [*metric.products.values(),
                                              *metric.sums.values()]
                           for name, _ in parts]))
    aggregates = _aggregates_for(factors)
    queries: list[str] = []

    def read(label: str, period: Period | None, filters: Mapping[str, str]
             ) -> dict[str, float]:
        sql = build_sql(source, aggregates, period, filters)
        queries.append(sql)
        (row,) = _run(run, sql) or [{}]
        if not any(row.get(k) for k in aggregates):
            raise ComputeError(f"no data for {label!r}")
        side = {f: _value(f, row) for f in factors}
        _reconcile(metric, side)
        return side

    if comparison.across_groups:
        first, second = comparison.period_a, comparison.period_b
        changes = {}
        for group in comparison.groups:
            filters = {**comparison.filters, comparison.group_dimension: group}
            changes[group] = (read(f"{group} in {first.label()}", first, filters),
                              read(f"{group} in {second.label()}", second, filters))
        records = _across_groups_rows(metric, comparison.group_dimension, changes,
                                      (first.label(), second.label()))
        return pd.DataFrame(records, columns=list(COLUMNS)), queries

    sides = [(label, read(label, period, filters))
             for label, period, filters in comparison.sides()]
    records = []
    for i, j in comparison.pairs():
        (label_a, a), (label_b, b) = sides[i], sides[j]
        records += _decomposition_rows(metric, (label_a, label_b), a, b)
    return pd.DataFrame(records, columns=list(COLUMNS)), queries


def _log_ratio(a: float, b: float) -> float | None:
    return math.log(b / a) if a > 0 and b > 0 else None


def _contribution_rows(analysis: str, side_a: tuple[str, dict], side_b: tuple[str, dict]
                       ) -> list[dict[str, object]]:
    (label_a, a), (label_b, b) = side_a, side_b
    keys = sorted(set(a) | set(b), key=str)
    total = sum(b.get(k, 0.0) - a.get(k, 0.0) for k in keys)
    records = [
        _record((label_a, label_b), analysis, k, a.get(k, 0.0), b.get(k, 0.0),
                (b.get(k, 0.0) - a.get(k, 0.0)) / total * 100 if total else None)
        for k in keys
    ]
    # Largest contributor: the largest change in the direction of the total.
    direction = 1.0 if total >= 0 else -1.0
    ordered = sorted(records, key=lambda r: -direction * r["change"])
    return [{**r, "largest_contributor": i == 0} for i, r in enumerate(ordered)]


def contribution_by_dimension(
    run: Runner, metric_name: str, dimension: str, comparison: Comparison
) -> tuple[pd.DataFrame, list[str]]:
    metric, source = _check_contribution(metric_name, dimension, comparison)
    dimension = _key(dimension)
    aggregates = _aggregates_for([metric.factor])
    tables, queries = [], []
    for label, period, filters in comparison.sides():
        sql = build_sql(source, aggregates, period, filters, dimension=dimension)
        queries.append(sql)
        values = {row[dimension]: _value(metric.factor, row) for row in _run(run, sql)}
        if not values:
            raise ComputeError(f"no data for {label!r}")
        tables.append((label, values))
    analysis = f"{metric.name} by {dimension}"
    records = []
    for i, j in comparison.pairs():
        records += _contribution_rows(analysis, tables[i], tables[j])
    return pd.DataFrame(records, columns=list(COLUMNS)), queries


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


def has_components(metric_name: str) -> bool:
    """True if decompose_change can split the metric into components."""
    metric = _metric(metric_name)
    return bool(metric.products or metric.sums)


def catalog() -> str:
    """Metrics, dimensions and what each tool allows, for the planning prompts."""
    lines = []
    for metric in METRICS.values():
        source = _source_of(metric)
        tools = []
        if metric.products or metric.sums:
            tools.append(
                "decompose_change" if metric.periods_allowed else
                "decompose_change (between groups only, not between periods; to "
                f"compare periods use {metric.over_time})")
        if metric.additive:
            tools.append("contribution_by_dimension")
        lines.append(
            f"- {metric.name} ({metric.definition}); dimensions: "
            f"{', '.join(sorted(source.dimensions))}; tools: {', '.join(tools)}")
    return "\n".join(lines)
