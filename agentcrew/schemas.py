"""Typed contracts.

Every LLM call in this system returns one of these models. Nothing downstream
ever parses free text, which is what makes the graph testable: a node either
gets a valid object or raises, and the orchestrator decides what to do about it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# LLM output contracts
# --------------------------------------------------------------------------
class PlanOutput(BaseModel):
    """Result of the planning call."""

    intent: str = Field(description="One sentence restating what the user wants.")
    ambiguous: bool = Field(
        default=False,
        description="True only when the question cannot be answered without a "
        "choice the agent has no basis to make.",
    )
    clarifying_question: str | None = None
    steps: list[str] = Field(
        default_factory=list,
        description="Ordered analysis goals. One SQL query per step.",
    )


class SqlDraft(BaseModel):
    """A candidate query for the current step."""

    sql: str
    rationale: str = Field(description="Why this query answers the step goal.")
    expected_shape: str = Field(
        default="",
        description="What the result should look like if correct, e.g. "
        "'one row per region with a numeric decline column'.",
    )


class VerificationVerdict(StrEnum):
    PASS = "pass"  # noqa: S105 - verdict label, not a credential
    WRONG_METRIC = "wrong_metric"
    INSUFFICIENT = "insufficient"


class Verification(BaseModel):
    """Semantic check: does this result actually answer the goal?"""

    verdict: VerificationVerdict
    reason: str
    repair_hint: str | None = Field(
        default=None,
        description="Concrete instruction for the next attempt. Required unless "
        "the verdict is 'pass'.",
    )


class FinalAnswer(BaseModel):
    """Synthesised answer over all completed steps."""

    answer: str = Field(description="Direct prose answer to the user's question.")
    key_numbers: list[str] = Field(
        default_factory=list,
        description="Supporting figures actually present in the query results.",
    )
    caveats: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Internal records (not LLM outputs)
# --------------------------------------------------------------------------
class FailureKind(StrEnum):
    NONE = "none"
    BLOCKED = "blocked"          # rejected by the safety guard
    SYNTAX = "syntax"            # SQL did not parse / DB rejected it
    MISSING_OBJECT = "missing_object"  # unknown table or column
    TYPE_ERROR = "type_error"
    TIMEOUT = "timeout"
    EMPTY = "empty"              # ran fine, returned nothing
    OVERSIZE = "oversize"        # truncated at the row cap
    OTHER = "other"


class ExecutionResult(BaseModel):
    """Outcome of running one query. Deterministic; no LLM involved."""

    ok: bool
    sql: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: float = 0.0
    failure: FailureKind = FailureKind.NONE
    error_message: str | None = None

    def preview(self, limit: int = 12) -> str:
        """Compact text rendering fed back to the model."""
        if not self.ok:
            return f"ERROR ({self.failure}): {self.error_message}"
        if not self.rows:
            return "0 rows returned."
        head = " | ".join(self.columns)
        body = "\n".join(
            " | ".join("NULL" if c is None else str(c) for c in row)
            for row in self.rows[:limit]
        )
        more = (
            f"\n... {self.row_count - limit} more row(s)"
            if self.row_count > limit
            else ""
        )
        return f"{head}\n{body}{more}"


class Attempt(BaseModel):
    """One (draft -> execute -> judge) cycle within a step."""

    n: int
    sql: str
    fingerprint: str
    result: ExecutionResult
    verification: Verification | None = None
    note: str | None = None


class StepResult(BaseModel):
    """A completed analysis step."""

    goal: str
    sql: str
    result: ExecutionResult
    attempts_used: int
    verified: bool
