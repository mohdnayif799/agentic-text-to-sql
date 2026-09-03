"""Single-agent baseline.

The point of this file is intellectual honesty. The claim "multiple
coordinated stages beat one prompt" is testable, and if it is not true for a
given question class, the evaluation should say so.

The baseline is deliberately *strong*, not a straw man:

* same model, same temperature;
* the full schema card for the same selected tables (schema selection is
  deterministic infrastructure, not part of the orchestration claim, so both
  arms get it);
* the same read-only guard and row cap;
* one retry if its query fails, so it is not penalised for a typo.

What it does not get is the thing under test: multi-step planning, a
deterministic triage stage, and semantic verification of the result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agentcrew.config import Settings, get_settings
from agentcrew.db.selector import render_schema_card, select_tables
from agentcrew.graph.state import Deps
from agentcrew.schemas import ExecutionResult, SqlDraft
from agentcrew.tools import execute_readonly_sql

BASELINE_SYSTEM = """\
You are a SQL analyst with read-only access to a SQLite database. Given a \
question and a schema, write ONE SQLite SELECT query that answers it.

- SQLite dialect, one statement, read-only.
- Use only the tables and columns shown.
- Dates are ISO TEXT ('YYYY-MM-DD').
"""

BASELINE_USER = """\
Question: {question}

Schema:
{schema_card}
{retry_block}
"""


@dataclass
class BaselineOutcome:
    question: str
    sql: str
    result: ExecutionResult
    attempts: int
    duration_s: float
    llm_calls: int
    input_tokens: int
    output_tokens: int
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result.ok and bool(self.result.rows)


def run_baseline(
    question: str,
    deps: Deps,
    settings: Settings | None = None,
    *,
    max_attempts: int = 2,
) -> BaselineOutcome:
    """One-shot text-to-SQL with a single retry on execution failure."""
    settings = settings or get_settings()
    started = time.perf_counter()

    selection = select_tables(
        question, deps.catalog, max_tables=deps.max_tables_in_context
    )
    schema_card = render_schema_card(
        deps.catalog,
        selection.tables,
        engine=deps.engine,
        sample_limit=deps.sample_values_per_column,
        max_distinct=deps.max_distinct_for_sampling,
    )

    calls = in_tok = out_tok = 0
    retry_block = ""
    result = ExecutionResult(ok=False, sql="", error_message="not attempted")
    sql = ""
    error: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            draft, usage = deps.llm.complete_structured(
                system=BASELINE_SYSTEM,
                user=BASELINE_USER.format(
                    question=question, schema_card=schema_card, retry_block=retry_block
                ),
                schema=SqlDraft,
                max_tokens=deps.max_output_tokens,
                temperature=deps.temperature,
            )
        except Exception as exc:
            error = str(exc)
            break

        calls += usage.calls
        in_tok += usage.input_tokens
        out_tok += usage.output_tokens
        sql = draft.sql

        result = execute_readonly_sql(deps.tools, sql)
        if result.ok:
            break
        retry_block = (
            f"\nYour previous query failed.\nSQL: {sql}\n"
            f"Error: {result.error_message}\nWrite a corrected query."
        )
        if attempt == max_attempts:
            break

    return BaselineOutcome(
        question=question,
        sql=result.sql or sql,
        result=result,
        attempts=min(calls, max_attempts) or 1,
        duration_s=round(time.perf_counter() - started, 3),
        llm_calls=calls,
        input_tokens=in_tok,
        output_tokens=out_tok,
        error=error,
    )
