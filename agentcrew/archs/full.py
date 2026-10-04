"""Architecture D: full (C plus exploration, compute tools and verification).

Up to the follow-up decision D is exactly C:
- step 0 is A's first call (`step0_llm`, A's response cache in evaluation);
- C's deterministic signals and disciplined repair (`shared_llm`, C's cache);
- C's follow-up decision (`shared_llm`).
With no signal and no follow-up, D answers directly with A's answer call, so
the direct path is exactly A.

When the run escalates, D adds:
- verification checks (archs/verify.py) on every query of the escalated
  path, step 0 included; a failed check triggers a disciplined repair;
- an exploration loop: decide_next_action returns one validated NextAction
  (archs/explore.py) given the full shared context, the question verbatim,
  why more work is needed and capped previews of earlier steps. Tools:
  get_distinct_values, search_values, sample_rows, run_sql, compute, answer.

Budgets are the live demo's: at most `max_steps` steps (step 0 counts as
one; every tool call is one step) and `max_attempts_per_step` executions per
query. The final result is assembled as in C and the answer is written from
it.
"""

from __future__ import annotations

from agentcrew.archs.answer import write_answer
from agentcrew.archs.baseline import SQL_SYSTEM, SQL_USER
from agentcrew.archs.explore import (
    NextAction,
    compute,
    lookup,
)
from agentcrew.archs.outcome import ArchOutcome, SqlAttempt
from agentcrew.archs.router import (
    PREVIEW_ROWS,
    FollowupDecision,
    Router,
    StepRun,
)
from agentcrew.archs.verify import verify
from agentcrew.compute.metrics import catalog
from agentcrew.config import Settings
from agentcrew.context import SharedContext
from agentcrew.context.values import profile_columns
from agentcrew.llm import LLMClient, StructuredOutputError
from agentcrew.schemas import ExecutionResult, SqlDraft
from agentcrew.tools import ToolContext, execute_readonly_sql

DECIDE_SYSTEM = """\
You plan the next step of an analysis of a read-only SQLite database. Choose \
exactly ONE action and return it as JSON.

Actions:
- get_distinct_values: table, column. Lists a column's real values.
- search_values: term (optional table, column). Fuzzy-matches a word from \
the question to real values stored in the database.
- sample_rows: table. Shows a few rows.
- run_sql: sql. One read-only SQLite SELECT (or WITH ... SELECT); only the \
tables and columns in the schema, text values exactly as listed, metric \
definitions from the data dictionary.
- compute: function, params. Deterministic calculations, no code:
  * on an earlier step's table (params.step = that step's number):
    pct_change(value, order?), yoy_growth(period, value),
    compare_periods(period, value, period_a, period_b, by?),
    share_of_total(value, label),
    zscore_anomalies(value, label, threshold?, direction?, against?) where \
direction is both|above|below and against is mean|trend,
    linear_trend_forecast(value, order, periods_ahead?),
    correlation(a, b);
  * metric analyses whose SQL is built from the metric definitions:
    decompose_change(metric, comparison) splits a metric's difference \
between two periods or two groups into its components (for example orders x \
average order value, or units x average price x (1 - average discount)) and \
reports each component's change;
    contribution_by_dimension(metric, dimension, comparison) shows how much \
each value of a dimension contributes to the change and names the largest.
    comparison is {"period_a": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, \
"period_b": {...}} or {"group_dimension": ..., "group_a": ..., "group_b": \
..., "period": {...}}, with optional "filters": {dimension: value}.
  Metrics and their dimensions:
{catalog}
- answer: stop, because the steps so far contain everything needed.

Method: for a "why" or difference question, split the metric into its \
components and check which values of the relevant dimension contribute most \
before answering. Every tool call uses one step; stop with answer when the \
results suffice.
"""

DECIDE_USER = """\
{context}

## Question
{question}

## Why more work is needed
{reason}

## Steps so far
{history}

## Steps left
{remaining}
"""


_FORMATS: dict[str, dict[tuple[str, str], str]] = {}


def _date_formats(tools: ToolContext) -> dict[tuple[str, str], str]:
    url = str(tools.engine.url)
    if url not in _FORMATS:
        profile = profile_columns(tools.engine, tools.catalog)
        _FORMATS[url] = {(d.table, d.column): d.fmt for d in profile.dates}
    return _FORMATS[url]


