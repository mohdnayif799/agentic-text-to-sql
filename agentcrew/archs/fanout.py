"""Zero-LLM join fan-out check (an escalation signal for C and D).

Flags a query whose SELECT aggregates (SUM, AVG or COUNT, not DISTINCT)
after joining two tables on a key that is not unique on either side, so
every aggregate is multiplied. Example: order lines joined to marketing
spend through region. Each spend row matches every order line of its
region, and vice versa.

Uniqueness is judged against the rows accumulated so far, not just the
table: after a one-to-many join (orders -> order lines), the rows of the
earlier tables repeat. So a key that is unique in its own table (a region's
id) is no longer unique in the joined rows. Key uniqueness in each table is
read from the database once and cached.

Derived tables (subqueries) are assumed unique on their join keys, and
joins without a condition are ignored, to avoid false alarms.
"""

from __future__ import annotations

from functools import lru_cache

import sqlglot
from sqlalchemy import Engine, text
from sqlglot import exp
from sqlglot.errors import ParseError

from agentcrew.db.introspect import Catalog


class KeyIndex:
    """Is a set of columns unique in a table? Answered from the data, cached."""

    def __init__(self, engine: Engine, catalog: Catalog) -> None:
        self._engine = engine
        self._columns = {
            name: {c.name.lower(): c.name for c in table.columns}
            for name, table in catalog.tables.items()
        }
        self.unique = lru_cache(maxsize=512)(self._unique)

    def table_with_column(self, column: str, tables: list[str]) -> list[str]:
        return [t for t in tables if column.lower() in self._columns.get(t, {})]

    def _unique(self, table: str, columns: frozenset[str]) -> bool:
        known = self._columns.get(table)
        if not known or not columns:
            return True  # unknown table: assume unique (no false alarm)
        names = [known.get(c.lower()) for c in columns]
        if None in names:
            return True
        cols = ", ".join(f'"{n}"' for n in sorted(names))
        with self._engine.connect() as conn:
            # Names come from introspection of this database, not input.
            distinct = conn.execute(text(
                f'SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM "{table}")'  # noqa: S608
            )).scalar_one()
            total = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()  # noqa: S608
        return distinct == total


def _multiplying_aggregate(select: exp.Select) -> bool:
    for projection in select.expressions:
        for agg in projection.find_all(exp.AggFunc):
            if agg.find_ancestor(exp.Select) is not select:
                continue
            if isinstance(agg, exp.Min | exp.Max):
                continue
            if isinstance(agg.this, exp.Distinct):
                continue
            return True
    return False


def _source(node: exp.Expression, sources: dict[str, str | None],
            ctes: set[str]) -> str | None:
    if isinstance(node, exp.Table):
        alias = node.alias_or_name
        # A CTE reference is a derived table, not a database table.
        sources[alias] = None if node.name.lower() in ctes else node.name
        return alias
    if isinstance(node, exp.Subquery):
        alias = node.alias_or_name
        sources[alias] = None  # derived table
        return alias
    return None


def _alias_of(col: exp.Column, scope: list[str], sources: dict[str, str | None],
              keys: KeyIndex) -> str | None:
    if col.table:
        return col.table
    candidates = [a for a in scope if sources.get(a)
                  and keys.table_with_column(col.name, [sources[a]])]
    return candidates[0] if len(candidates) == 1 else None


def _side(node: exp.Expression, right: str, joined: list[str],
          sources: dict[str, str | None], keys: KeyIndex) -> tuple:
    """Classify one side of an equality.

    ("col", alias, column)  a plain column
    ("expr", alias)         an expression over one table's columns
    ("const",)              no columns at all (a filter value)
    ("mixed",)              columns from several tables, or unresolved
    """
    scope = [*joined, right]
    if isinstance(node, exp.Column):
        alias = _alias_of(node, scope, sources, keys)
        return ("col", alias, node.name) if alias else ("mixed",)
    columns = list(node.find_all(exp.Column))
    if not columns:
        return ("const",)
    aliases = {_alias_of(c, scope, sources, keys) for c in columns}
    return ("expr", aliases.pop()) if len(aliases) == 1 and None not in aliases \
        else ("mixed",)


def _unique_side(alias: str, cols: set[str], has_expr: bool,
                 sources: dict[str, str | None], keys: KeyIndex) -> bool:
    if sources.get(alias) is None:
        return True  # derived table: assume unique on its join keys
    if has_expr:
        return False  # an expression of a table's columns is not a known key
    return keys.unique(sources[alias], frozenset(cols))


def _scope_problem(select: exp.Select, keys: KeyIndex, ctes: set[str]) -> str | None:
    # sqlglot >= 30 stores FROM under "from_"; older versions under "from".
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    sources: dict[str, str | None] = {}
    first = _source(from_.this, sources, ctes)
    if first is None:
        return None
    joined, repeated = [first], set()
    for join in select.args.get("joins") or []:
        right = _source(join.this, sources, ctes)
        on = join.args.get("on")
        if right is None or on is None:
            if right:
                joined.append(right)
            continue
        right_cols: set[str] = set()
        right_expr = False
        left_parts: list[tuple] = []
        for eq in ([on] if isinstance(on, exp.EQ) else list(on.find_all(exp.EQ))):
            a = _side(eq.left, right, joined, sources, keys)
            b = _side(eq.right, right, joined, sources, keys)
            for mine, other in ((a, b), (b, a)):
                if mine[0] in ("col", "expr") and mine[1] == right \
                        and other[0] in ("col", "expr", "mixed") \
                        and not (other[0] != "mixed" and other[1] == right):
                    if mine[0] == "col":
                        right_cols.add(mine[2])
                    else:
                        right_expr = True
                    left_parts.append(other)
                    break
        if not left_parts:  # only filters against constants: not a key join
            joined.append(right)
            continue
        right_unique = _unique_side(right, right_cols, right_expr, sources, keys)
        left_aliases = {p[1] for p in left_parts if p[0] in ("col", "expr")}
        if any(p[0] == "mixed" for p in left_parts) or len(left_aliases) != 1:
            left_unique = False
        else:
            alias = next(iter(left_aliases))
            left_cols = {p[2] for p in left_parts if p[0] == "col"}
            left_expr = any(p[0] == "expr" for p in left_parts)
            left_unique = alias not in repeated and _unique_side(
                alias, left_cols, left_expr, sources, keys)
        if not right_unique and not left_unique:
            condition = on.sql(dialect="sqlite")
            return (
                f"Join fan-out: {right} is joined on {condition}, a key that is "
                "not unique on either side, so SUM/COUNT/AVG values are "
                "multiplied. Aggregate each table separately (for example in "
                "subqueries) before combining them."
            )
        if not right_unique:
            repeated.update(joined)
        if not left_unique:
            repeated.add(right)
        joined.append(right)
    return None


def fanout_problem(sql: str, keys: KeyIndex) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except ParseError:
        return None
    if tree is None:
        return None
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for select in tree.find_all(exp.Select):
        if _multiplying_aggregate(select):
            problem = _scope_problem(select, keys, ctes)
            if problem:
                return problem
    return None
