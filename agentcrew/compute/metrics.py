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

Comparisons: two periods (period_a, period_b), or two groups of one
dimension (group_dimension, group_a, group_b) over an optional common
period. Optional `filters` restrict both sides to dimension values.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

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
           periods_allowed=False),
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


class Comparison(BaseModel):
    """Two periods, or two groups of one dimension (optionally within a period)."""

    period_a: Period | None = None
    period_b: Period | None = None
    group_dimension: str | None = None
    group_a: str | None = None
    group_b: str | None = None
    period: Period | None = None
    filters: dict[str, str] = {}

    @model_validator(mode="after")
    def _one_mode(self) -> Comparison:
        periods = self.period_a is not None and self.period_b is not None
        groups = bool(self.group_dimension and self.group_a and self.group_b)
        if periods == groups:
            raise ValueError("give either period_a and period_b, or "
                             "group_dimension with group_a and group_b")
        return self

    @property
    def by_period(self) -> bool:
        return self.period_a is not None

    def sides(self) -> list[tuple[str, Period | None, dict[str, str]]]:
        if self.by_period:
            return [(self.period_a.label(), self.period_a, dict(self.filters)),
                    (self.period_b.label(), self.period_b, dict(self.filters))]
        dim = self.group_dimension
        return [
            (self.group_a, self.period, {**self.filters, dim: self.group_a}),
            (self.group_b, self.period, {**self.filters, dim: self.group_b}),
        ]


Runner = Callable[[str], ExecutionResult]


def _literal(value: str) -> str:
    return exp.Literal.string(str(value)).sql(dialect="sqlite")


def _bounds(source: Source, period: Period) -> tuple[str, str]:
    if source.date_format == "YYYY-MM":
        return period.start.isoformat()[:7], period.end.isoformat()[:7]
    return period.start.isoformat(), period.end.isoformat()


def _dimension(source: Source, name: str) -> str:
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


def _aggregates_for(names: set[str]) -> dict[str, str]:
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
    if name not in METRICS:
        raise ComputeError(f"unknown metric {name!r}; choose from {sorted(METRICS)}")
    return METRICS[name]


def _check(metric: Metric, comparison: Comparison) -> Source:
    source = _source_of(metric)
    if not metric.periods_allowed and (comparison.by_period or comparison.period):
        raise ComputeError(f"{metric.name} cannot be split by period; compare groups")
    for dim in [*comparison.filters, comparison.group_dimension]:
        if dim:
            _dimension(source, dim)
    return source


def _pct(a: float, b: float) -> float | None:
    return (b - a) / abs(a) * 100 if a else None


