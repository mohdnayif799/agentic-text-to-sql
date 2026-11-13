"""Tracing.

Three sinks, and the split matters:

* **JSONL recorder (always on, local; the original agent, B).** Every node
  entry/exit, tool call, SQL execution, retry and token count lands in one
  append-only file per run. This is what the Streamlit trace panel reads for
  B. It has no dependencies and works offline.
* **Structured run log (always on; A, C, D).** One JSON line per run on
  stdout, agentcrew/runlog.py. That is the production trace of the router
  path: it reaches Cloud Logging with no setup.
* **Langfuse (optional extra).** On only when AGENTCREW_LANGFUSE_PUBLIC_KEY
  and _SECRET_KEY are set (and AGENTCREW_LANGFUSE_ENABLED is not false). Used
  by B's Tracer (LangfuseSink) and by the v2 run trace below (LangfuseRunSink).

Core debuggability must never depend on a SaaS being reachable, so Langfuse is
strictly additive: if it is off, fails or is slow, the run is unaffected. The
run trace never waits on the network (the client exports in the background;
nothing here calls flush on a request), and its first failure is logged once
rather than swallowed, so "on but broken" is visible.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Span:
    name: str
    kind: str  # node | tool | llm | sql
    start_ms: float
    end_ms: float | None = None
    duration_ms: float | None = None
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Tracer:
    """Collects spans for one agent run."""

    def __init__(
        self,
        run_id: str | None = None,
        *,
        trace_dir: Path | None = None,
        langfuse_sink: Any | None = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.spans: list[Span] = []
        self.started = time.perf_counter()
        self._trace_dir = Path(trace_dir) if trace_dir else None
        self._langfuse = langfuse_sink
        self.counters: dict[str, int] = {
            "llm_calls": 0,
            "sql_executions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "repairs": 0,
        }

    # -- recording ---------------------------------------------------------
    @contextmanager
    def span(self, name: str, kind: str, **data: Any) -> Iterator[Span]:
        s = Span(
            name=name,
            kind=kind,
            start_ms=round((time.perf_counter() - self.started) * 1000, 2),
            data=dict(data),
        )
        self.spans.append(s)
        try:
            yield s
        except Exception as exc:
            s.ok = False
            s.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            s.end_ms = round((time.perf_counter() - self.started) * 1000, 2)
            s.duration_ms = round(s.end_ms - s.start_ms, 2)
            self._emit(s)

    def bump(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def _emit(self, span: Span) -> None:
        if self._langfuse is not None:
            try:
                self._langfuse.record(self.run_id, span)
            except Exception:  # noqa: S110 - see module docstring
                pass  # observability must never break the run

    # -- output ------------------------------------------------------------
    @property
    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "counters": dict(self.counters),
            "spans": [asdict(s) for s in self.spans],
        }

    def flush(self) -> Path | None:
        """Write the run to disk. Returns the path, or None if disabled."""
        if self._trace_dir is None:
            return None
        self._trace_dir.mkdir(parents=True, exist_ok=True)
        path = self._trace_dir / f"run_{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        if self._langfuse is not None:
            try:
                self._langfuse.flush()
            except Exception:  # noqa: S110 - observability is strictly additive
                pass
        return path


class LangfuseSink:
    """Optional Langfuse forwarder. Constructed only when enabled."""

    def __init__(self, public_key: str, secret_key: str, host: str) -> None:
        from langfuse import Langfuse  # imported lazily on purpose

        self._client = Langfuse(
            public_key=public_key, secret_key=secret_key, host=host
        )

    def record(self, run_id: str, span: Span) -> None:
        self._client.create_event(
            name=f"{span.kind}:{span.name}",
            metadata={
                "run_id": run_id,
                "duration_ms": span.duration_ms,
                "ok": span.ok,
                **{k: str(v)[:500] for k, v in span.data.items()},
            },
        )

    def flush(self) -> None:
        self._client.flush()


#: Shared with app_runner's logger tree: app.py attaches the stderr handler
#: to "agentcrew.app", and Cloud Run forwards stderr to Cloud Logging.
_log = logging.getLogger("agentcrew.app.langfuse")


def langfuse_wanted(settings: Any) -> bool:
    """Langfuse is on when both keys are set, unless AGENTCREW_LANGFUSE_ENABLED
    is explicitly false. Keys alone switch it on, so a deployment needs no
    extra flag; the flag remains as an off switch that keeps the keys."""
    keys = bool(
        getattr(settings, "langfuse_public_key", None)
        and getattr(settings, "langfuse_secret_key", None)
    )
    enabled = getattr(settings, "langfuse_enabled", None)
    if enabled is None:
        return keys
    return bool(enabled) and keys


def build_tracer(settings: Any, run_id: str | None = None) -> Tracer:
    sink = None
    if langfuse_wanted(settings):
        try:
            sink = LangfuseSink(
                settings.langfuse_public_key,
                settings.langfuse_secret_key,
                settings.langfuse_host,
            )
        except Exception as exc:
            _log.warning("langfuse: could not start the client for the v1 "
                         "tracer: %s: %s", type(exc).__name__, exc)
            sink = None
    return Tracer(run_id, trace_dir=settings.trace_dir, langfuse_sink=sink)


# ---------------------------------------------------------------------------
# Run-level tracing for the v2 architectures (A, C, D), used by the app
# ---------------------------------------------------------------------------
#
# What a trace holds, by design (the same policy as agentcrew/runlog.py):
#   always      architecture, provider, model, status, path, the tools of each
#               step, call count, seconds; per call its role (the first line of
#               the system prompt, fixed text), model, tokens, seconds, status
#   only with   the question text and the final SQL
#   trace_content
#   never       prompts, completions, result rows, previews, values from an
#               uploaded dataset, keys, an error's message text
#
# langfuse>=4 is the OpenTelemetry-based client: start_observation() opens a
# span-like observation, update() sets fields, end() closes it, and export
# happens on the client's own background thread.


class RunTrace:
    """One run's trace. Every method is safe to call: a failure is logged once
    by the sink and otherwise ignored, because tracing is strictly additive."""

    def __init__(self, sink: LangfuseRunSink, root: Any) -> None:
        self._sink = sink
        self._root = root

    def call_started(self, *, role: str, model: str) -> Any | None:
        """A generation for one model call; returns a handle for call_ended."""
        try:
            return self._root.start_observation(
                name=f"llm:{role}", as_type="generation", model=model or None,
                metadata={"role": role},
            )
        except Exception as exc:
            self._sink.warn(exc)
            return None

    def call_ended(self, handle: Any | None, *, seconds: float, status: str,
                   usage: Any | None = None) -> None:
        if handle is None:
            return
        try:
            details: dict[str, int] | None = None
            if usage is not None:
                details = {"input": int(usage.input_tokens),
                           "output": int(usage.output_tokens)}
                if getattr(usage, "thoughts_tokens", 0):
                    details["reasoning"] = int(usage.thoughts_tokens)
            handle.update(
                metadata={"seconds": round(seconds, 3), "status": status},
                usage_details=details,
                level="DEFAULT" if status == "ok" else "ERROR",
                status_message=None if status == "ok" else status,
            )
            handle.end()
        except Exception as exc:
            self._sink.warn(exc)

    def finish(self, *, status: str, path: str, tools: list[list[str]],
               llm_calls: int, seconds: float, final_sql: str | None = None,
               error: str | None = None) -> None:
        """Close the trace. `final_sql` is recorded only with trace_content;
        `error` is an exception's type name, never its text."""
        try:
            output: dict[str, Any] = {
                "status": status, "path": path,
                "tools": ">".join("+".join(step) for step in tools) or "-",
            }
            if self._sink.trace_content and final_sql:
                output["final_sql"] = final_sql
            self._root.update(
                output=output,
                metadata={"llm_calls": llm_calls, "seconds": round(seconds, 3),
                          "error": error},
                level="DEFAULT" if error is None else "ERROR",
                status_message=error,
            )
            self._root.end()
        except Exception as exc:
            self._sink.warn(exc)


