"""Request and response bodies of the API.

Requests forbid unknown fields, so a misspelt field is an error rather than
silently ignored, and bound every string. Responses are plain data: no
field carries a key, a file path or another caller's data.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.sql_editor import MAX_SQL_CHARS

MAX_QUESTION_CHARS = 2000
DATASET_ID = r"^[0-9a-f]{32}$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AskRequest(_Request):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    dataset_id: str | None = Field(default=None, pattern=DATASET_ID)
    """Answer from this uploaded dataset; omitted, from the sample data."""


class SqlRequest(_Request):
    sql: str = Field(min_length=1, max_length=MAX_SQL_CHARS)
    dataset_id: str | None = Field(default=None, pattern=DATASET_ID)


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Result(BaseModel):
    """A result table. `rows_capped` is true when the table stopped at the
    row cap, so there may be more rows than these."""

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    rows_capped: bool = False


class AskResponse(Result):
    status: Literal["done", "failed", "refused", "needs_clarification"]
    """done: answered. failed: the agent ran and could not answer. refused:
    the question asked to change data. needs_clarification: the answer is a
    question back to you."""
    answer: str
    sql: str | None = None
    """The SQL behind the final table, when there is one."""
    path: str = "-"
    """direct (one query was enough) or escalated."""
    llm_calls: int = 0
    caveats: list[str] = Field(default_factory=list)
    dataset_id: str | None = None
    seconds: float = 0.0


class SqlResponse(Result):
    sql: str
    """The statement as it ran, with the row limit the guard added."""
    dataset_id: str | None = None
    seconds: float = 0.0


class DatasetCreated(BaseModel):
    dataset_id: str
    label: str
    kind: str
    tables: list[str]
    size_bytes: int
    descriptions: Literal["drafted", "empty"]
    """Whether the model drafted descriptions. Questions work either way."""
    expires_after_seconds: int
    """Deleted after this long without use."""


class ColumnSchema(BaseModel):
    name: str
    type: str
    description: str = ""


class TableSchema(BaseModel):
    name: str
    description: str = ""
    row_meaning: str = ""
    """What one row of the table is."""
    row_count: int = 0
    columns: list[ColumnSchema]


class JoinSchema(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    reason: str = ""


class DatasetSchema(BaseModel):
    dataset_id: str
    label: str
    kind: str
    tables: list[TableSchema]
    joins: list[JoinSchema]
    expires_after_seconds: int


def cell(value: Any) -> Any:
    """One result value as JSON. SQLite gives text, numbers and None already;
    PostgreSQL adds dates, decimals and the rest. Bytes are never sent."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def table(result: Any, max_rows: int) -> dict[str, Any]:
    """The Result fields for an ExecutionResult. The guard injects the cap as
    a LIMIT, so reaching it is the sign the table was cut."""
    if result is None or not result.ok:
        return {}
    capped = bool(result.truncated or (max_rows > 0 and result.row_count >= max_rows))
    return {
        "columns": list(result.columns),
        "rows": [[cell(v) for v in row] for row in result.rows],
        "row_count": result.row_count,
        "rows_capped": capped,
    }
