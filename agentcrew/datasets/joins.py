"""Joins for databases without foreign keys.

An uploaded CSV set, an Excel workbook or a company database dumped without
constraints has no foreign keys, so schema selection's join closure
(db/selector.py) and the "joins available" hints have nothing to work with,
and the model guesses joins from column names. This module proposes joins,
the visitor confirms, removes or corrects them, and the confirmed ones are
added to the catalog as if they were foreign keys.

A join is proposed only when the name AND the data agree:

* name: the same key-like column in both tables (``student_id`` and
  ``student_id``), or a reference to a table by its singular name
  (``course_id`` in ``marks`` -> ``courses.id`` or ``courses.course_id``);
* data: the referenced column is unique, and at least 90% of the distinct
  values on the referencing side are found in it.

Generic names are never enough. Two tables that both have ``id``, ``name``,
``date``, ``status`` or ``created_at`` share a word, not a relationship:
``students.id = courses.id`` matches numbers 1 to 40 on both sides and is
nonsense. So a generic column is never the shared name of a proposed join,
and a join a person writes is refused when both sides are the same generic
name.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, replace

from sqlalchemy import Engine

from agentcrew.db.engine import execute_select
from agentcrew.db.introspect import Catalog, ForeignKey, quote_name
from agentcrew.db.selector import tokenize

#: Column names that appear in many unrelated tables. Never a join by name.
GENERIC_COLUMNS = frozenset("""
    id uuid guid key code name title label description notes note comment
    comments type kind category status state flag active is_active enabled
    value amount total count quantity qty price cost score rank level grade
    rating percent percentage rate date day month year week quarter time
    timestamp datetime created created_at created_on updated updated_at
    updated_on modified modified_at deleted deleted_at start start_date end
    end_date email phone address city country region zip postcode url
    version source owner
""".split())

#: Endings that make a column look like a key rather than an attribute.
KEY_SUFFIXES = ("_id", "id", "_code", "_no", "_num", "_number", "_key", "_ref")

MIN_OVERLAP = 0.9
#: Each data check runs through execute_select, so it has the statement
#: timeout the agent's queries have: the overlap check is the one internal
#: query whose cost grows with the product of two tables.
QUERY_TIMEOUT = 5.0
#: All checks together. When it runs out, the joins found so far are kept:
#: a missing proposal costs a hint, a hung upload costs the visitor.
BUDGET_SECONDS = 20.0


@dataclass(frozen=True)
class Join:
    """from_table.from_column references to_table.to_column."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    reason: str = ""
    inferred: bool = True
    """False once a person added or corrected it."""

    def as_foreign_key(self) -> ForeignKey:
        return ForeignKey(self.from_table, (self.from_column,), self.to_table,
                          (self.to_column,))

    def sql(self) -> str:
        return (f"{self.from_table}.{self.from_column} = "
                f"{self.to_table}.{self.to_column}")


def is_generic(column: str) -> bool:
    return column.lower() in GENERIC_COLUMNS


def _key_like(column: str) -> bool:
    name = column.lower()
    return name.endswith(KEY_SUFFIXES) and not is_generic(name)


def _singular(table: str) -> str:
    """'students' -> 'student', 'course_marks' -> 'course_mark' (the selector's
    light-touch morphology, applied to the last word)."""
    words = table.lower().split("_")
    last = sorted(tokenize(words[-1])) or [words[-1]]
    return "_".join([*words[:-1], last[0]])


def _references(column: str, table: str) -> bool:
    """`column` names `table`: course_id / courseid / course_code -> courses."""
    stem = _singular(table)
    name = column.lower()
    return name in {f"{stem}_id", f"{stem}id", f"{stem}_code", f"{stem}_no",
                    f"{stem}_key", f"{stem}_number"}


def _candidates(catalog: Catalog) -> Iterable[tuple[str, str, str, str, str]]:
    """(from_table, from_column, to_table, to_column, rule) by name alone."""
    tables = catalog.tables
    for a, ta in tables.items():
        for ca in ta.columns:
            for b, tb in tables.items():
                if a == b:
                    continue
                for cb in tb.columns:
                    same = ca.name.lower() == cb.name.lower()
                    if same and _key_like(ca.name):
                        yield a, ca.name, b, cb.name, "same key column"
                    elif (_references(ca.name, b)
                          and (cb.primary_key or cb.name.lower() == "id"
                               or cb.name.lower() == ca.name.lower())
                          and not same):
                        yield a, ca.name, b, cb.name, f"{ca.name} names {b}"


