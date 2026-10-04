"""Architecture C: router.

Step 0 is the baseline's query (same shared context and SQL rules), returned
as {sql, needs_followup, reason}. The run escalates only on a signal:

* a concrete problem with a query (checks.classify: error, schema, empty,
  sanity) -> disciplined repair of that query;
* needs_followup -> follow-up queries, each seeing the original question
  verbatim, the reason, and capped previews of earlier results.

Otherwise it answers directly from step 0, exactly like A.

Disciplined repair: the schema check runs before executing (no LLM call);
a repair gets the exact signal text; the original query and result are kept,
and a fix is accepted only if it removes that signal and the query runs.
Budgets: at most `max_steps` queries in total and `max_attempts_per_step`
executions per step.

When the answer draws on more than one step, one final table holding every
value it uses is assembled (archs/assemble.py); that table is graded and
shown, and the answer is written from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from agentcrew.archs.answer import write_answer
from agentcrew.archs.assemble import AssemblyPlan, assemble, default_plan
from agentcrew.archs.baseline import SQL_SYSTEM
from agentcrew.archs.checks import Schema, Signal, classify, schema_error, schema_of
from agentcrew.archs.outcome import ArchOutcome, RepairEvent, SqlAttempt
from agentcrew.config import Settings
from agentcrew.context import SharedContext
from agentcrew.llm import LLMClient, StructuredOutputError
from agentcrew.schemas import ExecutionResult, SqlDraft
from agentcrew.tools import ToolContext, execute_readonly_sql

PREVIEW_ROWS = 8

ROUTER_SYSTEM = SQL_SYSTEM + """
Also decide whether this one query fully answers the question. Set \
needs_followup to true only if it does not, because:
- a later query depends on this query's result (a multi-step question);
- the question asks why, or for a drill-down, and explaining it needs further \
breakdowns;
- the answer needs a statistic computed from this query's rows;
- the question can be read more than one way and the reading needs checking.
Otherwise set needs_followup to false. Give the reason in one sentence.
"""

ROUTER_USER = """\
{context}

## Question
{question}
"""

FOLLOWUP_SYSTEM = """\
You are continuing an analysis of a read-only SQLite database. The user's \
question needs more than one query. You see the question, why a follow-up \
is needed, and the queries already run with previews of their results.

Decide the next step:
- If the results so far already contain everything needed to answer the \
question, set done to true and leave sql empty.
- Otherwise write ONE SQLite query for the next step. It may use values from \
earlier results; copy them exactly.

Rules for the query: SQLite dialect; exactly one read-only SELECT (or WITH ... \
SELECT) statement; only the tables and columns in the schema, and text values \
exactly as listed under allowed values; metric definitions from the data \
dictionary; clear aliases for computed columns.
"""

FOLLOWUP_USER = """\
{context}

## Question
{question}

## Why a follow-up is needed
{reason}

## Queries run so far
{history}
"""

REPAIR_SYSTEM = """\
You fix one SQLite query. A concrete problem was detected and is described \
below. Fix only that problem and keep the query's intent; do not resubmit \
the same query.

Rules: SQLite dialect; exactly one read-only SELECT (or WITH ... SELECT) \
statement; only the tables and columns in the schema, and text values \
exactly as listed under allowed values; metric definitions from the data \
dictionary.
"""

REPAIR_USER = """\
{context}

## Question
{question}

## Query
{sql}

## Problem detected ({kind})
{detail}

Write the corrected query.
"""

ASSEMBLY_SYSTEM = """\
Several queries were run to answer one question. Choose the final result \
table that the answer will be written from:
- base_step: the step whose rows answer the question;
- columns: values from OTHER steps that the answer also needs (each as \
{step, column}). Pick only values the answer uses; leave it empty if the base \
step's rows are enough.
"""

ASSEMBLY_USER = """\
## Question
{question}

