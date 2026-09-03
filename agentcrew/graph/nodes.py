"""Graph nodes.

Each node is a pure-ish function of (state, deps) returning a partial state
update. Nodes never decide the next node; the routers at the bottom of this
file do, based only on state. That separation is what makes control flow
reviewable.

Node inventory and justification
--------------------------------
plan          LLM. Turns a question into ordered goals; flags real ambiguity.
select_schema DETERMINISTIC. Was an "agent" in the original sketch; demoted
              because introspection + ranking needs no model. See db/selector.
author_sql    LLM. Writes one query for the current goal.
execute_sql   DETERMINISTIC. Guard, run, capture. Errors become data.
verify        LLM. Semantic check only; runs *after* the deterministic triage
              has already handled errors, so we never pay a model to notice a
              syntax error.
repair        LLM. Fixes a query using the real failure text.
advance       DETERMINISTIC. Commits a verified step, resets per-step state.
synthesize    LLM. Writes the final answer from verified results only.
fail          DETERMINISTIC. Explains why it stopped. Never invents an answer.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from agentcrew import prompts as P
from agentcrew.db.selector import render_schema_card, select_tables
from agentcrew.graph.control import StopReason, explain_stop
from agentcrew.graph.state import AgentState, Deps
from agentcrew.llm import StructuredOutputError
from agentcrew.schemas import (
    Attempt,
    ExecutionResult,
    FailureKind,
    FinalAnswer,
    PlanOutput,
    SqlDraft,
    StepResult,
    Verification,
    VerificationVerdict,
)
from agentcrew.tools import execute_readonly_sql

MAX_HISTORY_IN_PROMPT = 3


def _structured(deps: Deps, schema: type, *, system: str, user: str) -> Any:
    """Budgeted structured LLM call with usage accounting."""
    obj, usage = deps.llm.complete_structured(
        system=system,
        user=user,
        schema=schema,
        max_tokens=deps.max_output_tokens,
        temperature=deps.temperature,
    )
    deps.budget.note_llm_call(usage.calls)
    deps.tracer.bump("llm_calls", usage.calls)
    deps.tracer.bump("input_tokens", usage.input_tokens)
    deps.tracer.bump("output_tokens", usage.output_tokens)
    return obj


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def plan_node(state: AgentState, deps: Deps) -> AgentState:
    question = state["question"]
    with deps.tracer.span("plan", "node", question=question):
        table_names = ", ".join(sorted(deps.catalog.tables))
        try:
            plan: PlanOutput = _structured(
                deps,
                PlanOutput,
                system=P.PLANNER_SYSTEM.format(
                    max_steps=deps.budget.max_steps, table_names=table_names
                ),
                user=P.PLANNER_USER.format(
                    question=question, today=date.today().isoformat()
                ),
            )
        except StructuredOutputError as exc:
            return {"status": "failed", "error": str(exc), "stop_reason": "planner"}

        if plan.ambiguous and plan.clarifying_question:
            return {
                "intent": plan.intent,
                "needs_clarification": True,
                "clarifying_question": plan.clarifying_question,
                "status": "needs_clarification",
            }

        steps = plan.steps[: deps.budget.max_steps] or [question]
        return {
            "intent": plan.intent,
            "steps": steps,
            "step_index": 0,
            "needs_clarification": False,
        }


def select_schema_node(state: AgentState, deps: Deps) -> AgentState:
    """Deterministic. No LLM call, no tokens, microseconds."""
    question = state["question"]
    goals = " ".join(state.get("steps") or [])
    with deps.tracer.span("select_schema", "node") as span:
        selection = select_tables(
            f"{question} {goals}",
            deps.catalog,
            max_tables=deps.max_tables_in_context,
        )
        card = render_schema_card(
            deps.catalog,
            selection.tables,
            engine=deps.engine,
            sample_limit=deps.sample_values_per_column,
            max_distinct=deps.max_distinct_for_sampling,
        )
        span.data.update(
            {
                "tables": selection.tables,
                "seeded": selection.seeded_by,
                "fk_expanded": selection.expanded_by_fk,
                "card_chars": len(card),
            }
        )
        return {
            "schema_card": card,
            "selected_tables": selection.tables,
            "schema_debug": {
                "scores": {k: v for k, v in selection.scores.items() if v > 0},
                "seeded_by": selection.seeded_by,
                "expanded_by_fk": selection.expanded_by_fk,
            },
        }


def _prior_context(state: AgentState) -> str:
    results = state.get("step_results") or []
    if not results:
        return ""
    lines = ["\nAlready established by earlier steps:"]
    for r in results:
        lines.append(f"- {r.goal}\n  {r.result.preview(limit=6)}")
    return "\n".join(lines)


def author_sql_node(state: AgentState, deps: Deps) -> AgentState:
    goal = state["steps"][state["step_index"]]
    with deps.tracer.span("author_sql", "node", goal=goal):
        try:
            draft: SqlDraft = _structured(
                deps,
                SqlDraft,
                system=P.SQL_AUTHOR_SYSTEM,
                user=P.SQL_AUTHOR_USER.format(
                    question=state["question"],
                    goal=goal,
                    schema_card=state["schema_card"],
                    prior_context=_prior_context(state),
                ),
            )
        except StructuredOutputError as exc:
            return {"status": "failed", "error": str(exc), "stop_reason": "author"}
        return {"current_sql": draft.sql, "repair_hint": draft.expected_shape}


def _render_history(attempts: list[Attempt]) -> str:
    if not attempts:
        return "(no previous attempts)"
    parts = []
    for a in attempts[-MAX_HISTORY_IN_PROMPT:]:
        verdict = ""
        if a.verification is not None:
            verdict = (
                f"\n  verdict: {a.verification.verdict} - {a.verification.reason}"
            )
        parts.append(
            f"Attempt {a.n}:\n  SQL: {a.sql}\n"
            f"  outcome: {a.result.preview(limit=5)}{verdict}"
        )
    return "\n\n".join(parts)


def repair_node(state: AgentState, deps: Deps) -> AgentState:
    goal = state["steps"][state["step_index"]]
    attempts = state.get("attempts") or []
    hint = state.get("repair_hint")
    hint_block = f"Specific guidance: {hint}\n" if hint else ""

    with deps.tracer.span("repair", "node", goal=goal, attempt=len(attempts) + 1):
        deps.tracer.bump("repairs")
        try:
            draft: SqlDraft = _structured(
                deps,
                SqlDraft,
                system=P.REPAIR_SYSTEM,
                user=P.REPAIR_USER.format(
                    question=state["question"],
                    goal=goal,
                    schema_card=state["schema_card"],
                    history=_render_history(attempts),
                    hint_block=hint_block,
                ),
            )
        except StructuredOutputError as exc:
            return {"status": "failed", "error": str(exc), "stop_reason": "repair"}
        return {"current_sql": draft.sql}


def execute_sql_node(state: AgentState, deps: Deps) -> AgentState:
    sql = state.get("current_sql") or ""
    attempts = list(state.get("attempts") or [])
    n = len(attempts) + 1

    with deps.tracer.span("execute_sql", "node", attempt=n) as span:
        result: ExecutionResult = execute_readonly_sql(deps.tools, sql)
        deps.budget.note_sql()
        deps.tracer.bump("sql_executions")

        from agentcrew.db.safety import fingerprint

        fp = fingerprint(result.sql or sql)
        is_repeat, seen = deps.loop_guard.observe(fp)

        span.data.update(
            {
                "ok": result.ok,
                "failure": str(result.failure),
                "rows": result.row_count,
                "duration_ms": result.duration_ms,
                "fingerprint": fp,
                "repeat": is_repeat,
            }
        )
        span.ok = result.ok

        note = None
        if is_repeat:
            note = f"Duplicate query (seen {seen}x) - not counted as progress."

        attempts.append(
            Attempt(
                n=n, sql=result.sql or sql, fingerprint=fp,
                result=result, note=note,
            )
        )
        return {"attempts": attempts}


def verify_node(state: AgentState, deps: Deps) -> AgentState:
    goal = state["steps"][state["step_index"]]
    attempts = list(state["attempts"])
    latest = attempts[-1]

    with deps.tracer.span("verify", "node", goal=goal) as span:
        try:
            verification: Verification = _structured(
                deps,
                Verification,
                system=P.VERIFIER_SYSTEM,
                user=P.VERIFIER_USER.format(
                    goal=goal,
                    sql=latest.sql,
                    expected_shape=state.get("repair_hint") or "(not stated)",
                    row_count=latest.result.row_count,
                    preview=latest.result.preview(),
                ),
            )
        except StructuredOutputError:
            # A verifier that cannot answer must not block progress silently;
            # treat it as a pass but record the degradation.
            verification = Verification(
                verdict=VerificationVerdict.PASS,
                reason="Verifier unavailable; accepted without semantic check.",
            )
        latest.verification = verification
        attempts[-1] = latest
        span.data["verdict"] = str(verification.verdict)

        update: AgentState = {
            "attempts": attempts,
            "repair_hint": verification.repair_hint,
        }

        if verification.verdict is VerificationVerdict.PASS:
            span.data["decision"] = "advance"
            return {**update, "next_action": "advance"}

        budget_stop = deps.budget.check()
        if budget_stop is not StopReason.NONE:
            span.data["decision"] = "fail"
            return {
                **update,
                "next_action": "fail",
                "stop_reason": str(budget_stop),
            }

        distinct_attempts = len({a.fingerprint for a in attempts})
        if distinct_attempts >= deps.budget.max_attempts_per_step:
            # Out of retries on a semantically wrong result: commit it anyway
            # but record it as unverified, rather than discarding work the
            # user may still find useful.
            span.data["decision"] = "advance-unverified"
            return {**update, "next_action": "advance"}

        span.data["decision"] = "repair"
        return {**update, "next_action": "repair"}


def advance_node(state: AgentState, deps: Deps) -> AgentState:
    """Commit the verified step and reset per-step working state."""
    goal = state["steps"][state["step_index"]]
    attempts = state["attempts"]
    latest = attempts[-1]
    results = list(state.get("step_results") or [])
    results.append(
        StepResult(
            goal=goal,
            sql=latest.sql,
            result=latest.result,
            attempts_used=len(attempts),
            verified=latest.verification is not None
            and latest.verification.verdict is VerificationVerdict.PASS,
        )
    )
    deps.loop_guard.reset()
    with deps.tracer.span("advance", "node", step=state["step_index"], goal=goal):
        return {
            "step_results": results,
            "step_index": state["step_index"] + 1,
            "attempts": [],
            "current_sql": None,
            "repair_hint": None,
        }


def synthesize_node(state: AgentState, deps: Deps) -> AgentState:
    results = state.get("step_results") or []
    findings = "\n\n".join(
        f"Step {i + 1}: {r.goal}\nSQL: {r.sql}\nResult:\n{r.result.preview(limit=20)}"
        for i, r in enumerate(results)
    )
    with deps.tracer.span("synthesize", "node", steps=len(results)):
        try:
            final: FinalAnswer = _structured(
                deps,
                FinalAnswer,
                system=P.SYNTHESIS_SYSTEM,
                user=P.SYNTHESIS_USER.format(
                    question=state["question"], findings=findings
                ),
            )
        except StructuredOutputError as exc:
            return {"status": "failed", "error": str(exc), "stop_reason": "synthesis"}
        return {
            "final_answer": final.answer,
            "key_numbers": final.key_numbers,
            "caveats": final.caveats,
            "status": "done",
        }


def clarify_node(state: AgentState, deps: Deps) -> AgentState:
    with deps.tracer.span("clarify", "node"):
        return {
            "status": "needs_clarification",
            "final_answer": state.get("clarifying_question"),
        }


def fail_node(state: AgentState, deps: Deps) -> AgentState:
    reason_raw = state.get("stop_reason") or StopReason.ATTEMPTS_EXHAUSTED
    try:
        reason = StopReason(reason_raw)
    except ValueError:
        reason = StopReason.ATTEMPTS_EXHAUSTED

    steps = state.get("steps") or []
    idx = min(state.get("step_index", 0), max(len(steps) - 1, 0))
    goal = steps[idx] if steps else None

    message = state.get("error") or explain_stop(reason, step_goal=goal)
    attempts = state.get("attempts") or []
    if attempts:
        last = attempts[-1].result
        if not last.ok and last.error_message:
            message += f"\n\nLast database error: {last.error_message}"

    with deps.tracer.span("fail", "node", reason=str(reason)):
        return {
            "status": "failed",
            "final_answer": message,
            "stop_reason": str(reason),
        }


# ---------------------------------------------------------------------------
# Routers - the only place next-node decisions are made
# ---------------------------------------------------------------------------
def route_after_plan(state: AgentState) -> str:
    if state.get("status") == "failed":
        return "fail"
    if state.get("needs_clarification"):
        return "clarify"
    return "select_schema"


def route_after_author(state: AgentState) -> str:
    return "fail" if state.get("status") == "failed" else "execute_sql"


def triage_node(state: AgentState, deps: Deps) -> AgentState:
    """Deterministic triage after a query runs.

    This node is why the LLM verifier is cheap: syntax errors, blocked
    statements, missing objects and empty results are all classified here for
    free. The model is only consulted for results that actually ran and
    returned data - the only case where semantics are genuinely in question.

    It is a *node* rather than a routing function because it needs to write
    `stop_reason` and `repair_hint` into state, and LangGraph routers are
    read-only. Routers below are therefore pure projections of `next_action`.
    """
    attempts = state.get("attempts") or []
    if state.get("status") == "failed" or not attempts:
        return {"next_action": "fail"}

    latest = attempts[-1]
    result = latest.result

    budget_stop = deps.budget.check()
    if budget_stop is not StopReason.NONE:
        return {"next_action": "fail", "stop_reason": str(budget_stop)}

    if deps.loop_guard.should_abort(latest.fingerprint):
        return {
            "next_action": "fail",
            "stop_reason": str(StopReason.REPEATED_QUERY),
        }

    # Duplicate attempts do not consume the retry budget, but they do count
    # toward the repeat limit checked above.
    distinct_attempts = len({a.fingerprint for a in attempts})

    with deps.tracer.span(
        "triage",
        "node",
        ok=result.ok,
        failure=str(result.failure),
        distinct_attempts=distinct_attempts,
    ) as span:
        if not result.ok:
            if distinct_attempts >= deps.budget.max_attempts_per_step:
                span.data["decision"] = "fail"
                return {
                    "next_action": "fail",
                    "stop_reason": str(StopReason.ATTEMPTS_EXHAUSTED),
                }
            span.data["decision"] = "repair"
            return {
                "next_action": "repair",
                "repair_hint": _hint_for_failure(result),
            }

        if result.failure is FailureKind.EMPTY:
            if distinct_attempts >= deps.budget.max_attempts_per_step:
                # An empty result may genuinely be the answer; accept it
                # rather than failing the whole run.
                span.data["decision"] = "verify-empty"
                return {"next_action": "verify"}
            span.data["decision"] = "repair-empty"
            return {
                "next_action": "repair",
                "repair_hint": (
                    "The query ran but returned no rows. Check filter values "
                    "against the sample values in the schema, and check that "
                    "the date range overlaps the data."
                ),
            }

        span.data["decision"] = "verify"
        return {"next_action": "verify"}


def _hint_for_failure(result: ExecutionResult) -> str:
    base = result.error_message or "unknown error"
    return {
        FailureKind.BLOCKED: f"The safety guard rejected the query: {base}",
        FailureKind.MISSING_OBJECT: (
            f"{base}. Re-read the schema and use only listed tables/columns."
        ),
        FailureKind.SYNTAX: f"SQLite rejected the syntax: {base}",
        FailureKind.TYPE_ERROR: f"Type problem: {base}. Cast explicitly.",
        FailureKind.TIMEOUT: (
            "The query timed out. Reduce its scope, add filters, or aggregate "
            "earlier."
        ),
    }.get(result.failure, base)


def route_on_next_action(state: AgentState) -> str:
    """Pure projection. All decisions were made in the preceding node."""
    return state.get("next_action") or "fail"


def route_after_advance(state: AgentState) -> str:
    if state["step_index"] >= len(state.get("steps") or []):
        return "synthesize"
    return "author_sql"
