"""Loop control and budgets.

This module is the answer to "how do you stop the agent looping forever?" and
it is deliberately separate from the nodes so it can be unit-tested without a
model, a database or a graph.

Three independent stopping mechanisms:

1. **Budgets.** Hard ceilings on attempts per step, total LLM calls, total SQL
   executions and wall-clock time. Any breach ends the run with an
   explanation, never with a fabricated answer.
2. **Fingerprint loop detection.** If the model emits a query it has already
   tried (after normalisation), that is not progress. We do not spend a fresh
   attempt on it; we escalate the instruction, and a second repeat aborts the
   step. This catches the classic
   ``generate -> fail -> regenerate identical -> fail`` spiral, which a plain
   retry counter would happily run to exhaustion.
3. **Monotonic progress requirement.** A step only advances on a passing
   verification, so a query that runs cleanly but answers the wrong question
   cannot silently terminate the loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class StopReason(StrEnum):
    NONE = "none"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    LLM_BUDGET = "llm_budget_exceeded"
    SQL_BUDGET = "sql_budget_exceeded"
    TIME_BUDGET = "time_budget_exceeded"
    REPEATED_QUERY = "repeated_query"
    STEP_LIMIT = "step_limit_exceeded"


@dataclass
class Budget:
    """Mutable run budget. One instance per agent run."""

    max_attempts_per_step: int = 3
    max_steps: int = 4
    max_llm_calls: int = 30
    max_sql_executions: int = 24
    wall_clock_seconds: float = 180.0

    llm_calls: int = 0
    sql_executions: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def note_llm_call(self, n: int = 1) -> None:
        self.llm_calls += n

    def note_sql(self, n: int = 1) -> None:
        self.sql_executions += n

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> StopReason:
        """Global budget check. Called before every expensive operation."""
        if self.llm_calls >= self.max_llm_calls:
            return StopReason.LLM_BUDGET
        if self.sql_executions >= self.max_sql_executions:
            return StopReason.SQL_BUDGET
        if self.elapsed >= self.wall_clock_seconds:
            return StopReason.TIME_BUDGET
        return StopReason.NONE

    def exhausted(self) -> bool:
        return self.check() is not StopReason.NONE


@dataclass
class LoopGuard:
    """Per-step duplicate-query detector."""

    seen: dict[str, int] = field(default_factory=dict)
    repeat_limit: int = 2
    """How many times the same normalised query may appear before we abort."""

    def observe(self, fingerprint: str) -> tuple[bool, int]:
        """Record a query.

        Returns:
            (is_repeat, times_seen_including_this_one)
        """
        count = self.seen.get(fingerprint, 0) + 1
        self.seen[fingerprint] = count
        return count > 1, count

    def should_abort(self, fingerprint: str) -> bool:
        return self.seen.get(fingerprint, 0) >= self.repeat_limit

    def reset(self) -> None:
        self.seen.clear()


def explain_stop(reason: StopReason, *, step_goal: str | None = None) -> str:
    """User-facing wording for every stop condition.

    Written out explicitly so the agent never has to improvise an excuse, and
    so the failure surface is reviewable in one place.
    """
    goal = f" while working on: {step_goal}" if step_goal else ""
    return {
        StopReason.ATTEMPTS_EXHAUSTED: (
            f"I could not produce a query that correctly answers this question"
            f"{goal}. I tried several times and each attempt either failed to "
            "run or did not measure the right thing. Rather than guess, I am "
            "stopping here."
        ),
        StopReason.REPEATED_QUERY: (
            f"I got stuck generating the same query repeatedly{goal}, which "
            "means further retries would not help. Stopping instead of looping."
        ),
        StopReason.LLM_BUDGET: (
            "I reached the limit on model calls for a single question before "
            "reaching a verified answer."
        ),
        StopReason.SQL_BUDGET: (
            "I reached the limit on database queries for a single question "
            "before reaching a verified answer."
        ),
        StopReason.TIME_BUDGET: (
            "I ran out of time on this question before reaching a verified "
            "answer."
        ),
        StopReason.STEP_LIMIT: (
            "The analysis plan needed more steps than allowed for one question."
        ),
        StopReason.NONE: "",
    }[reason]