## Results by step
{history}
"""


class RouterDraft(BaseModel):
    sql: str
    needs_followup: bool = False
    reason: str = Field(default="", description="One sentence.")


class FollowupDraft(BaseModel):
    done: bool = False
    sql: str = ""
    reason: str = ""


@dataclass
class StepRun:
    number: int
    sql: str
    """The executed SQL (after the safety guard, e.g. with a LIMIT added)."""
    result: ExecutionResult
    signal: Signal | None
    reason: str = ""
    model_sql: str = ""
    """The SQL as the model wrote it; repairs work on this."""
    repairs: list[RepairEvent] = field(default_factory=list)
    attempts: list[SqlAttempt] = field(default_factory=list)


def _normalise(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").lower()


class Router:
    def __init__(
        self,
        question: str,
        *,
        llm: LLMClient,
        tools: ToolContext,
        context: SharedContext,
        settings: Settings,
    ) -> None:
        self.question = question
        self.llm = llm
        self.tools = tools
        self.context_text = context.render()
        self.settings = settings
        self.schema: Schema = schema_of(tools.catalog)

    def _structured(self, schema: type, system: str, user: str):
        obj, _ = self.llm.complete_structured(
            system=system, user=user, schema=schema,
            max_tokens=self.settings.max_output_tokens,
            temperature=self.settings.temperature,
        )
        return obj

    def _execute(self, sql: str) -> tuple[ExecutionResult, Signal | None]:
        problem = schema_error(sql, self.schema)  # before executing, no LLM
        result = execute_readonly_sql(self.tools, sql)
        return result, classify(result, problem, self.tools.max_rows)

    def run_step(self, number: int, sql: str, reason: str = "") -> StepRun:
        result, signal = self._execute(sql)
        step = StepRun(number, result.sql or sql, result, signal, reason, sql)
        step.attempts.append(SqlAttempt.from_result(result, sql))
        tries = 1
        while step.signal and tries < self.settings.max_attempts_per_step:
            tries += 1
            prompt = REPAIR_USER.format(
                context=self.context_text, question=self.question,
                sql=step.model_sql, kind=step.signal.kind, detail=step.signal.detail,
            )
            draft: SqlDraft = self._structured(SqlDraft, REPAIR_SYSTEM, prompt)
            if _normalise(draft.sql) == _normalise(step.model_sql):
                step.repairs.append(RepairEvent(
                    number, step.signal.kind, step.signal.detail, step.result, None,
                    accepted=False))
                continue
            new, new_signal = self._execute(draft.sql)
            step.attempts.append(SqlAttempt.from_result(new, draft.sql))
            resolved = new.ok and (new_signal is None
                                   or new_signal.kind != step.signal.kind)
            step.repairs.append(RepairEvent(
                number, step.signal.kind, step.signal.detail, step.result, new,
                accepted=resolved))
            if resolved:
                step.sql, step.result = new.sql or draft.sql, new
                step.model_sql, step.signal = draft.sql, new_signal
        return step

    def _history(self, steps: list[StepRun]) -> str:
        return "\n\n".join(
            f"Step {s.number}:\nSQL: {s.sql}\nResult:\n"
            f"{s.result.preview(limit=PREVIEW_ROWS)}"
            for s in steps
        )

    def run(self) -> ArchOutcome:
        try:
            draft: RouterDraft = self._structured(
                RouterDraft, ROUTER_SYSTEM,
                ROUTER_USER.format(context=self.context_text, question=self.question),
            )
            steps = [self.run_step(1, draft.sql, draft.reason)]
            if draft.needs_followup and steps[0].result.ok:
                while len(steps) < self.settings.max_steps:
                    nxt: FollowupDraft = self._structured(
                        FollowupDraft, FOLLOWUP_SYSTEM, FOLLOWUP_USER.format(
                            context=self.context_text, question=self.question,
                            reason=draft.reason or "(not given)",
                            history=self._history(steps),
                        ))
                    if nxt.done or not nxt.sql.strip():
                        break
                    steps.append(self.run_step(len(steps) + 1, nxt.sql, nxt.reason))
            selected, selected_step, plan_used = self._select(steps)
            if selected is None:
                return self._failed(steps, draft, "every query failed")
            final, _ = write_answer(
                self.llm, self.question, selected,
                max_tokens=self.settings.max_output_tokens,
                temperature=self.settings.temperature,
            )
        except StructuredOutputError as exc:
            return ArchOutcome(
                arch="router", question=self.question, status="failed",
                answer=f"Could not complete the analysis: {exc}", selected=None,
                path="escalated", stop_reason="error", error=str(exc),
            )

        escalated = draft.needs_followup or any(s.repairs for s in steps)
        step_records = [
            {"step": s.number,
             "tools": ["run_sql"] + (["repair"] if s.repairs else []),
             "sql": s.sql, "reason": s.reason,
             "signal": s.signal.kind if s.signal else None}
            for s in steps
        ]
        if plan_used:
            step_records.append({"step": "final", "tools": ["assemble"],
                                 "plan": plan_used})
        return ArchOutcome(
            arch="router", question=self.question, status="done",
            answer=final.answer, selected=selected,
            path="escalated" if escalated else "direct",
            plan=tuple([draft.reason] if draft.needs_followup else []),
            attempts=tuple(a for s in steps for a in s.attempts),
            steps=tuple(step_records),
            key_numbers=tuple(final.key_numbers), caveats=tuple(final.caveats),
            repairs=tuple(r for s in steps for r in s.repairs),
            selected_step=selected_step,
        )

    def _select(
        self, steps: list[StepRun]
    ) -> tuple[ExecutionResult | None, int | None, dict | None]:
        usable = {s.number: s.result for s in steps if s.result.ok and s.result.rows}
        if not usable:
            ran = [s for s in steps if s.result.ok]
            if not ran:
                return None, None, None
            return ran[-1].result, ran[-1].number, None
        if len(usable) == 1:
            (number, result), = usable.items()
            return result, number, None
        try:
            plan: AssemblyPlan = self._structured(
                AssemblyPlan, ASSEMBLY_SYSTEM, ASSEMBLY_USER.format(
                    question=self.question,
                    history=self._history([s for s in steps if s.number in usable]),
                ))
        except StructuredOutputError:
            plan = default_plan(usable)
        if plan.base_step not in usable:
            plan = default_plan(usable)
        return assemble(plan, usable), plan.base_step, plan.model_dump()

    def _failed(
        self, steps: list[StepRun], draft: RouterDraft, why: str
    ) -> ArchOutcome:
        last = steps[-1]
        message = f"I could not answer: {why} ({last.result.error_message})."
        return ArchOutcome(
            arch="router", question=self.question, status="failed", answer=message,
            selected=None,
            path="escalated" if (draft.needs_followup or last.repairs) else "direct",
            attempts=tuple(a for s in steps for a in s.attempts),
            repairs=tuple(r for s in steps for r in s.repairs),
            stop_reason="error", error=message,
        )


def run_router(
    question: str,
    *,
    llm: LLMClient,
    tools: ToolContext,
    context: SharedContext,
    settings: Settings,
) -> ArchOutcome:
    return Router(question, llm=llm, tools=tools, context=context,
                  settings=settings).run()