def _scalar(engine: Engine, sql: str) -> int:
    """One number, or -1 when the query failed or timed out (not a join)."""
    result = execute_select(engine, sql, max_rows=1, timeout=QUERY_TIMEOUT)
    if not result.ok or not result.rows:
        return -1
    try:
        return int(result.rows[0][0] or 0)
    except (TypeError, ValueError):
        return -1


def _unique(engine: Engine, table: str, column: str) -> bool:
    # Introspected names, quoted by the dialect (introspect.quote_name).
    c, t = quote_name(engine, column), quote_name(engine, table)
    total = _scalar(engine, f"SELECT COUNT({c}) FROM {t}")  # noqa: S608
    distinct = _scalar(engine, f"SELECT COUNT(DISTINCT {c}) FROM {t}")  # noqa: S608
    return total > 0 and total == distinct


def _overlap(engine: Engine, a: str, ca: str, b: str, cb: str) -> float:
    qa, qca = quote_name(engine, a), quote_name(engine, ca)
    qb, qcb = quote_name(engine, b), quote_name(engine, cb)
    values = _scalar(engine, f"SELECT COUNT(DISTINCT {qca}) FROM {qa}")  # noqa: S608
    if values <= 0:
        return 0.0
    found = _scalar(engine, (
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {qca} AS v FROM {qa} "  # noqa: S608
        f"WHERE {qca} IS NOT NULL) x "
        f"WHERE EXISTS (SELECT 1 FROM {qb} WHERE {qb}.{qcb} = x.v)"))
    return max(found, 0) / values


def infer_joins(engine: Engine, catalog: Catalog, *,
                min_overlap: float = MIN_OVERLAP,
                budget: float = BUDGET_SECONDS) -> list[Join]:
    """Joins the names suggest and the data confirms, excluding any the
    catalog already has as a foreign key. Sorted for a stable display.
    Stops proposing after `budget` seconds and returns what it has."""
    existing = {(fk.from_table, fk.from_columns, fk.to_table, fk.to_columns)
                for fk in catalog.foreign_keys}
    found: dict[tuple[str, str, str, str], Join] = {}
    deadline = time.monotonic() + budget
    for a, ca, b, cb, rule in _candidates(catalog):
        if time.monotonic() > deadline:
            break
        if (a, (ca,), b, (cb,)) in existing or (b, (cb,), a, (ca,)) in existing:
            continue
        if not _unique(engine, b, cb):
            continue  # the referenced side must be a key
        if _overlap(engine, a, ca, b, cb) < min_overlap:
            continue
        if (b, cb, a, ca) in found and _unique(engine, a, ca):
            # One-to-one: keep the direction toward the table the name points at.
            if _references(ca, b):
                del found[(b, cb, a, ca)]
            else:
                continue
        found[(a, ca, b, cb)] = Join(a, ca, b, cb, reason=rule)
    return sorted(found.values(), key=lambda j: (j.from_table, j.from_column,
                                                 j.to_table, j.to_column))


def check_join(join: Join, catalog: Catalog) -> str | None:
    """Why a join a person entered is refused, or None if it is fine."""
    for table, column in ((join.from_table, join.from_column),
                          (join.to_table, join.to_column)):
        if table not in catalog.tables:
            return f"there is no table {table!r}"
        if column not in catalog.tables[table].column_names:
            return f"{table} has no column {column!r}"
    if join.from_table == join.to_table:
        return "a join must connect two different tables"
    if (join.from_column.lower() == join.to_column.lower()
            and is_generic(join.from_column)):
        return (f"both sides are the generic column {join.from_column!r}, which "
                "does not make two tables related")
    return None


def with_joins(catalog: Catalog, joins: Iterable[Join]) -> Catalog:
    """A new catalog whose foreign keys also hold `joins` (the original is
    not modified: it may be shared)."""
    extra = [j.as_foreign_key() for j in joins]
    return replace(catalog, tables=dict(catalog.tables),
                   foreign_keys=[*catalog.foreign_keys, *extra])
