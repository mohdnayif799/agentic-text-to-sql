"""The shared final-answer step (architectures A, C and D).

The answer is written from the selected result only, so every number in it
can be traced to a row the evaluation also grades. One LLM call.
"""

from __future__ import annotations

from agentcrew.llm import LLMClient, Usage
from agentcrew.schemas import ExecutionResult, FinalAnswer

ANSWER_ROWS = 40

ANSWER_SYSTEM = """\
You write the final answer of an analytics assistant. You are given the \
user's question, the SQL that was run and its result. Answer using ONLY that \
result.

Rules:
- Answer the question directly in one to four sentences.
- Every number you state must appear in the result. Copy figures from it \
(rounding for readability is fine); never estimate or use outside knowledge.
- Name the specific items (with their IDs when the result has them) together \
with the figures that answer the question.
- If the question could reasonably be read more than one way, say which \
reading the result reflects, starting with "Assuming".
- If the result has no rows, say that no matching data was found.
"""

ANSWER_USER = """\
Question: {question}

SQL that was run:
{sql}

Result ({row_count} row(s){truncated}):
{preview}
"""


def write_answer(
    llm: LLMClient,
    question: str,
    result: ExecutionResult,
    *,
    max_tokens: int,
    temperature: float,
) -> tuple[FinalAnswer, Usage]:
    truncated = (
        "; only the first rows are shown" if result.row_count > ANSWER_ROWS else ""
    )
    return llm.complete_structured(
        system=ANSWER_SYSTEM,
        user=ANSWER_USER.format(
            question=question,
            sql=result.sql,
            row_count=result.row_count,
            truncated=truncated,
            preview=result.preview(limit=ANSWER_ROWS),
        ),
        schema=FinalAnswer,
        max_tokens=max_tokens,
        temperature=temperature,
    )
