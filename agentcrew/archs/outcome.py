"""The common result of every architecture (A baseline, B v1, C router, D full)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentcrew.schemas import ExecutionResult

PREVIEW_ROWS = 5


@dataclass(frozen=True)
class SqlAttempt:
    """One executed query, whatever step produced it."""

    sql: str
    ok: bool
    failure: str
    error: str | None
    row_count: int
    preview: str

    @classmethod
    def from_result(cls, result: ExecutionResult, sql: str = "") -> SqlAttempt:
        return cls(
            sql=result.sql or sql,
            ok=result.ok,
            failure=str(result.failure),
            error=result.error_message,
            row_count=result.row_count,
            preview=result.preview(limit=PREVIEW_ROWS),
        )


@dataclass(frozen=True)
class RepairEvent:
    """One repair attempt on one step, on a concrete signal.

    `before` is the query's result when the signal was raised and is kept;
    `after` replaces it only if `accepted` (the signal was resolved).
    """

    step: int
    signal: str
    detail: str
    before: ExecutionResult
    after: ExecutionResult | None
    accepted: bool
    attempts: int = 1
    via: str = "repair"
    """"repair" (C's repair call) or "A's retry" (A's own retry call)."""


@dataclass(frozen=True)
class ArchOutcome:
    """What an architecture produced for one question.

    `selected` is the result the final answer was built from; it is what the
    evaluation grades. `path` is "direct" or "escalated" for the v2
    architectures and "v1" for the original agent.
    """

    arch: str
    question: str
    status: str
    answer: str
    selected: ExecutionResult | None
    path: str
    plan: tuple[str, ...] = ()
    attempts: tuple[SqlAttempt, ...] = ()
    steps: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    key_numbers: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    stop_reason: str = "none"
    error: str | None = None
    repairs: tuple[RepairEvent, ...] = ()
    selected_step: int | None = None
    """Step whose rows form the graded result (the base step if assembled)."""

    @property
    def succeeded(self) -> bool:
        return self.status == "done"