def decompose_change(
    run: Runner, metric_name: str, comparison: Comparison
) -> tuple[pd.DataFrame, list[str]]:
    metric = _metric(metric_name)
    if not (metric.products or metric.sums):
        raise ComputeError(f"{metric.name} has no component decomposition; "
                           "use contribution_by_dimension")
    source = _check(metric, comparison)
    factors = {metric.factor}
    for parts in [*metric.products.values(), *metric.sums.values()]:
        factors.update(name for name, _ in parts)
    aggregates = _aggregates_for(factors)

    sides, queries = [], []
    for label, period, filters in comparison.sides():
        sql = build_sql(source, aggregates, period, filters)
        queries.append(sql)
        (row,) = _run(run, sql) or [{}]
        if not any(row.get(k) for k in aggregates):
            raise ComputeError(f"no data for {label!r}")
        sides.append((label, {f: _value(f, row) for f in factors}))
    (label_a, a), (label_b, b) = sides

    rows = [["metric", metric.name, a[metric.factor], b[metric.factor]]]
    for label, parts in metric.products.items():
        for side in (a, b):
            product = math.prod(side[f] ** e for f, e in parts)
            if abs(product - side[metric.factor]) > RECONCILE_TOLERANCE * max(
                    1.0, abs(side[metric.factor])):
                raise ComputeError(f"components of {label!r} do not reconcile")
        rows += [[label, f if e == 1 else f"{f} (divides)", a[f], b[f]]
                 for f, e in parts]
    for label, parts in metric.sums.items():
        for side in (a, b):
            total = sum(s * side[f] for f, s in parts)
            if abs(total - side[metric.factor]) > RECONCILE_TOLERANCE * max(
                    1.0, abs(side[metric.factor])):
                raise ComputeError(f"components of {label!r} do not reconcile")
        rows += [[label, f if s == 1 else f"minus {f}", s * a[f], s * b[f]]
                 for f, s in parts]

    out = pd.DataFrame(
        rows, columns=["decomposition", "component", "value_a", "value_b"])
    out["change"] = out["value_b"] - out["value_a"]
    out["pct_change"] = [_pct(x, y) for x, y in zip(out["value_a"], out["value_b"],
                                                    strict=True)]
    total_log = _log_ratio(a[metric.factor], b[metric.factor])
    total_change = b[metric.factor] - a[metric.factor]
    shares = []
    for deco, comp, x, y, change in zip(out["decomposition"], out["component"],
                                        out["value_a"], out["value_b"],
                                        out["change"], strict=True):
        if deco in metric.products:
            # Log contributions: exponent x ln(b/a) over ln(metric_b/metric_a);
            # they add up to 100% across one decomposition's components.
            sign = -1 if comp.endswith("(divides)") else 1
            ratio = _log_ratio(x, y)
            shares.append(sign * ratio / total_log * 100
                          if ratio is not None and total_log else None)
        elif deco in metric.sums:
            shares.append(change / total_change * 100 if total_change else None)
        else:
            shares.append(None)
    out["share_of_change_pct"] = shares
    out.insert(0, "side_b", label_b)
    out.insert(0, "side_a", label_a)
    return out, queries


def _log_ratio(a: float, b: float) -> float | None:
    return math.log(b / a) if a > 0 and b > 0 else None


def contribution_by_dimension(
    run: Runner, metric_name: str, dimension: str, comparison: Comparison
) -> tuple[pd.DataFrame, list[str]]:
    metric = _metric(metric_name)
    if not metric.additive:
        raise ComputeError(f"{metric.name} is a ratio; contributions are only "
                           "defined for additive metrics (sums and counts)")
    source = _check(metric, comparison)
    _dimension(source, dimension)
    aggregates = _aggregates_for({metric.factor})
    tables, queries = [], []
    for label, period, filters in comparison.sides():
        sql = build_sql(source, aggregates, period, filters, dimension=dimension)
        queries.append(sql)
        values = {row[dimension]: _value(metric.factor, row) for row in _run(run, sql)}
        if not values:
            raise ComputeError(f"no data for {label!r}")
        tables.append((label, values))
    (label_a, a), (label_b, b) = tables
    keys = sorted(set(a) | set(b), key=str)
    out = pd.DataFrame({
        dimension: keys,
        "value_a": [a.get(k, 0.0) for k in keys],
        "value_b": [b.get(k, 0.0) for k in keys],
    })
    out["change"] = out["value_b"] - out["value_a"]
    out["pct_change"] = [_pct(x, y) for x, y in zip(out["value_a"], out["value_b"],
                                                    strict=True)]
    total = float(out["change"].sum())
    out["share_of_change_pct"] = out["change"] / total * 100 if total else None
    direction = 1.0 if total >= 0 else -1.0
    out = out.sort_values("change", ascending=direction < 0).reset_index(drop=True)
    out["largest_contributor"] = [i == 0 for i in range(len(out))]
    out.insert(0, "side_b", label_b)
    out.insert(0, "side_a", label_a)
    return out, queries


def catalog() -> str:
    """Metrics and dimensions, for the decide_next_action prompt."""
    lines = []
    for metric in METRICS.values():
        source = _source_of(metric)
        tools = ["decompose_change"] if (metric.products or metric.sums) else []
        if metric.additive:
            tools.append("contribution_by_dimension")
        lines.append(
            f"- {metric.name} ({metric.definition}); dimensions: "
            f"{', '.join(sorted(source.dimensions))}; tools: {', '.join(tools)}")
    return "\n".join(lines)
