"""Deterministic signals that trigger a repair (no LLM calls).

Checked in this order, and only the first one is acted on:

  error   the query failed (database error or safety-guard rejection)
  schema  sqlglot `qualify` against the real schema cannot resolve a column.
          SQLite silently treats an unknown double-quoted name as a string
          literal, so this catches mistakes the database would not report.
  fanout  an aggregate after a join on a key that is not unique on either
          side (archs/fanout.py), which multiplies the aggregated values
  empty   the query ran but returned no rows
  sanity  every value is NULL, or the result hit the row cap (the query
          probably lacks an aggregation or filter)
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from agentcrew.db.introspect import Catalog
from agentcrew.schemas import ExecutionResult

Schema = dict[str, dict[str, str]]


@dataclass(frozen=True)
class Signal:
    kind: str
    detail: str


def schema_of(catalog: Catalog) -> Schema:
    return {
        name: {c.name: c.type for c in table.columns}
        for name, table in catalog.tables.items()
    }


def schema_error(sql: str, schema: Schema) -> str | None:
    """The qualify error text, or None if every column resolves."""
    try:
        qualify(
            sqlglot.parse_one(sql, read="sqlite"), schema=schema, dialect="sqlite",
            validate_qualify_columns=True, identify=False,
        )
    except OptimizeError as exc:
        return str(exc)
    except ParseError:
        return None  # the database reports syntax errors itself
    return None


def classify(
    result: ExecutionResult,
    schema_problem: str | None,
    max_rows: int,
    fanout_problem: str | None = None,
) -> Signal | None:
    """First signal for an executed query. `schema_problem` (schema_error) and
    `fanout_problem` (fanout.fanout_problem) are found statically, before the
    query is executed."""
    if not result.ok:
        return Signal("error", result.error_message or "query failed")
    if schema_problem:
        return Signal("schema", f"Schema check: {schema_problem}")
    if fanout_problem:
        return Signal("fanout", fanout_problem)
    if not result.rows:
        return Signal("empty", "The query ran but returned no rows.")
    if all(cell is None for row in result.rows for cell in row):
        return Signal("sanity", "Every value in the result is NULL.")
    if result.row_count >= max_rows:
        return Signal(
            "sanity",
            f"The result hit the {max_rows}-row cap; the query probably lacks "
            "an aggregation or a filter.",
        )
    return None
