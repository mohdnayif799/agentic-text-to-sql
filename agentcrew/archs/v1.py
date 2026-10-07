"""Architecture B: the original v1 agent, unchanged, behind the common interface.

The graph, prompts and budgets are v1's own. The adapter only reads its
outcome: the selected result is the last committed step's result (the v1
grading rule), and every executed query is read back from the tracer.
"""

from __future__ import annotations

from agentcrew.archs.outcome import ArchOutcome, SqlAttempt
from agentcrew.config import Settings
from agentcrew.graph.build import build_deps, run_question
from agentcrew.llm import LLMClient
from agentcrew.tracer import Tracer


def _attempts(trace: dict) -> tuple[SqlAttempt, ...]:
    return tuple(
        SqlAttempt(
            sql=span["data"].get("sql", ""),
            ok=bool(span["data"].get("ok")),
            failure=str(span["data"].get("failure", "")),
            error=span["data"].get("error"),
            row_count=int(span["data"].get("rows") or 0),
            preview=span["data"].get("preview", ""),
        )
        for span in trace.get("spans", [])
        if span.get("name") == "execute_sql"
    )


def run_v1(
    question: str, *, llm: LLMClient, settings: Settings, tracer: Tracer
) -> ArchOutcome:
    deps = build_deps(settings, llm=llm, tracer=tracer)
    out = run_question(question, settings=settings, deps=deps, persist_trace=False)
    selected = out.step_results[-1].result if out.step_results else None
    return ArchOutcome(
        arch="v1",
        question=question,
        status=out.status,
        answer=out.answer or "",
        selected=selected,
        path="v1",
        plan=tuple(out.steps),
        attempts=_attempts(out.trace),
        steps=tuple(
            {"step": i + 1, "goal": r.goal, "tools": ["run_sql"], "sql": r.sql}
            for i, r in enumerate(out.step_results)
        ),
        key_numbers=tuple(out.key_numbers),
        caveats=tuple(out.caveats),
        stop_reason=out.stop_reason,
    )
