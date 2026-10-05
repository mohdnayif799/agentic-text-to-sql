"""Architecture C: router.

Step 0 IS architecture A's first call: the same prompt, schema and settings
(through `step0_llm`, which the eval runner points at A's response cache,
so it is byte-identical to A). The run then escalates only on a signal:

1. Deterministic signals on the executed query (checks.classify: SQL error,
   schema check, join fan-out, empty result, all-NULL or row-cap sanity)
   -> disciplined repair of that query. No LLM call is needed to decide.
2. If step 0 produced a usable result, one small structured call decides
   {needs_followup, reason} from the question, the SQL and a result preview.
   needs_followup -> follow-up queries, each seeing the original question
   verbatim, the reason and capped previews of earlier results.

With no signal and no follow-up, C answers directly with A's answer call on
A's result, so the direct path is exactly A. A question that asks for a
forecast or a fitted trend (checks.forecast_signal, zero LLM calls) skips the
decision and escalates to follow-ups directly.

Disciplined repair: schema and fan-out checks run before executing (no LLM
call); a repair gets the exact signal text; the original query and result
are kept, and a fix is accepted only if it removes that signal and the query
runs. Budgets: at most `max_steps` queries and `max_attempts_per_step`
executions per step.

Never worse than A when escalation fails:
- an SQL error at step 1 is first retried with A's own retry call (its
  prompt and, in evaluation, its cache), so C and D recover wherever A does;
- an invalid model reply in a repair, decision or follow-up call ends that
  part of the work instead of the run (the results so far are kept);
- when nothing escalated is usable, the final table is step 1's;
- when the final table is A's own result, the answer is A's answer call;
- if the answer call on an escalated table fails, the run answers from A's
  result with A's answer call instead.

When the answer draws on more than one step, one final table holding every
value it uses is assembled (archs/assemble.py); that table is graded and
shown, and the answer is written from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from agentcrew.archs import progress
from agentcrew.archs.answer import write_answer
from agentcrew.archs.assemble import AssemblyPlan, assemble, default_plan
from agentcrew.archs.baseline import RETRY_BLOCK, SQL_SYSTEM, SQL_USER
from agentcrew.archs.checks import (
    Schema,
    Signal,
    classify,
    forecast_signal,
    schema_error,
    schema_of,
)
from agentcrew.archs.fanout import KeyIndex, fanout_problem
from agentcrew.archs.outcome import ArchOutcome, RepairEvent, SqlAttempt
from agentcrew.config import Settings
from agentcrew.context import SharedContext
from agentcrew.llm import LLMClient, StructuredOutputError
from agentcrew.schemas import ExecutionResult, SqlDraft
from agentcrew.tools import ToolContext, execute_readonly_sql

PREVIEW_ROWS = 8

#: (executed sql, result) -> a verification Signal or None (architecture D).
Verifier = Callable[[str, ExecutionResult], "Signal | None"]

DECISION_SYSTEM = """\
You check whether one query result fully answers a user's question about a \
database. Set needs_followup to true only if it does not, because:
- the question has several steps and a further query depends on this result;
- the question asks why, or for a drill-down, and this result does not yet \
explain the cause;
- the answer needs a statistic or a comparison that is not in this result;
- the question can be read in more than one way and this result does not \
show which reading it uses.
Otherwise set needs_followup to false. Give the reason in one sentence.
"""

DECISION_USER = """\
## Question
{question}

## Query that was run
{sql}

## Result ({row_count} row(s))
{preview}
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


class FollowupDecision(BaseModel):
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
    kind: str = "sql"
    """sql | compute | lookup. Only sql and compute results can be selected."""
    tool: str = "run_sql"
    from_a: bool = False
    """The result came from A's own calls (step 1, or A's retry of it)."""