class LangfuseRunSink:
    """Builds one RunTrace per run on a Langfuse client."""

    def __init__(self, client: Any, *, trace_content: bool = False) -> None:
        self._client = client
        self.trace_content = bool(trace_content)
        self._warned = False

    def start(self, *, arch: str, provider: str, model: str,
              question: str) -> RunTrace | None:
        try:
            root = self._client.start_observation(
                name="agentcrew.run", as_type="agent",
                input={"question": question} if self.trace_content else None,
                metadata={"arch": arch, "provider": provider, "model": model,
                          "trace_content": self.trace_content},
            )
        except Exception as exc:
            self.warn(exc)
            return None
        return RunTrace(self, root)

    def warn(self, exc: BaseException) -> None:
        """Log the first failure; tracing must never break or spam a run."""
        if not self._warned:
            self._warned = True
            _log.warning("langfuse: tracing failed and is being skipped: %s: %s",
                         type(exc).__name__, exc)


def langfuse_client_factory(settings: Any) -> Any:
    """The real client. Imported lazily so the package stays optional."""
    from langfuse import Langfuse  # imported lazily on purpose

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )


def build_run_sink(
    settings: Any,
    *,
    client_factory: Callable[[Any], Any] = langfuse_client_factory,
    check_auth: bool = True,
) -> LangfuseRunSink | None:
    """A sink when Langfuse is wanted (langfuse_wanted), otherwise None. Says
    on the log what it decided: an operator who set the keys must be able to
    tell "off", "on" and "on but broken" apart."""
    if not langfuse_wanted(settings):
        if getattr(settings, "langfuse_enabled", None):
            _log.warning("langfuse: AGENTCREW_LANGFUSE_ENABLED is true but "
                         "AGENTCREW_LANGFUSE_PUBLIC_KEY or _SECRET_KEY is "
                         "missing; tracing is off")
        return None
    try:
        client = client_factory(settings)
    except Exception as exc:
        _log.warning("langfuse: could not start the client, tracing is off: "
                     "%s: %s (pip install langfuse)", type(exc).__name__, exc)
        return None
    sink = LangfuseRunSink(client, trace_content=settings.trace_content)
    _log.info("langfuse: enabled host=%s trace_content=%s",
              settings.langfuse_host, "on" if sink.trace_content else "off")
    if check_auth:
        # Off the request path: a credential check is a network call, and a
        # slow one must not delay the first visitor.
        def _check() -> None:
            try:
                ok = client.auth_check()
            except Exception as exc:
                _log.warning("langfuse: credential check failed: %s: %s",
                             type(exc).__name__, exc)
                return
            (_log.info if ok else _log.warning)(
                "langfuse: credential check %s", "passed" if ok else "FAILED")

        threading.Thread(target=_check, name="langfuse-auth", daemon=True).start()
    return sink
