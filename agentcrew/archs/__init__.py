"""Architectures selectable with AGENTCREW_ARCH (eval/PROTOCOL.md, protocol v2).

    baseline  A: one SQL from the shared context, one retry on error, answer
    v1        B: the original agent, unchanged (reference only)
    router    C: A as step 0, plus escalation, disciplined repair and one
              assembled final table when several steps are used
    full      D: C plus an exploration loop, compute tools and
              verification checks on the escalated path

A, C and D get the same shared context; only the control flow differs.
"""

from __future__ import annotations

from agentcrew.archs.baseline import run_baseline
from agentcrew.archs.full import run_full
from agentcrew.archs.outcome import ArchOutcome, RepairEvent, SqlAttempt
from agentcrew.archs.router import run_router
from agentcrew.archs.v1 import run_v1
from agentcrew.config import Settings
from agentcrew.context import ContextBuilder
from agentcrew.llm import LLMClient
from agentcrew.tools import ToolContext
from agentcrew.tracer import Tracer

ARCHS = ("baseline", "v1", "router", "full")

__all__ = ["ARCHS", "ArchOutcome", "RepairEvent", "SqlAttempt", "run_architecture"]


def run_architecture(
    arch: str,
    question: str,
    *,
    settings: Settings,
    llm: LLMClient,
    tools: ToolContext,
    context_builder: ContextBuilder,
    tracer: Tracer | None = None,
    step0_llm: LLMClient | None = None,
    shared_llm: LLMClient | None = None,
) -> ArchOutcome:
    """`step0_llm` serves step 0 (A's first call) and the direct answer of C
    and D; `shared_llm` serves D's C-identical calls (step-0 repairs and the
    follow-up decision). The eval runner points them at A's and C's caches."""
    if arch == "baseline":
        return run_baseline(
            question, llm=llm, tools=tools,
            context=context_builder.build(question), settings=settings,
        )
    if arch == "router":
        return run_router(
            question, llm=llm, tools=tools,
            context=context_builder.build(question), settings=settings,
            step0_llm=step0_llm,
        )
    if arch == "full":
        return run_full(
            question, llm=llm, tools=tools,
            context=context_builder.build(question), settings=settings,
            step0_llm=step0_llm, shared_llm=shared_llm,
        )
    if arch == "v1":
        return run_v1(question, llm=llm, settings=settings,
                      tracer=tracer or Tracer(trace_dir=None))
    raise ValueError(f"unknown architecture {arch!r}; choose from {ARCHS}")
