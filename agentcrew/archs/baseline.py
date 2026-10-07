"""Architecture A: the single-shot baseline on the shared context.

One SQL query from the shared context, one retry only if that query errors
(including a safety-guard rejection), then the shared answer step. No repair
of a query that ran, no verification, no follow-up queries. This keeps the
legacy baseline's SQL behaviour, so its graded result stays comparable with
the old numbers (eval/PROTOCOL.md, protocol v2).
"""

from __future__ import annotations

from agentcrew.archs import progress
from agentcrew.archs.answer import write_answer
from agentcrew.archs.outcome import ArchOutcome, RepairEvent, SqlAttempt
from agentcrew.config import Settings
from agentcrew.context import SharedContext
from agentcrew.llm import LLMClient, StructuredOutputError
from agentcrew.schemas import ExecutionResult, SqlDraft
from agentcrew.tools import ToolContext, execute_readonly_sql

SQL_SYSTEM = """\
You are a SQL analyst with read-only access to a SQLite database. Write ONE \
SQLite query that answers the user's question.

Rules:
- SQLite dialect; exactly one read-only SELECT (or WITH ... SELECT) statement.
- Use only the tables and columns in the schema, and text values exactly as \
listed under allowed values.
- Follow the metric definitions in the data dictionary.
- Return the rows that answer the question. For a "which" or "top N" \
question, return the N item(s) together with the figure that ranks them, and \
include IDs and names when the question asks for them.
- Give computed columns clear aliases.
"""

SQL_USER = """\
{context}

## Question
{question}
{retry_block}"""

RETRY_BLOCK = """
## Your previous query failed
SQL: {sql}
Error: {error}
Write a corrected query.
"""


def _draft(llm: LLMClient, user: str, settings: Settings) -> SqlDraft:
    draft, _ = llm.complete_structured(
        system=SQL_SYSTEM,
        user=user,
        schema=SqlDraft,
        max_tokens=settings.max_output_tokens,
        temperature=settings.temperature,
    )
    return draft


def run_baseline(
    question: str,
    *,
    llm: LLMClient,
    tools: ToolContext,
    context: SharedContext,
    settings: Settings,
) -> ArchOutcome:
    def failed(message: str, attempts: list[SqlAttempt]) -> ArchOutcome:
        return ArchOutcome(
            arch="baseline", question=question, status="failed", answer=message,
            selected=None, path="direct", attempts=tuple(attempts),
            stop_reason="error", error=message, repairs=tuple(repairs),
        )

    user = SQL_USER.format(context=context.render(), question=question,
                           retry_block="")
    attempts: list[SqlAttempt] = []
    repairs: list[RepairEvent] = []
    try:
        progress.enter("step 1: SQL")
        draft = _draft(llm, user, settings)
        result: ExecutionResult = execute_readonly_sql(tools, draft.sql)
        attempts.append(SqlAttempt.from_result(result, draft.sql))
        if not result.ok:
            first = result
            progress.enter("step 1: retry")
            retry = RETRY_BLOCK.format(sql=result.sql or draft.sql,
                                       error=result.error_message)
            draft = _draft(llm, user + retry, settings)
            result = execute_readonly_sql(tools, draft.sql)
            attempts.append(SqlAttempt.from_result(result, draft.sql))
            repairs.append(RepairEvent(
                step=1, signal="error", detail=first.error_message or "",
                before=first, after=result, accepted=result.ok,
            ))
    except StructuredOutputError as exc:
        return failed(f"Could not produce a query: {exc}", attempts)

    if not result.ok:
        return failed(
            f"I could not answer: the query failed ({result.error_message}).",
            attempts,
        )
    try:
        progress.enter("final: answer")
        final, _ = write_answer(
            llm, question, result,
            max_tokens=settings.max_output_tokens, temperature=settings.temperature,
        )
    except StructuredOutputError as exc:
        return failed(f"Could not write the answer: {exc}", attempts)

    return ArchOutcome(
        arch="baseline", question=question, status="done", answer=final.answer,
        selected=result, path="direct", attempts=tuple(attempts),
        steps=({"step": 1, "tools": ["run_sql"], "sql": result.sql},),
        key_numbers=tuple(final.key_numbers), caveats=tuple(final.caveats),
        repairs=tuple(repairs), selected_step=1,
    )