class Full(Router):
    def __init__(
        self,
        question: str,
        *,
        llm: LLMClient,
        tools: ToolContext,
        context: SharedContext,
        settings: Settings,
        step0_llm: LLMClient | None = None,
        shared_llm: LLMClient | None = None,
    ) -> None:
        super().__init__(question, llm=llm, tools=tools, context=context,
                         settings=settings, step0_llm=step0_llm)
        self.shared_llm = shared_llm or llm
        self.formats = _date_formats(tools)
        # replace(), not format(): the prompt contains literal JSON braces.
        self.system = DECIDE_SYSTEM.replace("{catalog}", catalog())

    def _verify(self, sql: str, result: ExecutionResult):
        return verify(sql, result, self.question, self.schema, self.formats)

    def _explore_history(self, steps: list[StepRun]) -> str:
        return "\n\n".join(
            f"Step {s.number} [{s.tool}]"
            + (f" - {s.reason}" if s.reason else "")
            + f"\n{s.sql}\nResult:\n{s.result.preview(limit=PREVIEW_ROWS)}"
            for s in steps
        )

    def _next_action(self, steps: list[StepRun], reason: str) -> NextAction | None:
        prompt = DECIDE_USER.format(
            context=self.context_text, question=self.question,
            reason=reason or "(not given)", history=self._explore_history(steps),
            remaining=self.settings.max_steps - len(steps),
        )
        try:
            return self._structured(NextAction, self.system, prompt)
        except StructuredOutputError:
            return None

    def _act(self, number: int, action: NextAction,
             steps: list[StepRun]) -> StepRun:
        if action.action == "run_sql":
            step = self.run_step(number, action.sql, action.reason,
                                 verify=self._verify)
            return step
        if action.action == "compute":
            result, _ = compute(
                action, {s.number: s.result for s in steps},
                lambda sql: execute_readonly_sql(self.tools, sql),
            )
            step = StepRun(number, result.sql, result, None, action.reason,
                           result.sql, kind="compute",
                           tool=f"compute:{action.function}")
        else:
            result = lookup(action, self.tools)
            step = StepRun(number, result.sql, result, None, action.reason,
                           result.sql, kind="lookup", tool=action.action)
        step.attempts.append(SqlAttempt.from_result(result, result.sql))
        return step

    def run(self) -> ArchOutcome:
        decision = FollowupDecision()
        steps: list[StepRun] = []
        try:
            # Step 0 = A's first call; repairs and decision = C's own calls.
            draft: SqlDraft = self._structured(
                SqlDraft, SQL_SYSTEM,
                SQL_USER.format(context=self.context_text, question=self.question,
                                retry_block=""),
                llm=self.step0_llm,
            )
            first = self.run_step(1, draft.sql, repair_llm=self.shared_llm)
            steps.append(first)
            if first.result.ok and first.result.rows:
                decision = self._decide(first, llm=self.shared_llm)
            explore = decision.needs_followup or first.signal is not None
            escalated = explore or bool(first.repairs)
            if escalated and first.result.ok and first.signal is None:
                first.signal = self._verify(first.sql, first.result)
                self.repair(first, verify=self._verify)
                # A failed check that repair could not resolve: explore.
                explore = explore or first.signal is not None
            if explore:
                reason = decision.reason or (
                    first.signal.detail if first.signal else "")
                while len(steps) < self.settings.max_steps:
                    action = self._next_action(steps, reason)
                    if action is None or action.action == "answer":
                        break
                    steps.append(self._act(len(steps) + 1, action, steps))
            selected, selected_step, plan_used = self._select(steps)
            if selected is None:
                failed = self._failed(steps, decision, "every query failed")
                return self._as_full(failed)
            final, _ = write_answer(
                self.llm if escalated else self.step0_llm, self.question, selected,
                max_tokens=self.settings.max_output_tokens,
                temperature=self.settings.temperature,
            )
        except StructuredOutputError as exc:
            return ArchOutcome(
                arch="full", question=self.question, status="failed",
                answer=f"Could not complete the analysis: {exc}", selected=None,
                path="escalated" if steps else "direct", stop_reason="error",
                error=str(exc),
                attempts=tuple(a for s in steps for a in s.attempts),
                repairs=tuple(r for s in steps for r in s.repairs),
            )

        records = [
            {"step": s.number,
             "tools": [s.tool] + (["repair"] if s.repairs else []),
             "sql": s.sql, "reason": s.reason,
             "signal": s.repairs[0].signal if s.repairs else None}
            for s in steps
        ]
        if plan_used:
            records.append({"step": "final", "tools": ["assemble"], "plan": plan_used})
        return ArchOutcome(
            arch="full", question=self.question, status="done",
            answer=final.answer, selected=selected,
            path="escalated" if escalated else "direct",
            plan=tuple([decision.reason] if decision.needs_followup else []),
            attempts=tuple(a for s in steps for a in s.attempts),
            steps=tuple(records),
            key_numbers=tuple(final.key_numbers), caveats=tuple(final.caveats),
            repairs=tuple(r for s in steps for r in s.repairs),
            selected_step=selected_step,
        )

    @staticmethod
    def _as_full(outcome: ArchOutcome) -> ArchOutcome:
        from dataclasses import replace

        return replace(outcome, arch="full")


def run_full(
    question: str,
    *,
    llm: LLMClient,
    tools: ToolContext,
    context: SharedContext,
    settings: Settings,
    step0_llm: LLMClient | None = None,
    shared_llm: LLMClient | None = None,
) -> ArchOutcome:
    return Full(question, llm=llm, tools=tools, context=context, settings=settings,
                step0_llm=step0_llm, shared_llm=shared_llm).run()
