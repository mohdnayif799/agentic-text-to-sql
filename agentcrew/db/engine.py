"""Database connectivity.

The read-only guarantee lives here, not in the prompt. A connection created by
`read_only_engine()` is physically incapable of writing:

* opened through the SQLite URI ``file:<path>?mode=ro`` - the driver refuses
  write operations at the C level;
* ``PRAGMA query_only=ON`` set on every connection as a second stop;
* ``PRAGMA busy_timeout`` and a statement interrupt bound execution time.

If someone deletes the entire AST guard, the agent still cannot mutate data.
That is the property worth defending in a design review.

Untrusted SQLite files
----------------------
An uploaded SQLite file is someone else's schema. A view or trigger in it
can call SQL functions, and an application-defined function would then run
with the app's privileges. Every SQLite connection therefore also sets
``PRAGMA trusted_schema=OFF`` (schema objects may only call functions marked
innocuous) and SQLite's defensive mode (no writes to the schema tables, no
``PRAGMA writable_schema``). Northstar is the app's own file, but the same
hardening costs nothing there, so there is one engine, not two.

PostgreSQL (self-hosted only)
-----------------------------
``postgres_read_only_engine`` is the PostgreSQL counterpart. There is no URI
flag that makes a server connection read-only, so it takes three layers:
the session default is read-only (``default_transaction_read_only=on``),
every query runs inside ``SET TRANSACTION READ ONLY`` (execute_select), and
the deployment is told to connect as a role that has only SELECT
(docs/SELF_HOSTED.md). The role is the real guarantee; the other two stop a
mistake in the role from becoming a write.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection, make_url

from agentcrew.schemas import ExecutionResult, FailureKind


class DatabaseNotFoundError(FileNotFoundError):
    pass


class UnsupportedDatabaseError(ValueError):
    """AGENTCREW_DATABASE_URL names a database this build does not support."""


class DatabaseUnavailableError(RuntimeError):
    """The database at AGENTCREW_DATABASE_URL could not be reached or read.
    Its message has been through scrub_url, and it is raised `from None`, so
    neither it nor its traceback can carry the URL or the password."""


#: What sits between "scheme://user:" and the LAST "@" of a URL: the password
#: even when it holds an unescaped "@", which a URL parser would cut short.
_PASSWORD_SPAN = re.compile(r"^[^:/?#]+://[^:/@]*:(.*)@")


def scrub_url(text: str, url: str | None) -> str:
    """`text` with a database URL and its password replaced by ***.

    Driver and server error messages are written by other people's code and
    can echo what they were given. Everything that might be the password is
    removed: the whole URL, the password as the parser reads it, its
    percent-encoded and decoded spellings, and the greedy span above. No
    minimum length: a short password over-redacts a log line, which is the
    safe way to be wrong.
    """
    if not url or not text:
        return text
    secrets = {url}
    try:
        password = make_url(url).password
    except Exception:  # an unparseable URL still has to be scrubbed
        password = None
    span = _PASSWORD_SPAN.match(url)
    for candidate in (password, span.group(1) if span else None):
        if candidate:
            secrets |= {candidate, unquote(candidate), quote(candidate, safe=""),
                        quote_plus(candidate)}
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text


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
        harden_sqlite(dbapi_conn, timeout=timeout)

    return engine


def harden_sqlite(conn: sqlite3.Connection, *, timeout: float = 15.0) -> None:
    """Make a SQLite connection read-only and safe for an untrusted schema.

    Shared by the agent's engine and by the upload checks
    (agentcrew/datasets/sqlite_upload.py), so an uploaded file is opened with
    exactly the settings the agent later uses on it.
    """
    # Defensive mode first: it refuses PRAGMA writable_schema and direct
    # writes to sqlite_master, which could otherwise rewrite the schema the
    # other settings rely on. setconfig exists from Python 3.12.
    defensive = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
    if defensive is not None and hasattr(conn, "setconfig"):
        conn.setconfig(defensive, True)
    cur = conn.cursor()
    cur.execute("PRAGMA query_only=ON")
    # Views and triggers may only call functions marked innocuous.
    cur.execute("PRAGMA trusted_schema=OFF")
    cur.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    cur.close()


POSTGRES_SCHEMES = ("postgresql", "postgres", "postgresql+psycopg")


def postgres_read_only_engine(url: str, *, timeout: float = 15.0) -> Engine:
    """A read-only engine on a PostgreSQL server (self-hosted mode only).

    `url` is AGENTCREW_DATABASE_URL, e.g.
    postgresql://agent_ro:PASSWORD@db:5432/company. The scheme is normalised
    to the psycopg 3 driver; any other database is refused, so a MySQL URL
    fails here with a clear message instead of later with a driver error.
    """
    try:
        parsed = make_url(url)
    except Exception:
        # from None: the parser's own error can quote the string it was given.
        raise UnsupportedDatabaseError(
            "AGENTCREW_DATABASE_URL is not a valid database URL") from None
    if parsed.drivername not in POSTGRES_SCHEMES:
        raise UnsupportedDatabaseError(
            f"AGENTCREW_DATABASE_URL must be a PostgreSQL URL "
            f"(postgresql://...), not {parsed.drivername!r}. MySQL and other "
            "databases are not supported in this version.")
    parsed = parsed.set(drivername="postgresql+psycopg")
    millis = int(timeout * 1000)
    engine = create_engine(
        parsed,
        # Settings travel with the connection, before any statement: the
        # session is read-only and every statement has a time limit.
        connect_args={"options": f"-c default_transaction_read_only=on "
                                 f"-c statement_timeout={millis}",
                      "connect_timeout": 10},
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
    )
    return engine


def is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


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
        elif is_postgres(engine):
            # The first statement of the transaction SQLAlchemy just began:
            # this one query runs read-only whatever the session default says.
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")

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
