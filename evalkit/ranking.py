"""Is a result ranked by a metric? (protocol v2, rule 3 for which/top-N.)

A which/top-N question may be answered by a full ranking instead of exactly N
rows, but only if the OUTERMOST query is ORDERed BY the ranking metric. That
is what makes "the first N rows" mean "the top N" rather than an accident of
row order. Checked statically with sqlglot:

* ORDER BY a result column (alias, name or position) whose values are all
  numeric, or
* ORDER BY a computed expression (an aggregate or arithmetic).

ORDER BY a label, no ORDER BY, or ORDER BY only inside a subquery is not a
ranking.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlglot
from sqlglot import exp

_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    try:
        float(str(value))
    except ValueError:
        return False
    return True


def _numeric_column(rows: Sequence[Sequence[Any]], index: int) -> bool:
    values = [r[index] for r in rows if r[index] is not None]
    return bool(values) and all(_is_number(v) for v in values)


def _result_column(
    key: exp.Expression, tree: exp.Expression, columns: Sequence[str]
) -> int | None:
    if isinstance(key, exp.Literal) and key.is_int:
        position = int(key.this) - 1
        return position if 0 <= position < len(columns) else None
    if isinstance(key, exp.Column):
        name = key.name.lower()
        for i, column in enumerate(columns):
            if column.lower() == name:
                return i
        for i, select in enumerate(getattr(tree, "selects", []) or []):
            if select.alias_or_name.lower() == name and i < len(columns):
                return i
    return None


def ranks_by_metric(
    sql: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> tuple[bool, str]:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.ParseError as exc:
        return False, f"SQL could not be parsed: {str(exc)[:80]}"
    order = tree.args.get("order")
    if order is None or not order.expressions:
        return False, "outermost query has no ORDER BY"
    first = order.expressions[0]
    key = first.this if isinstance(first, exp.Ordered) else first

    index = _result_column(key, tree, columns)
    if index is not None:
        if _numeric_column(rows, index):
            return True, f"ranked by result column {columns[index]!r}"
        return False, f"ordered by non-numeric column {columns[index]!r}"
    if key.find(exp.AggFunc) is not None or isinstance(key, _ARITHMETIC):
        return True, f"ranked by computed metric {key.sql(dialect='sqlite')[:60]}"
    return False, f"ORDER BY {key.sql(dialect='sqlite')[:60]} is not a metric"
