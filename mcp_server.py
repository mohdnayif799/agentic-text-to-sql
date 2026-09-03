"""MCP adapter over the agent's database tools.

Why MCP is here, and why it is *thin*
-------------------------------------
The honest evaluation: MCP adds nothing to the in-process agent. Routing a
function call through a JSON-RPC transport to reach code in the same
interpreter buys latency and a serialisation boundary in exchange for zero
capability. So the graph calls `tools/tools.py` directly.

What MCP genuinely buys is *reuse outside this process*. The same four tools,
unchanged, can be mounted in Claude Desktop, Cursor, or any MCP client, which
means the read-only guard and row caps protect those sessions too. That is a
real benefit and it costs about eighty lines.

This file is therefore an adapter, not an implementation. Every tool below
delegates to the exact functions the agent uses, so the two can never drift.

API note: verified against mcp==2.1.0 (2026-08-25). The 1.x entry point
`mcp.server.fastmcp.FastMCP` was removed in 2.0; the current class is
`mcp.server.MCPServer`.

Run it:
    python mcp_server/server.py
Or register in an MCP client with:
    command: python, args: ["/abs/path/mcp_server/server.py"]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp.server import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from agentcrew import tools  # noqa: E402
from agentcrew.config import get_settings  # noqa: E402
from agentcrew.db.engine import read_only_engine  # noqa: E402

settings = get_settings()
_engine = read_only_engine(settings.database_path, timeout=settings.sql_timeout_seconds)
_ctx = tools.ToolContext.create(
    _engine,
    max_rows=settings.max_result_rows,
    timeout=settings.sql_timeout_seconds,
    allow_write=False,  # never true over MCP, regardless of local config
)

server = MCPServer(
    name="agentcrew-analytics",
    title="AgentCrew Analytics Database",
    version="1.0.0",
    instructions=(
        "Read-only access to the NorthStar analytics database. Start with "
        "list_tables, then describe_table for the tables you need, then "
        "execute_readonly_sql. Write statements are rejected by the server."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


@server.tool(
    description="List every table with its column and row counts.",
    annotations=READ_ONLY,
)
def list_tables() -> list[dict[str, Any]]:
    return tools.list_tables(_ctx)


@server.tool(
    description="Columns, types, keys and foreign keys for one table.",
    annotations=READ_ONLY,
)
def describe_table(table: str) -> dict[str, Any]:
    return tools.describe_table(_ctx, table)


@server.tool(
    description=(
        "Return a few real rows from a table. Use this to check how values are "
        "actually formatted before filtering on them."
    ),
    annotations=READ_ONLY,
)
def get_sample_rows(table: str, limit: int = 5) -> dict[str, Any]:
    return tools.get_sample_rows(_ctx, table, limit=limit)


@server.tool(
    description=(
        "Run one read-only SQLite SELECT. Write statements, multiple "
        "statements and PRAGMA/ATTACH are rejected. Results are row-capped."
    ),
    annotations=READ_ONLY,
)
def execute_readonly_sql(sql: str) -> dict[str, Any]:
    result = tools.execute_readonly_sql(_ctx, sql)
    return {
        "ok": result.ok,
        "sql": result.sql,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "duration_ms": result.duration_ms,
        "error": result.error_message,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