def _normalise(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").lower()


_KEY_INDEXES: dict[str, KeyIndex] = {}


def _key_index(tools: ToolContext) -> KeyIndex:
    """One uniqueness cache per database, shared across questions."""
    url = str(tools.engine.url)
    if url not in _KEY_INDEXES:
        _KEY_INDEXES[url] = KeyIndex(tools.engine, tools.catalog)
    return _KEY_INDEXES[url]


class Router:
    def __init__(
        self,
        question: str,
        *,
        llm: LLMClient,
        tools: ToolContext,
        context: SharedContext,
        settings: Settings,
        step0_llm: LLMClient | None = None,
    ) -> None:
        self.question = question
        self.llm = llm
        self.step0_llm = step0_llm or llm
        self.tools = tools
        self.context_text = context.render()
        self.settings = settings
        self.schema: Schema = schema_of(tools.catalog)
        self.keys = _key_index(tools)
        # Read from the question before any call; C and D share it, so they
        # stay identical up to escalation.
        self.question_signal = forecast_signal(question)

    def _followup_decision(self, first: StepRun,
                           llm: LLMClient | None = None) -> FollowupDecision:
        """The question signal escalates without a call; otherwise the
        decision call runs when step 0 gave a usable result."""
        if self.question_signal is not None:
            return FollowupDecision(needs_followup=True,
                                    reason=self.question_signal.detail)
        if first.result.ok and first.result.rows:
            try:
                return self._decide(first, llm=llm)
            except StructuredOutputError:
                return FollowupDecision()  # no valid decision: answer as A does
        return FollowupDecision()

    @staticmethod
    def escalated(decision: FollowupDecision, first: StepRun) -> bool:
        """The one escalation rule, shared by C and D: a follow-up is needed
        (decision or question signal), or step 1 had a signal (and so was
        repaired). Everything it reads comes before any D-only work."""
        return (decision.needs_followup or bool(first.repairs)
                or first.signal is not None)

    def _signal_record(self, number: int) -> dict:
        return ({"escalation_signal": self.question_signal.kind}
                if self.question_signal is not None and number == 1 else {})

    def _structured(self, schema: type, system: str, user: str,
                    llm: LLMClient | None = None):
        obj, _ = (llm or self.llm).complete_structured(
            system=system, user=user, schema=schema,
            max_tokens=self.settings.max_output_tokens,
            temperature=self.settings.temperature,
        )
        return obj

    def _execute(self, sql: str) -> tuple[ExecutionResult, Signal | None]:
        problem = schema_error(sql, self.schema)    # before executing, no LLM
        fanout = fanout_problem(sql, self.keys)     # before executing, no LLM
        result = execute_readonly_sql(self.tools, sql)
        return result, classify(result, problem, self.tools.max_rows, fanout)

    def _check(self, sql: str, verify: Verifier | None
               ) -> tuple[ExecutionResult, Signal | None]:
        result, signal = self._execute(sql)
        if signal is None and verify is not None:
            signal = verify(result.sql or sql, result)
        return result, signal

    def run_step(
        self,
        number: int,
        sql: str,
        reason: str = "",
        *,
        repair_llm: LLMClient | None = None,
        verify: Verifier | None = None,
        from_a: bool = False,
    ) -> StepRun:
        """Execute, then repair on a signal. `verify` (architecture D's
        escalated path) adds verification checks to the signals. `from_a`
        marks step 1, whose query is A's."""
        progress.enter(f"step {number}: query")
        result, signal = self._check(sql, verify)
        step = StepRun(number, result.sql or sql, result, signal, reason, sql,
                       from_a=from_a)
        step.attempts.append(SqlAttempt.from_result(result, sql))
        return self.repair(step, llm=repair_llm, verify=verify)

    def repair(
        self,
        step: StepRun,
        *,
        llm: LLMClient | None = None,
        verify: Verifier | None = None,
    ) -> StepRun:
        """Disciplined repair of `step` while it has a signal and attempts remain."""
        tries = len(step.attempts)
        while step.signal and tries < self.settings.max_attempts_per_step:
            tries += 1
            # A's own retry first for an SQL error in A's query: C and D then
            # recover wherever A does.
            a_retry = (step.from_a and step.signal.kind == "error"
                       and not step.repairs)
            via = "A's retry" if a_retry else "repair"
            progress.enter(f"step {step.number}: {via} ({step.signal.kind})")
            try:
                if a_retry:
                    draft: SqlDraft = self._structured(
                        SqlDraft, SQL_SYSTEM,
                        SQL_USER.format(context=self.context_text,
                                        question=self.question, retry_block="")
                        + RETRY_BLOCK.format(sql=step.sql,
                                             error=step.result.error_message),
                        llm=self.step0_llm)
                else:
                    prompt = REPAIR_USER.format(
                        context=self.context_text, question=self.question,
                        sql=step.model_sql, kind=step.signal.kind,
                        detail=step.signal.detail,
                    )
                    draft = self._structured(SqlDraft, REPAIR_SYSTEM, prompt, llm=llm)
            except StructuredOutputError:
                # No usable repair reply: keep the result as it is.
                step.repairs.append(RepairEvent(
                    step.number, step.signal.kind, step.signal.detail, step.result,
                    None, accepted=False, via=via))
                break
            if _normalise(draft.sql) == _normalise(step.model_sql):
                step.repairs.append(RepairEvent(
                    step.number, step.signal.kind, step.signal.detail, step.result,
                    None, accepted=False, via=via))
                continue
            new, new_signal = self._check(draft.sql, verify)
            step.attempts.append(SqlAttempt.from_result(new, draft.sql))
            resolved = new.ok and (new_signal is None
                                   or new_signal.kind != step.signal.kind)
            step.repairs.append(RepairEvent(
                step.number, step.signal.kind, step.signal.detail, step.result, new,
                accepted=resolved, via=via))
            if resolved:
                step.sql, step.result = new.sql or draft.sql, new
                step.model_sql, step.signal = draft.sql, new_signal
                step.from_a = step.from_a and a_retry
        return step

    def _answer_llm(self, selected: ExecutionResult, steps: list[StepRun],
                    escalated: bool) -> LLMClient:
        """A's answer call when the run is direct or its final table is A's
        own result (so it answers exactly as A does); otherwise the
        architecture's own call."""
        first = steps[0] if steps else None
        if not escalated or (first is not None and first.from_a
                             and selected is first.result):
            return self.step0_llm
        return self.llm

    def _answer(self, selected: ExecutionResult, selected_step: int | None,
                steps: list[StepRun], escalated: bool, guidance: str = ""):
        """(answer, selected, selected_step, fell_back). If the answer call on
        an escalated table fails, answer from step 1's result instead."""
        progress.enter("final: answer")
        try:
            final, _ = write_answer(
                self._answer_llm(selected, steps, escalated), self.question,
                selected, max_tokens=self.settings.max_output_tokens,
                temperature=self.settings.temperature, guidance=guidance)
            return final, selected, selected_step, False
        except StructuredOutputError:
            first = steps[0]
            if selected is first.result or not first.result.ok:
                raise
        progress.enter("final: answer (fallback to step 1)")
        final, _ = write_answer(
            self._answer_llm(first.result, steps, escalated), self.question,
            first.result, max_tokens=self.settings.max_output_tokens,
            temperature=self.settings.temperature)
        return final, first.result, 1, True

    def _history(self, steps: list[StepRun]) -> str:
        return "\n\n".join(
            f"Step {s.number}:\nSQL: {s.sql}\nResult:\n"
            f"{s.result.preview(limit=PREVIEW_ROWS)}"
            for s in steps
        )

    def _decide(self, step: StepRun, llm: LLMClient | None = None) -> FollowupDecision:
        """Follow-up decision: only reached when step 0 gave a usable result."""
        progress.enter("follow-up decision")
        prompt = DECISION_USER.format(
            question=self.question, sql=step.sql,
            row_count=step.result.row_count,
            preview=step.result.preview(limit=PREVIEW_ROWS),
        )
        return self._structured(FollowupDecision, DECISION_SYSTEM, prompt, llm=llm)

    def run(self) -> ArchOutcome:
        decision = FollowupDecision()
        steps: list[StepRun] = []
        try:
            # Step 0 = A's first call, byte for byte.
            progress.enter("step 1: SQL (A's call)")
            draft: SqlDraft = self._structured(
                SqlDraft, SQL_SYSTEM,
                SQL_USER.format(context=self.context_text, question=self.question,
                                retry_block=""),
                llm=self.step0_llm,
            )
            steps.append(self.run_step(1, draft.sql, from_a=True))
            first = steps[0]
            decision = self._followup_decision(first)
            if decision.needs_followup:
                while len(steps) < self.settings.max_steps:
                    progress.enter(f"step {len(steps) + 1}: follow-up SQL")
                    try:
                        nxt: FollowupDraft = self._structured(
                            FollowupDraft, FOLLOWUP_SYSTEM, FOLLOWUP_USER.format(
                                context=self.context_text, question=self.question,
                                reason=decision.reason or "(not given)",
                                history=self._history(steps),
                            ))
                    except StructuredOutputError:
                        break  # no valid follow-up: answer from what ran
                    if nxt.done or not nxt.sql.strip():
                        break
                    steps.append(self.run_step(len(steps) + 1, nxt.sql, nxt.reason))
            selected, selected_step, plan_used = self._select(steps)
            if selected is None:
                return self._failed(steps, decision, "every query failed")
            direct = not self.escalated(decision, first)
            final, selected, selected_step, fell_back = self._answer(
                selected, selected_step, steps, not direct)
            if fell_back:
                plan_used = {"fallback": "the answer call failed on the final "
                                         "table; answered from step 1"}
        except StructuredOutputError as exc:
            return ArchOutcome(
                arch="router", question=self.question, status="failed",
                answer=f"Could not complete the analysis: {exc}", selected=None,
                path="escalated" if steps else "direct", stop_reason="error",
                error=str(exc),
                attempts=tuple(a for s in steps for a in s.attempts),
                repairs=tuple(r for s in steps for r in s.repairs),
            )

        step_records = [
            {"step": s.number,
             "tools": ["run_sql"] + (["repair"] if s.repairs else []),
             "sql": s.sql, "reason": s.reason,
             "signal": s.repairs[0].signal if s.repairs else None,
             **self._signal_record(s.number)}
            for s in steps
        ]
        if plan_used:
            step_records.append({"step": "final",
                                 "tools": ["fallback" if "fallback" in plan_used
                                           else "assemble"],
                                 "plan": plan_used})
        return ArchOutcome(
            arch="router", question=self.question, status="done",
            answer=final.answer, selected=selected,
            path="direct" if direct else "escalated",
            plan=tuple([decision.reason] if decision.needs_followup else []),
            attempts=tuple(a for s in steps for a in s.attempts),
            steps=tuple(step_records),
            key_numbers=tuple(final.key_numbers), caveats=tuple(final.caveats),
            repairs=tuple(r for s in steps for r in s.repairs),
            selected_step=selected_step,
        )

    def _select(
        self, steps: list[StepRun]
    ) -> tuple[ExecutionResult | None, int | None, dict | None]:
        selectable = [s for s in steps if s.kind in ("sql", "compute")]
        usable = {s.number: s.result for s in selectable
                  if s.result.ok and s.result.rows}
        if not usable:
            ran = [s for s in selectable if s.result.ok]
            if not ran:
                return None, None, None
            # Nothing usable: step 1's (A's) result, as A would answer.
            pick = ran[0] if ran[0].number == 1 else ran[-1]
            return pick.result, pick.number, None
        if len(usable) == 1:
            (number, result), = usable.items()
            return result, number, None
        try:
            progress.enter("final: assembly")
            plan: AssemblyPlan = self._structured(
                AssemblyPlan, ASSEMBLY_SYSTEM, ASSEMBLY_USER.format(
                    question=self.question,
                    history=self._history([s for s in selectable
                                           if s.number in usable]),
                ))
        except StructuredOutputError:
            plan = default_plan(usable)
        if plan.base_step not in usable:
            plan = default_plan(usable)
        return assemble(plan, usable), plan.base_step, plan.model_dump()

    def _failed(
        self, steps: list[StepRun], decision: FollowupDecision, why: str
    ) -> ArchOutcome:
        last = steps[-1]
        message = f"I could not answer: {why} ({last.result.error_message})."
        escalated = decision.needs_followup or any(s.repairs for s in steps)
        return ArchOutcome(
            arch="router", question=self.question, status="failed", answer=message,
            selected=None, path="escalated" if escalated else "direct",
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
    step0_llm: LLMClient | None = None,
) -> ArchOutcome:
    return Router(question, llm=llm, tools=tools, context=context,
                  settings=settings, step0_llm=step0_llm).run()
