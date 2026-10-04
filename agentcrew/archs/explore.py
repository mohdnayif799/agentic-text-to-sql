"""Exploration tools for architecture D and the validated next-action schema.

decide_next_action returns one NextAction (validated JSON, no native tool
calling). Tools:

  get_distinct_values(table, column)   a column's real values
  search_values(term[, table, column]) fuzzy-match a user's word to real
                                       values (difflib), across text columns
  sample_rows(table)                   a few rows of a table
  run_sql(sql)                         read-only, guarded, row-limited query
  compute(function, params)            deterministic pandas (compute/)
  answer                               stop; the results suffice

Every tool returns an ExecutionResult so the loop, previews, selection and
the final answer treat all steps alike. Only run_sql and compute results can
become the graded final result; lookups only inform later steps.
"""

from __future__ import annotations

import difflib
import inspect
from collections.abc import Callable
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import Engine, text

from agentcrew.compute.functions import FUNCTIONS, ComputeError
from agentcrew.compute.metrics import (
    METRICS,
    Comparison,
    contribution_by_dimension,
    decompose_change,
    has_components,
)
from agentcrew.db.introspect import Catalog
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext, get_sample_rows

ACTIONS = ("get_distinct_values", "search_values", "sample_rows", "run_sql",
           "compute", "answer")
METRIC_TOOLS = ("decompose_change", "contribution_by_dimension")
COMPUTE_FUNCTIONS = tuple(FUNCTIONS) + METRIC_TOOLS
SEARCHABLE_DISTINCT = 5000
MATCH_CUTOFF = 0.5


class NextAction(BaseModel):
    action: Literal["get_distinct_values", "search_values", "sample_rows",
                    "run_sql", "compute", "answer"]
    reason: str = Field(default="", description="One sentence.")
    table: str | None = None
    column: str | None = None
    term: str | None = None
    sql: str | None = None
    function: Literal[
        "pct_change", "yoy_growth", "compare_periods", "share_of_total",
        "zscore_anomalies", "linear_trend_forecast", "correlation",
        "decompose_change", "contribution_by_dimension",
    ] | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _required(self) -> NextAction:
        needs = {"get_distinct_values": ("table", "column"),
                 "search_values": ("term",), "sample_rows": ("table",),
                 "run_sql": ("sql",), "compute": ("function",)}
        missing = [f for f in needs.get(self.action, ()) if not getattr(self, f)]
        if missing:
            raise ValueError(f"action {self.action} needs {', '.join(missing)}")
        return self


class MetricPlan(BaseModel):
    """The metric analysis that opens a why-question's exploration (D).

    The metric name is checked against the metric allowlist (METRICS, which
    mirrors the data dictionary's metric definitions) when the model's JSON
    is validated, before any SQL exists; the comparison's dimension names are
    checked against each source's dimension allowlist when the SQL is built.
    """

    metric: str = Field(description="One of the listed metrics.")
    comparison: Comparison
    dimension: str | None = Field(
        default=None,
        description="Only for a metric without components: the dimension whose "
                    "values may explain the difference.")
    reason: str = Field(default="", description="One sentence.")

    @model_validator(mode="after")
    def _allowed(self) -> MetricPlan:
        if self.metric not in METRICS:
            raise ValueError(f"unknown metric {self.metric!r}; "
                             f"choose from {sorted(METRICS)}")
        if not has_components(self.metric) and not self.dimension:
            raise ValueError(f"{self.metric} has no components; give the dimension "
                             "whose values may explain the difference")
        return self

    def to_action(self) -> NextAction:
        """decompose_change when the metric has components, otherwise
        contribution_by_dimension over the given dimension."""
        params: dict[str, Any] = {
            "metric": self.metric,
            "comparison": self.comparison.model_dump(mode="json", exclude_none=True),
        }
        if has_components(self.metric):
            function = "decompose_change"
        else:
            function = "contribution_by_dimension"
            params["dimension"] = self.dimension
        return NextAction(action="compute", function=function, params=params,
                          reason=self.reason)


def _result(columns: list[str], rows: list[list[Any]], sql: str) -> ExecutionResult:
    return ExecutionResult(ok=True, sql=sql, columns=columns, rows=rows,
                           row_count=len(rows))


def _error(message: str, sql: str = "") -> ExecutionResult:
    return ExecutionResult(ok=False, sql=sql, error_message=message)


