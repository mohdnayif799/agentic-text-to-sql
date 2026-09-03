"""Graph assembly and the public entry point.

Verified against langgraph==1.2.11 (introspected 2026-08-25):
`StateGraph(state_schema)`, `add_node`, `add_conditional_edges(source, path,
path_map)`, `compile(checkpointer=...)`, `START`/`END` from `langgraph.graph`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from agentcrew.config import Settings, get_settings
from agentcrew.db.engine import read_only_engine
from agentcrew.graph import nodes
from agentcrew.graph.control import Budget, LoopGuard
from agentcrew.graph.state import AgentState, Deps, initial_state
from agentcrew.llm import LLMClient, build_client
from agentcrew.schemas import StepResult
from agentcrew.tools import ToolContext
from agentcrew.tracer import Tracer, build_tracer


def build_graph(deps: Deps):
    """Wire the state machine.

    9 nodes, 5 routers. Every edge is either unconditional or decided by a
    router that reads state only - there is no place where a model chooses the
    next node.
    """
    g = StateGraph(AgentState)

    g.add_node("plan", partial(nodes.plan_node, deps=deps))
    g.add_node("select_schema", partial(nodes.select_schema_node, deps=deps))
    g.add_node("author_sql", partial(nodes.author_sql_node, deps=deps))
    g.add_node("execute_sql", partial(nodes.execute_sql_node, deps=deps))
    g.add_node("triage", partial(nodes.triage_node, deps=deps))
    g.add_node("verify", partial(nodes.verify_node, deps=deps))
    g.add_node("repair", partial(nodes.repair_node, deps=deps))
    g.add_node("advance", partial(nodes.advance_node, deps=deps))
    g.add_node("synthesize", partial(nodes.synthesize_node, deps=deps))
    g.add_node("clarify", partial(nodes.clarify_node, deps=deps))
    g.add_node("fail", partial(nodes.fail_node, deps=deps))

    g.add_edge(START, "plan")
    g.add_conditional_edges(
        "plan",
        nodes.route_after_plan,
        {"select_schema": "select_schema", "clarify": "clarify", "fail": "fail"},
    )
    g.add_edge("select_schema", "author_sql")
    g.add_conditional_edges(
        "author_sql",
        nodes.route_after_author,
        {"execute_sql": "execute_sql", "fail": "fail"},
    )
    g.add_edge("execute_sql", "triage")
    g.add_conditional_edges(
        "triage",
        nodes.route_on_next_action,
        {"verify": "verify", "repair": "repair", "fail": "fail"},
    )
    g.add_conditional_edges(
        "repair",
        nodes.route_after_author,
        {"execute_sql": "execute_sql", "fail": "fail"},
    )
    g.add_conditional_edges(
        "verify",
        nodes.route_on_next_action,
        {"advance": "advance", "repair": "repair", "fail": "fail"},
    )
    g.add_conditional_edges(
        "advance",
        nodes.route_after_advance,
        {"author_sql": "author_sql", "synthesize": "synthesize"},
    )
    g.add_edge("synthesize", END)
    g.add_edge("clarify", END)
    g.add_edge("fail", END)

    return g.compile()


@dataclass
class RunOutcome:
    """Everything the UI, the eval harness and the tests need."""

    question: str
    status: str
    answer: str | None
    key_numbers: list[str]
    caveats: list[str]
    steps: list[str]
    step_results: list[StepResult]
    selected_tables: list[str]
    schema_debug: dict[str, Any]
    stop_reason: str
    trace: dict[str, Any]
    trace_path: Path | None
    state: AgentState

    @property
    def succeeded(self) -> bool:
        return self.status == "done"

    @property
    def sql_statements(self) -> list[str]:
        return [r.sql for r in self.step_results]

    @property
    def total_attempts(self) -> int:
        return sum(r.attempts_used for r in self.step_results)


def build_deps(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    tracer: Tracer | None = None,
) -> Deps:
    """Assemble collaborators. `llm` override is what tests use."""
    settings = settings or get_settings()
    engine = read_only_engine(
        settings.database_path, timeout=settings.sql_timeout_seconds
    )
    tools = ToolContext.create(
        engine,
        max_rows=settings.max_result_rows,
        timeout=settings.sql_timeout_seconds,
        allow_write=settings.allow_write_mode,
    )
    return Deps(
        llm=llm or build_client(settings),
        tools=tools,
        tracer=tracer or build_tracer(settings),
        budget=Budget(
            max_attempts_per_step=settings.max_attempts_per_step,
            max_steps=settings.max_steps,
            max_llm_calls=settings.max_llm_calls,
            max_sql_executions=settings.max_sql_executions,
            wall_clock_seconds=settings.wall_clock_seconds,
        ),
        loop_guard=LoopGuard(),
        max_tables_in_context=settings.max_tables_in_context,
        sample_values_per_column=settings.sample_values_per_column,
        max_distinct_for_sampling=settings.max_distinct_for_sampling,
        max_output_tokens=settings.max_output_tokens,
        temperature=settings.temperature,
    )


def run_question(
    question: str,
    *,
    settings: Settings | None = None,
    deps: Deps | None = None,
    persist_trace: bool = True,
) -> RunOutcome:
    """Answer one question end to end."""
    settings = settings or get_settings()
    deps = deps or build_deps(settings)
    graph = build_graph(deps)

    # recursion_limit is LangGraph's own backstop; our budgets should always
    # trip first. If this one fires, it is a bug in the budget logic.
    final: AgentState = graph.invoke(
        initial_state(question),
        config={"recursion_limit": 60},
    )

    trace_path = deps.tracer.flush() if persist_trace else None
    return RunOutcome(
        question=question,
        status=final.get("status", "failed"),
        answer=final.get("final_answer"),
        key_numbers=final.get("key_numbers") or [],
        caveats=final.get("caveats") or [],
        steps=final.get("steps") or [],
        step_results=final.get("step_results") or [],
        selected_tables=final.get("selected_tables") or [],
        schema_debug=final.get("schema_debug") or {},
        stop_reason=final.get("stop_reason", "none"),
        trace=deps.tracer.to_dict(),
        trace_path=trace_path,
        state=final,
    )
