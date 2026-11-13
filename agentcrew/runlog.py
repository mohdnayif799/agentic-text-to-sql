"""One structured JSON line per run, on stdout.

This is the primary trace of the v2 architectures (A, C, D) in production.
Cloud Run forwards stdout to Cloud Logging, and a line that is one JSON
object becomes a structured log entry there with no agent, no library and
no setup: every field below is filterable (``jsonPayload.path="escalated"``),
``severity`` sets the entry's level and ``message`` is the text shown in the
list view. The same record serves the Streamlit app and, later, the API.

What a line holds, by design:

  always      surface (app, api or sql_editor), which key paid (server or
              own), provider, model, architecture, status, path (direct or
              escalated), the tools of each step, model calls, tokens in /
              out / thinking, seconds, and on failure the exception's type name
  sql_editor  also the row count; no model, so provider, model, arch and
              key are "-", and error is the failure kind (blocked, syntax...)
  api         also client, a short hash of the caller's API key; and one
              "request" line per HTTP request (method, route template,
              status, seconds, error code), whatever became of it
  only with   the question text and the final SQL (AGENTCREW_TRACE_CONTENT,
  trace_content   off by default and off on the public demo)
  never       prompts, completions, result rows, previews, values from an
              uploaded dataset, keys, or an error's message text (provider
              messages can echo request data; the redacted text goes to the
              stderr detail line instead)

Langfuse (agentcrew/tracer.py) is an optional extra beside this, never a
replacement: a line here needs nothing to be reachable.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

LOGGER_NAME = "agentcrew.run"
log = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class RunRecord:
    """Everything one line records. Build it; never log pieces by hand."""

    surface: str
    """Where the run came from: "app", "api" or "sql_editor"."""
    provider: str
    model: str
    arch: str
    status: str
    """done, failed, refused, error, quota, timeout, daily_cap, start_failed."""
    path: str = "-"
    tools: tuple[tuple[str, ...], ...] = ()
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    """An exception's type name (or, for a SQL editor run, the failure kind:
    blocked, syntax, timeout...), never its text."""
    key: str = "server"
    """Which key paid: "server" or "own"."""
    question: str | None = None
    """Only set when trace_content is on."""
    final_sql: str | None = None
    """Only set when trace_content is on."""
    rows: int | None = None
    """Rows returned, for a SQL editor run that ran; None otherwise."""
    client: str | None = None
    """API only: a short hash of the caller's API key (agentcrew/api/auth.py),
    so one caller's lines can be found without the key ever being logged."""


def tools_text(tools: Sequence[Sequence[str]]) -> str:
    """Steps joined by ">", tools within a step by "+": the compact form the
    log has always used, e.g. ``run_sql+repair>compute:decompose_change``."""
    return ">".join("+".join(step) for step in tools) or "-"


def to_dict(record: RunRecord) -> dict[str, Any]:
    """The JSON object for one line. Cloud Logging reads ``severity`` and
    ``message``; the rest lands under jsonPayload as given."""
    tools = tools_text(record.tools)
    payload: dict[str, Any] = {
        "severity": "INFO" if record.error is None else "WARNING",
        "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": "run",
        "message": (
            f"run status={record.status} path={record.path} tools={tools} "
            f"llm_calls={record.llm_calls} seconds={record.seconds:.1f}"
            + (f" rows={record.rows}" if record.rows is not None else "")
            + (f" error={record.error}" if record.error else "")
        ),
        "surface": record.surface,
        "key": record.key,
        "provider": record.provider,
        "model": record.model,
        "arch": record.arch,
        "status": record.status,
        "path": record.path,
        "tools": tools,
        "llm_calls": record.llm_calls,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "thoughts_tokens": record.thoughts_tokens,
        "seconds": round(record.seconds, 3),
        "error": record.error,
    }
    if record.rows is not None:
        payload["rows"] = record.rows
    if record.client is not None:
        payload["client"] = record.client
    if record.question is not None:
        payload["question"] = record.question
    if record.final_sql is not None:
        payload["final_sql"] = record.final_sql
    return payload


@dataclass(frozen=True)
class RequestRecord:
    """One API request, whatever became of it (agentcrew/api). The run lines
    above record what an agent or SQL run did; this records that a request
    arrived, who sent it and how it was answered, including the ones refused
    before any run (401, 413, 429)."""

    method: str
    route: str
    """The route template (/v1/datasets/{dataset_id}/schema), never the raw
    path or the query string."""
    status: int
    seconds: float
    client: str | None = None
    """A short hash of the API key; None when the request had no valid key."""
    code: str | None = None
    """The error envelope's code, when the request failed."""
    request_id: str = ""


def request_dict(record: RequestRecord) -> dict[str, Any]:
    failed = record.status >= 400
    return {
        "severity": "WARNING" if record.status >= 500 else "INFO",
        "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": "request",
        "message": (
            f"request {record.method} {record.route} status={record.status} "
            f"seconds={record.seconds:.1f}"
            + (f" code={record.code}" if failed and record.code else "")
        ),
        "surface": "api",
        "method": record.method,
        "route": record.route,
        "status": record.status,
        "seconds": round(record.seconds, 3),
        "client": record.client,
        "code": record.code,
        "request_id": record.request_id,
    }


def emit_request(record: RequestRecord, logger: logging.Logger = log) -> str:
    """Log one API request line and return it."""
    line = json.dumps(request_dict(record), ensure_ascii=False,
                      separators=(",", ":"))
    logger.log(logging.WARNING if record.status >= 500 else logging.INFO, line)
    return line


def emit(record: RunRecord, logger: logging.Logger = log) -> str:
    """Log the line and return it (tests read the return value)."""
    line = json.dumps(to_dict(record), ensure_ascii=False, separators=(",", ":"))
    logger.log(logging.INFO if record.error is None else logging.WARNING, line)
    return line


def configure(stream: TextIO | None = None) -> None:
    """Send run lines to stdout as bare JSON, once per process.

    Separate from the "agentcrew.app" logger, whose handler prefixes a
    timestamp and level: a prefix would stop Cloud Logging from parsing the
    line as JSON. No propagation, so the root logger cannot print it twice.
    """
    if log.handlers:
        return
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
