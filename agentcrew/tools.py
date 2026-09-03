"""The agent's tool surface.

Four tools, deliberately. Every one of them is something the agent genuinely
cannot do by reasoning alone, and none of them overlaps another:

  list_tables          - what exists
  describe_table       - what a table contains
  get_sample_rows      - what the values actually look like
  execute_readonly_sql - the actual work

Anything more would be surface area without capability. Anything less and the
agent is guessing at the schema.

This module is the single source of truth. `mcp_server.py` is a thin
adapter that re-exports these same functions over MCP, so the in-process agent
and any external MCP client are provably running identical code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from agentcrew.db.engine import execute_select
from agentcrew.db.introspect import Catalog, build_catalog
from agentcrew.db.safety import UnsafeSQLError, guard_sql
from agentcrew.schemas import ExecutionResult, FailureKind


@dataclass
class ToolContext:
    """Everything the tools need. Constructed once per run."""

    engine: Engine
    catalog: Catalog
    max_rows: int = 500
    timeout: float = 15.0
    allow_write: bool = False

    @classmethod
    def create(
        cls,
        engine: Engine,
        *,
        max_rows: int = 500,
        timeout: float = 15.0,
        allow_write: bool = False,
    ) -> ToolContext:
        return cls(
            engine=engine,
            catalog=build_catalog(engine),
            max_rows=max_rows,
            timeout=timeout,
            allow_write=allow_write,
        )


def list_tables(ctx: ToolContext) -> list[dict[str, Any]]:
    """Every table with its column count and row count."""
    return [
        {
            "table": name,
            "columns": len(t.columns),
            "rows": t.row_count,
        }
        for name, t in sorted(ctx.catalog.tables.items())
    ]


def describe_table(ctx: ToolContext, table: str) -> dict[str, Any]:
    """Columns, types, keys and foreign-key relationships for one table."""
    t = ctx.catalog.tables.get(table)
    if t is None:
        return {
            "error": f"No such table '{table}'.",
            "available": sorted(ctx.catalog.tables),
        }
    related = [
        {
            "from": f"{fk.from_table}.{'/'.join(fk.from_columns)}",
            "to": f"{fk.to_table}.{'/'.join(fk.to_columns)}",
        }
        for fk in ctx.catalog.foreign_keys
        if table in (fk.from_table, fk.to_table)
    ]
    return {
        "table": t.name,
        "rows": t.row_count,
        "columns": [
            {
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
                "primary_key": c.primary_key,
            }
            for c in t.columns
        ],
        "foreign_keys": related,
    }


def get_sample_rows(ctx: ToolContext, table: str, limit: int = 5) -> dict[str, Any]:
    """A few real rows. The cheapest cure for hallucinated value formats."""
    if table not in ctx.catalog.tables:
        return {
            "error": f"No such table '{table}'.",
            "available": sorted(ctx.catalog.tables),
        }
    limit = max(1, min(int(limit), 20))
    result = execute_select(
        ctx.engine,
        f'SELECT * FROM "{table}" LIMIT {limit}',  # noqa: S608 - name validated above
        max_rows=limit,
        timeout=ctx.timeout,
    )
    if not result.ok:
        return {"error": result.error_message}
    return {"table": table, "columns": result.columns, "rows": result.rows}


def execute_readonly_sql(ctx: ToolContext, sql: str) -> ExecutionResult:
    """Guard, then run, a single read-only statement.

    Guard rejections are returned as a normal failed result rather than raised,
    because the agent needs to read the reason and try again. That feedback
    loop is the point of the whole system.
    """
    try:
        guarded = guard_sql(
            sql, max_rows=ctx.max_rows, allow_write=ctx.allow_write
        )
    except UnsafeSQLError as exc:
        return ExecutionResult(
            ok=False,
            sql=sql,
            failure=FailureKind.BLOCKED,
            error_message=str(exc),
        )

    result = execute_select(
        ctx.engine,
        guarded.sql,
        max_rows=ctx.max_rows,
        timeout=ctx.timeout,
    )
    result.sql = guarded.sql
    return result
