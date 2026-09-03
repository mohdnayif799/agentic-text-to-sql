"""Tracing.

Two sinks, and the split matters:

* **JSONL recorder (always on, local).** Every node entry/exit, tool call,
  SQL execution, retry and token count lands in one append-only file per run.
  This is what the Streamlit trace panel reads and what you actually debug
  with. It has no dependencies and works offline.
* **Langfuse (optional).** Enabled by env var. Useful for comparing runs over
  time and sharing traces.

Core debuggability must never depend on a SaaS being reachable, so Langfuse is
strictly additive. If it is off or its network call fails, the run is
unaffected.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
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


def build_tracer(settings: Any, run_id: str | None = None) -> Tracer:
    sink = None
    if (
        getattr(settings, "langfuse_enabled", False)
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    ):
        try:
            sink = LangfuseSink(
                settings.langfuse_public_key,
                settings.langfuse_secret_key,
                settings.langfuse_host,
            )
        except Exception:
            sink = None
    return Tracer(run_id, trace_dir=settings.trace_dir, langfuse_sink=sink)
