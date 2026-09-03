"""Database connectivity.

The read-only guarantee lives here, not in the prompt. A connection created by
`read_only_engine()` is physically incapable of writing:

* opened through the SQLite URI ``file:<path>?mode=ro`` - the driver refuses
  write operations at the C level;
* ``PRAGMA query_only=ON`` set on every connection as a second stop;
* ``PRAGMA busy_timeout`` and a statement interrupt bound execution time.

If someone deletes the entire AST guard, the agent still cannot mutate data.
That is the property worth defending in a design review.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection

from agentcrew.schemas import ExecutionResult, FailureKind


class DatabaseNotFoundError(FileNotFoundError):
    pass


def _sqlite_url(path: Path, read_only: bool) -> str:
    if read_only:
        return f"sqlite+pysqlite:///file:{path}?mode=ro&uri=true"
    return f"sqlite+pysqlite:///{path}"


def read_only_engine(path: Path, *, timeout: float = 15.0) -> Engine:
    """Create an engine that cannot write to `path`."""
    path = Path(path)
    if not path.exists():
        raise DatabaseNotFoundError(
            f"No database at {path}. Run `python scripts/build_database.py` first."
        )

    engine = create_engine(
        _sqlite_url(path, read_only=True),
        connect_args={"timeout": timeout, "check_same_thread": False},
        poolclass=None,
    )

    @event.listens_for(engine, "connect")
    def _harden(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA query_only=ON")
        cur.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        cur.close()

    return engine


def writable_engine(path: Path) -> Engine:
    """Only used by the seed script. Never handed to the agent."""
    return create_engine(_sqlite_url(Path(path), read_only=False))


def _classify(message: str) -> FailureKind:
    m = message.lower()
    if "no such table" in m or "no such column" in m:
        return FailureKind.MISSING_OBJECT
    if "syntax error" in m or "incomplete input" in m or "unrecognized token" in m:
        return FailureKind.SYNTAX
    if "datatype mismatch" in m or "cannot be cast" in m:
        return FailureKind.TYPE_ERROR
    if "interrupted" in m or "timeout" in m:
        return FailureKind.TIMEOUT
    return FailureKind.OTHER


def execute_select(
    engine: Engine,
    sql: str,
    *,
    max_rows: int,
    timeout: float = 15.0,
) -> ExecutionResult:
    """Run a statement and capture the outcome as data, never as an exception.

    Errors are returned as `ExecutionResult(ok=False, ...)` because the agent
    needs to *read* the failure in order to repair the query. A raised
    exception would break the loop we are trying to build.
    """
    started = time.perf_counter()
    conn: Connection | None = None
    watchdog: threading.Timer | None = None
    try:
        conn = engine.connect()
        raw = conn.connection.driver_connection  # underlying sqlite3.Connection
        if isinstance(raw, sqlite3.Connection):
            watchdog = threading.Timer(timeout, raw.interrupt)
            watchdog.daemon = True
            watchdog.start()

        cursor = conn.execute(text(sql))
        columns = list(cursor.keys())
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [list(r) for r in fetched[:max_rows]]
        duration = (time.perf_counter() - started) * 1000
        return ExecutionResult(
            ok=True,
            sql=sql,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=round(duration, 2),
            failure=FailureKind.OVERSIZE
            if truncated
            else (FailureKind.EMPTY if not rows else FailureKind.NONE),
        )
    except Exception as exc:
        duration = (time.perf_counter() - started) * 1000
        message = str(exc)
        # SQLAlchemy wraps driver errors; the useful part is the tail.
        if "\n[SQL:" in message:
            message = message.split("\n[SQL:")[0]
        return ExecutionResult(
            ok=False,
            sql=sql,
            duration_ms=round(duration, 2),
            failure=_classify(message),
            error_message=message.strip(),
        )
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if conn is not None:
            conn.close()