def _plain(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def frame_result(df: pd.DataFrame, sql: str) -> ExecutionResult:
    rows = [[_plain(v) for v in row] for row in df.itertuples(index=False)]
    return _result([str(c) for c in df.columns], rows, sql)


class ValueIndex:
    """Distinct values of every non-date text column, for fuzzy search."""

    def __init__(self, engine: Engine, catalog: Catalog) -> None:
        self.values: list[tuple[str, str, str]] = []
        with engine.connect() as conn:
            for table in sorted(catalog.tables):
                for col in catalog.tables[table].columns:
                    if "TEXT" not in col.type.upper():
                        continue
                    # Names come from introspection of this database.
                    c, t = f'"{col.name}"', f'"{table}"'
                    count = conn.execute(
                        text(f"SELECT COUNT(DISTINCT {c}) FROM {t}")  # noqa: S608
                    ).scalar_one()
                    if count > SEARCHABLE_DISTINCT:
                        continue
                    values = conn.execute(text(
                        f"SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL"  # noqa: S608
                    )).scalars().all()
                    first = str(values[0]) if values else ""
                    if first[:4].isdigit() and "-" in first:
                        continue  # dates
                    self.values += [(table, col.name, str(v)) for v in values]

    def search(self, term: str, table: str | None = None,
               column: str | None = None, limit: int = 5) -> list[list[Any]]:
        needle = " ".join(term.lower().split())
        scored = []
        for t, c, value in self.values:
            if (table and t != table) or (column and c != column):
                continue
            hay = value.lower()
            score = difflib.SequenceMatcher(None, needle, hay).ratio()
            if needle in hay or hay in needle:
                score = max(score, 0.9)
            if score >= MATCH_CUTOFF:
                scored.append((score, t, c, value))
        scored.sort(key=lambda s: (-s[0], s[1], s[2], s[3]))
        return [[t, c, v, round(s, 3)] for s, t, c, v in scored[:limit]]


_INDEXES: dict[str, ValueIndex] = {}


def value_index(tools: ToolContext) -> ValueIndex:
    url = str(tools.engine.url)
    if url not in _INDEXES:
        _INDEXES[url] = ValueIndex(tools.engine, tools.catalog)
    return _INDEXES[url]


def _known(tools: ToolContext, table: str, column: str | None = None) -> str | None:
    if table not in tools.catalog.tables:
        return f"no table {table!r}; tables: {sorted(tools.catalog.tables)}"
    if column is not None:
        names = [c.name for c in tools.catalog.tables[table].columns]
        if column not in names:
            return f"no column {column!r} in {table}; columns: {names}"
    return None


def lookup(action: NextAction, tools: ToolContext) -> ExecutionResult:
    """get_distinct_values, search_values and sample_rows."""
    if action.action == "get_distinct_values":
        problem = _known(tools, action.table, action.column)
        if problem:
            return _error(problem)
        sql = (f'SELECT DISTINCT "{action.column}" FROM "{action.table}" '  # noqa: S608
               f'ORDER BY 1 LIMIT 100')
        with tools.engine.connect() as conn:
            rows = [[v] for v in conn.execute(text(sql)).scalars()]
        return _result([action.column], rows, f"-- get_distinct_values\n{sql}")
    if action.action == "search_values":
        if action.table and _known(tools, action.table, action.column):
            return _error(_known(tools, action.table, action.column))
        rows = value_index(tools).search(action.term, action.table, action.column)
        return _result(["table", "column", "value", "score"], rows,
                       f"-- search_values({action.term!r})")
    problem = _known(tools, action.table)
    if problem:
        return _error(problem)
    sample = get_sample_rows(tools, action.table, limit=5)
    return _result(sample["columns"], sample["rows"],
                   f"-- sample_rows({action.table})")


def compute(
    action: NextAction,
    step_results: dict[int, ExecutionResult],
    run_sql: Callable[[str], ExecutionResult],
) -> tuple[ExecutionResult, list[str]]:
    """Run one compute function. Returns (result, feeding SQL)."""
    params = dict(action.params)
    name = action.function
    label = f"-- compute: {name}"
    try:
        if name in METRIC_TOOLS:
            comparison = Comparison.model_validate(params.get("comparison") or {})
            metric = params.get("metric", "")
            if name == "decompose_change":
                df, queries = decompose_change(run_sql, metric, comparison)
            else:
                df, queries = contribution_by_dimension(
                    run_sql, metric, params.get("dimension", ""), comparison)
            return frame_result(df, label + "\n" + "\n".join(queries)), queries
        step = params.pop("step", None)
        if step not in step_results:
            raise ComputeError(f"params.step must be one of {sorted(step_results)}")
        source = step_results[step]
        if not source.ok or not source.rows:
            raise ComputeError(f"step {step} has no rows to compute on")
        function = FUNCTIONS[name]
        allowed = set(inspect.signature(function).parameters) - {"df"}
        unknown = set(params) - allowed
        if unknown:
            raise ComputeError(f"unknown params {sorted(unknown)}; "
                               f"{name} takes {sorted(allowed)}")
        df = pd.DataFrame(source.rows, columns=source.columns)
        return frame_result(function(df, **params), f"{label} on step {step}"), []
    except (ComputeError, ValidationError, TypeError) as exc:
        return _error(f"{name}: {exc}", label), []
