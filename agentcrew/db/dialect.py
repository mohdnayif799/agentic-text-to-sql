"""Which SQL dialect the current run speaks.

Why a context variable
----------------------
The SQL checks in archs/ (schema check, fan-out, join filter, verification,
assumptions) and the guard in db/safety.py parse SQL with sqlglot, and
sqlglot needs to know the dialect. Northstar and uploaded data are SQLite;
a self-hosted deployment may point AGENTCREW_DATABASE_URL at PostgreSQL.

Passing the dialect through every function that parses SQL would touch the
signature of each check in the router and in D. Instead the run sets it once
(archs.run_architecture, from ToolContext.dialect) and each parse reads it
here. The default is "sqlite", so a caller that never sets it, including
every existing test and the evaluation harness, behaves exactly as before.

A ContextVar rather than a global: Streamlit serves several sessions from
threads of one process, and a context variable is per thread (and per task),
so one session's PostgreSQL run cannot change another's dialect.

Only "sqlite" and "postgres" are accepted today. MySQL is left out on
purpose (identifier quoting and read-only transactions differ); adding it
means adding the name here, a read-only engine in db/engine.py and tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

SQLITE = "sqlite"
POSTGRES = "postgres"
SUPPORTED = (SQLITE, POSTGRES)

_current: ContextVar[str] = ContextVar("agentcrew_sql_dialect", default=SQLITE)


def current() -> str:
    """The sqlglot dialect name for the run in progress."""
    return _current.get()


def check(name: str) -> str:
    """`name` if supported, else ValueError naming what is."""
    if name not in SUPPORTED:
        raise ValueError(f"unsupported SQL dialect {name!r}; supported: {SUPPORTED}")
    return name


@contextmanager
def using(name: str) -> Iterator[None]:
    """Run the block with `name` as the current dialect."""
    token = _current.set(check(name))
    try:
        yield
    finally:
        _current.reset(token)
