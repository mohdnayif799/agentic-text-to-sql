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
  null_row  some rows are NULL in every numeric column while other rows have
          values. An aggregate over no rows still returns one row, so a query
          that glues the branches of a conditional question together with
          UNION ALL keeps a hollow row for the branch that does not apply.
          Added after a holdout failure (ha20). Measures are read from the
          result (columns holding a number), never from a row's label.

Two more deterministic signals are read from the question, not the result:

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
  conditional  a sentence opens with "if ...,": the answer depends on a
            condition. It also escalates without a decision call: one query
            that evaluates the condition and every branch hides which branch
            was taken and can leave rows from the other one (holdout ha19 named
            the wrong branch, ha20 kept a hollow row). Kept narrow: "what if"
            and "if any" do not trigger it.
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
    return _null_row_signal(result)


#: LAG and LEAD leave NULL in the first or last rows by design.
_OFFSET_WINDOW = re.compile(r"\b(lag|lead)\s*\(", re.IGNORECASE)
MAX_LABELS = 5


def is_number(cell: object) -> bool:
    return isinstance(cell, int | float) and not isinstance(cell, bool)


def hollow_rows(result: ExecutionResult) -> list[int]:
    """Indexes of rows that are NULL in every measure column (a column holding
    a number) while at least one other row has a value in one."""
    rows = result.rows
    measures = [j for j in range(len(result.columns))
                if any(is_number(row[j]) for row in rows)]
    if not measures or _OFFSET_WINDOW.search(result.sql or ""):
        return []
    return [i for i, row in enumerate(rows) if all(row[j] is None for j in measures)]


def _null_row_signal(result: ExecutionResult) -> Signal | None:
    hollow = hollow_rows(result)
    if not hollow:
        return None
    labels = [next((str(cell) for cell in result.rows[i] if cell is not None),
                   f"row {i + 1}") for i in hollow[:MAX_LABELS]]
    return Signal(
        "null_row",
        f"{len(hollow)} row(s) have no value in any numeric column while other "
        f"rows have values: {', '.join(labels)}. A query branch or join that does "
        "not apply to this question probably produced them. Return only the rows "
        "that answer the question, with the other rows' values unchanged.",
    )


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


_CONDITIONAL = re.compile(r"(?:^|[.?!;:]\s+)if\b(?!\s+any\b)[^.?!;]*,", re.IGNORECASE)
CONDITIONAL_DETAIL = (
    "The answer depends on a condition: evaluate the condition first with its own "
    "query, then answer only the branch that applies."
)


def conditional_signal(question: str) -> Signal | None:
    """The question-level "conditional" signal (see the module docstring)."""
    return (Signal("conditional", CONDITIONAL_DETAIL)
            if _CONDITIONAL.search(question) else None)


def question_signal(question: str) -> Signal | None:
    """The signal read from the question, if any. A forecast has its own
    tool path, so it wins when a question is both."""
    return forecast_signal(question) or conditional_signal(question)
