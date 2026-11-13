"""Run a visitor's own SQL: the core of the SQL editor (and, later, the API).

Why this module exists
----------------------
The editor must not be a second, weaker way into the database. So it adds no
SQL handling of its own: every statement goes through
``tools.execute_readonly_sql`` on the caller's ``ToolContext``, the same
object the agent's runs use, so the read-only connection, the sqlglot guard,
the injected LIMIT, the row cap and the statement timeout are the agent's,
on NorthStar, an uploaded dataset or self-hosted PostgreSQL alike.

What it adds is what a person typing SQL needs and the agent does not:

* limits: a per-session allowance of SQL runs and a process-wide daily
  count of SQL runs (agentcrew/caps.py, separate from the agent's daily run
  cap), both checked before anything runs;
* one run-log line per run (agentcrew/runlog.py, surface sql_editor);
* errors as one short plain sentence, never a stack trace or a driver's
  multi-line dump (the agent reads the raw text to repair its query; a
  person needs the gist).

No model is called here, ever.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from agentcrew import runlog
from agentcrew.caps import DailyCap
from agentcrew.schemas import ExecutionResult, FailureKind
from agentcrew.tools import ToolContext, execute_readonly_sql

log = logging.getLogger(__name__)

SQL_RUNS_USED = "sql_runs_used"
"""The session-state key holding how many editor runs this session made."""

MAX_SQL_CHARS = 20_000
"""Longer than any query the agent writes; refused before the guard parses it."""

MAX_ERROR_CHARS = 200

EMPTY_MESSAGE = "Enter a SQL statement to run."
TOO_LONG_MESSAGE = f"That statement is too long (over {MAX_SQL_CHARS:,} characters)."
SESSION_LIMIT_MESSAGE = (
    "You've used this session's SQL runs. Reload the page to start a new session."
)
DAILY_LIMIT_MESSAGE = (
    "The SQL editor has reached its daily limit. Try again tomorrow."
)
FAILED_MESSAGE = "The query could not run."


@dataclass(frozen=True)
class EditorRun:
    """One press of Run: the result when the statement ran, or a short
    message saying why it did not (or why it failed)."""

    result: ExecutionResult | None = None
    message: str | None = None
    charged: bool = False
    """Whether this run counted against the session and daily limits."""
    status: str = "done"
    """For the run log: done, refused, failed, error, invalid, session_limit
    or daily_cap."""
    error: str | None = None
    """For the run log: the failure kind or exception type name, never text."""

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok


def runs_left(state: MutableMapping[str, Any], limit: int) -> int | None:
    """SQL runs this session may still make; None when there is no limit."""
    if limit <= 0:
        return None
    return max(0, limit - int(state.get(SQL_RUNS_USED, 0)))


def run_sql(
    sql: str,
    *,
    tools: ToolContext,
    state: MutableMapping[str, Any],
    session_limit: int,
    cap: DailyCap | None,
    scrub: Callable[[str], str] = lambda text: text,
    trace_content: bool = False,
    surface: str = "sql_editor",
) -> EditorRun:
    """Check the limits, run `sql` through the agent's own guarded path, and
    write one run-log line (agentcrew/runlog.py) for every call, whatever
    the outcome. The SQL text goes in that line only with `trace_content`
    (AGENTCREW_TRACE_CONTENT); result rows never do.

    A run is charged once it passes the limit checks, whatever happens next:
    a refused or failing statement costs the database a parse or a query
    just as a good one does, and charging only successes would let anyone
    hammer the guard for free. Empty and over-long input is not charged.

    `cap` is the process-wide daily cap; its SQL-run counter is used, not
    its agent-run counter. `scrub` removes secrets (the self-hosted database
    URL) from any error text before it is shown.
    """
    started = time.perf_counter()
    text = (sql or "").strip()
    run = _run(text, tools=tools, state=state, session_limit=session_limit,
               cap=cap, scrub=scrub)
    runlog.emit(runlog.RunRecord(
        surface=surface, provider="-", model="-", arch="-", status=run.status,
        seconds=time.perf_counter() - started, error=run.error, key="-",
        rows=run.result.row_count if run.ok and run.result is not None else None,
        final_sql=text if trace_content and text else None,
    ))
    return run


def _run(text: str, *, tools: ToolContext, state: MutableMapping[str, Any],
         session_limit: int, cap: DailyCap | None,
         scrub: Callable[[str], str]) -> EditorRun:
    if not text:
        return EditorRun(message=EMPTY_MESSAGE, status="invalid")
    if len(text) > MAX_SQL_CHARS:
        return EditorRun(message=TOO_LONG_MESSAGE, status="invalid")
    if runs_left(state, session_limit) == 0:
        return EditorRun(message=SESSION_LIMIT_MESSAGE, status="session_limit")
    if cap is not None and not cap.allows_sql_run():
        return EditorRun(message=DAILY_LIMIT_MESSAGE, status="daily_cap")

    state[SQL_RUNS_USED] = int(state.get(SQL_RUNS_USED, 0)) + 1
    if cap is not None:
        cap.charge_sql_run()

    try:
        result = execute_readonly_sql(tools, text)
    except Exception as exc:  # execute_readonly_sql reports failures as data
        log.warning("sql editor run failed: %s", type(exc).__name__)
        return EditorRun(message=FAILED_MESSAGE, charged=True, status="error",
                         error=type(exc).__name__)
    if not result.ok:
        refused = result.failure == FailureKind.BLOCKED
        return EditorRun(result=result, charged=True,
                         message=plain_error(result, tools.timeout, scrub),
                         status="refused" if refused else "failed",
                         error=str(result.failure))
    return EditorRun(result=result, charged=True)


_DRIVER_PREFIX = re.compile(r"^\((?:[\w.]+\.)?\w+(?:Error|Exception|Warning)\)\s*")
_SQLGLOT_MARKUP = re.compile(r"\x1b\[[0-9;]*m")


def plain_error(result: ExecutionResult, timeout: float,
                scrub: Callable[[str], str] = lambda text: text) -> str:
    """One short sentence for a failed result: no driver class names, no
    echoed SQL, no second line."""
    if result.failure == FailureKind.TIMEOUT:
        return f"The query took longer than {timeout:g} seconds and was stopped."
    raw = scrub(_SQLGLOT_MARKUP.sub("", result.error_message or ""))
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    first = _DRIVER_PREFIX.sub("", first)
    if len(first) > MAX_ERROR_CHARS:
        first = first[: MAX_ERROR_CHARS - 3].rstrip() + "..."
    if result.failure == FailureKind.BLOCKED:
        return f"Refused: {first or 'only read-only queries can run.'}"
    return first or FAILED_MESSAGE
