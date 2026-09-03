"""The graph's shared state.

A single TypedDict flows through every node. Two rules keep it honest:

* Nodes return **partial** updates, never the whole state. That makes each node
  independently testable - you can call it with a hand-built dict and assert
  only on the keys it claims to own.
* Non-serialisable collaborators (engine, LLM client, tracer) live in
  `Deps`, which is passed to nodes via closure, not stored in state. Keeping
  state serialisable is what makes checkpointing and the trace panel work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from sqlalchemy import Engine

from agentcrew.db.introspect import Catalog
from agentcrew.graph.control import Budget, LoopGuard
from agentcrew.llm import LLMClient
from agentcrew.schemas import Attempt, StepResult
from agentcrew.tools import ToolContext
from agentcrew.tracer import Tracer

RunStatus = Literal["running", "done", "failed", "needs_clarification"]


class AgentState(TypedDict, total=False):
    """Serialisable workflow state."""

    question: str
    intent: str

    # planning
    steps: list[str]
    step_index: int
    needs_clarification: bool
    clarifying_question: str | None

    # schema
    schema_card: str
    selected_tables: list[str]
    schema_debug: dict[str, Any]

    # current step working set
    attempts: list[Attempt]
    current_sql: str | None
    repair_hint: str | None

    # routing decision, written by a node and merely *read* by the routers.
    # LangGraph routing functions cannot mutate state, so every decision has
    # to be computed in a node and projected here.
    next_action: str

    # completed work
    step_results: list[StepResult]

    # output
    final_answer: str | None
    key_numbers: list[str]
    caveats: list[str]
    status: RunStatus
    stop_reason: str
    error: str | None


@dataclass
class Deps:
    """Non-serialisable collaborators, injected into nodes via closure."""

    llm: LLMClient
    tools: ToolContext
    tracer: Tracer
    budget: Budget
    loop_guard: LoopGuard
    max_tables_in_context: int = 8
    sample_values_per_column: int = 3
    max_distinct_for_sampling: int = 40
    max_output_tokens: int = 2048
    temperature: float = 0.0

    @property
    def engine(self) -> Engine:
        return self.tools.engine

    @property
    def catalog(self) -> Catalog:
        return self.tools.catalog


def initial_state(question: str) -> AgentState:
    return AgentState(
        question=question,
        intent="",
        steps=[],
        step_index=0,
        needs_clarification=False,
        clarifying_question=None,
        schema_card="",
        selected_tables=[],
        schema_debug={},
        attempts=[],
        current_sql=None,
        repair_hint=None,
        next_action="",
        step_results=[],
        final_answer=None,
        key_numbers=[],
        caveats=[],
        status="running",
        stop_reason="none",
        error=None,
    )
