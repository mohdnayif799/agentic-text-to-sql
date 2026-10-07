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

One more deterministic signal is read from the question, not the result:

  forecast  the question asks for a forecast, a projection, a trend line or
            a regression fit. It escalates directly to follow-up work (no
            follow-up decision call, nothing to repair): one SQL query that
            fits a line can be silently wrong (an x offset between the fit and
            the forecast), and the fit belongs in a deterministic tool.
            Added on 2026-10-05 after a development failure (dev2 dv03: A's
            one-query fit was wrong with no error and C's follow-up decision
            accepted it, so neither C nor D reached the compute tools). Kept
            narrow: z-scores and correlations, which one query computes
            correctly (dev2 dv04, showcase sc04), do not trigger it.
"""

from __future__ import annotations

import re
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


_FORECAST = re.compile(
    r"\b(forecast\w*|project|projects|projected|projecting|projections?"
    r"|extrapolat\w*|trend\s*line|(linear|straight[-\s]line)\s+trend|regression"
    r"|least[-\s]squares|line\s+of\s+best\s+fit|best[-\s]fit\s+line)\b",
    re.IGNORECASE,
)
FORECAST_DETAIL = (
    "The question asks for a forecast, a projection or a fitted trend line: "
    "get the series it is fitted on (one row per period), fit it from that "
    "series, and report each forecast with its period."
)


def forecast_signal(question: str) -> Signal | None:
    """The question-level "forecast" signal (see the module docstring)."""
    return Signal("forecast", FORECAST_DETAIL) if _FORECAST.search(question) else None
