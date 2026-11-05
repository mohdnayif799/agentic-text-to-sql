"""What a query's structure says about its result (zero LLM calls).

Three facts about the SQL itself, none of them read from the wording of a question:

  join_filter_problem  a LEFT JOIN's ON clause filters a table nothing else reads, so
          the filter restricts nothing. A LEFT JOIN keeps every row on its left, so
          `LEFT JOIN products p ON ... AND p.category = 'Accessories'` still counts
          every category unless the query goes on to read p. No error, no empty
          result: only a wrong number. Added after a holdout failure (hv08).
  text_filters  the text values a query filters on, by real table and column, so the
          caller can look them up in the data.
  reads_no_table  the query reads no table. `SELECT 0 AS deleted_orders` runs and
          returns a row, but that row is not a fact from the data. Added after a
          safety-set failure (sa01).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from agentcrew.archs.checks import Schema

_FILTERS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between,
            exp.Like, exp.ILike, exp.Is, exp.Not)


def _parse(sql: str, schema: Schema) -> exp.Expression | None:
    """The query with its columns tied to their tables, or None if it does not
    parse. If qualifying fails the plain tree is returned: the schema check reports
    that problem, and qualified names are only needed for unqualified columns."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except ParseError:
        return None
    try:
        return qualify(tree.copy(), schema=schema, dialect="sqlite",
                       validate_qualify_columns=False, identify=False)
    except OptimizeError:
        return tree


def _conjuncts(condition: exp.Expression) -> list[exp.Expression]:
    return (list(condition.flatten()) if isinstance(condition, exp.And)
            else [condition])


def _only_on(predicate: exp.Expression, alias: str) -> bool:
    """True if a predicate compares this table's columns with fixed values."""
    if not isinstance(predicate, _FILTERS):
        return False
    columns = list(predicate.find_all(exp.Column))
    return bool(columns) and all(c.table.lower() == alias for c in columns)


def _read_elsewhere(select: exp.Select, on: exp.Expression, alias: str) -> bool:
    """True if anything outside this ON clause reads the table: the select list, the
    filters, the grouping, or another join's condition, through which it can still
    decide which rows survive."""
    own = {id(node) for node in on.walk()}
    return any(column.table.lower() == alias and id(column) not in own
               for column in select.find_all(exp.Column))


def join_filter_problem(sql: str, schema: Schema) -> str | None:
    tree = _parse(sql, schema)
    if tree is None:
        return None
    for select in tree.find_all(exp.Select):
        if select.is_star:
            continue
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if on is None or (join.side or "").upper() != "LEFT":
                continue
            alias = join.this.alias_or_name.lower()
            if _read_elsewhere(select, on, alias):
                continue
            for predicate in _conjuncts(on):
                if _only_on(predicate, alias):
                    shown = predicate.sql(dialect="sqlite")
                    return (
                        f"The filter {shown} is in the ON clause of a LEFT JOIN and "
                        f"nothing else in the query reads {alias}, so it restricts no "
                        "rows: the result still counts every row. Move the filter to "
                        "WHERE, or use an inner join."
                    )
    return None


def text_filters(sql: str, schema: Schema) -> list[tuple[str, str, str]]:
    """(table, column, value) for each text value the query filters a column on with
    =, IN or LIKE. A value in a LIKE keeps its % signs."""
    tree = _parse(sql, schema)
    if tree is None:
        return []
    tables = {t.alias_or_name.lower(): t.name for t in tree.find_all(exp.Table)
              if t.name in schema}
    found: list[tuple[str, str, str]] = []

    def add(column: exp.Expression, value: exp.Expression) -> None:
        if not (isinstance(column, exp.Column) and isinstance(value, exp.Literal)
                and value.is_string):
            return
        table = tables.get(column.table.lower())
        if table is not None:
            found.append((table, column.name, value.this))

    for node in tree.find_all(exp.EQ, exp.Like):
        add(node.left, node.right)
        add(node.right, node.left)
    for node in tree.find_all(exp.In):
        for value in node.expressions:
            add(node.this, value)
    return list(dict.fromkeys(found))


def reads_no_table(sql: str) -> bool:
    """True if the query parses and reads no table at all."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except ParseError:
        return False
    return isinstance(tree, exp.Query) and tree.find(exp.Table) is None
