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
period. Optional `filters` restrict both sides to dimension values.

Both tools return the same columns (COLUMNS): one row per item compared
between side_a (the reference) and side_b (what is explained), with the
change, its share of the metric's change, a comparison label ("higher",
"lower", or "about the same" within SIMILAR_PCT percent) and the largest
contributor marked, so every component's comparison is stated by the tool,
including the components that do not move.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
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


#: Most groups one comparison may list (the first and up to five others), so
#: the result stays small enough for the answer step to read in full.
MAX_GROUPS = 6


class Comparison(BaseModel):
    """What is compared: side_a is the reference, side_b what is explained.

    Modes: two periods (period_a, then the period_b being explained); two
    groups of one dimension (group_a, then the group_b being explained); or
    `groups`, the group being explained first and then each group it is
    compared with. Group modes take an optional common `period`.
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
        pair = bool(self.group_a and self.group_b)
        if pair and self.groups:
            raise ValueError("give group_a and group_b, or groups, not both")
        grouped = bool(self.group_dimension and (pair or self.groups))
        if periods == grouped:
            raise ValueError("give either period_a and period_b, or "
                             "group_dimension with group_a and group_b (or groups)")
        if self.groups is not None and not (
                2 <= len(self.groups) <= MAX_GROUPS
                and len(set(self.groups)) == len(self.groups)):
            raise ValueError(f"groups needs 2 to {MAX_GROUPS} different values")
        return self

    @property
    def by_period(self) -> bool:
        return self.period_a is not None

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


def _check(metric: Metric, comparison: Comparison) -> Source:
    source = _source_of(metric)
    if not metric.periods_allowed and (comparison.by_period or comparison.period):
        raise ComputeError(f"{metric.name} cannot be split by period; compare groups")
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


def decompose_change(
    run: Runner, metric_name: str, comparison: Comparison
) -> tuple[pd.DataFrame, list[str]]:
    metric = _metric(metric_name)
    if not (metric.products or metric.sums):
        raise ComputeError(f"{metric.name} has no component decomposition; "
                           "use contribution_by_dimension")
    source = _check(metric, comparison)
    # Declaration order (the metric, then each decomposition's components).
    factors = list(dict.fromkeys(
        [metric.factor] + [name for parts in [*metric.products.values(),
                                              *metric.sums.values()]
                           for name, _ in parts]))
    aggregates = _aggregates_for(factors)

    sides, queries = [], []
    for label, period, filters in comparison.sides():
        sql = build_sql(source, aggregates, period, filters)
        queries.append(sql)
        (row,) = _run(run, sql) or [{}]
        if not any(row.get(k) for k in aggregates):
            raise ComputeError(f"no data for {label!r}")
        side = {f: _value(f, row) for f in factors}
        _reconcile(metric, side)
        sides.append((label, side))

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
    metric = _metric(metric_name)
    if not metric.additive:
        raise ComputeError(f"{metric.name} is a ratio; contributions are only "
                           "defined for additive metrics (sums and counts)")
    source = _check(metric, comparison)
    dimension = _key(dimension)
    _dimension(source, dimension)
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


def has_components(metric_name: str) -> bool:
    """True if decompose_change can split the metric into components."""
    metric = _metric(metric_name)
    return bool(metric.products or metric.sums)


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
