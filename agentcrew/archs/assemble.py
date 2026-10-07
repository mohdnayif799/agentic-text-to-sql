"""Assemble one final result table from several steps (architecture C).

When an answer draws on more than one step's result, the graded and shown
result must be a single table that contains every value the answer uses, and
the prose is written from it. The model only CHOOSES (which step's rows form
the table, which values from other steps to add); the table itself is built
here deterministically, so every value is copied from an executed result.

A value from another step is added as a column repeated on every row. A step
with several rows contributes one column per row, named after that row's
label (its first text cell). The base step's SQL is kept as provenance, so a
ranked base result still satisfies the top-N rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from agentcrew.schemas import ExecutionResult

MAX_PIVOT_ROWS = 12


class AssemblyColumn(BaseModel):
    step: int
    column: str


class AssemblyPlan(BaseModel):
    base_step: int = Field(description="Step whose rows answer the question.")
    columns: list[AssemblyColumn] = Field(
        default_factory=list,
        description="Values from OTHER steps that the answer also uses.",
    )


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate, n = f"{name}_{n}", n + 1
    taken.add(candidate.lower())
    return candidate


def _label(row: Sequence[object], skip: int) -> str:
    for i, cell in enumerate(row):
        if i != skip and isinstance(cell, str):
            return cell
    return "row"


def default_plan(results: Mapping[int, ExecutionResult]) -> AssemblyPlan:
    """Last step as the base, plus every column of earlier one-row steps."""
    base = max(results)
    return AssemblyPlan(
        base_step=base,
        columns=[
            AssemblyColumn(step=s, column=c)
            for s, r in results.items() if s != base and r.row_count == 1
            for c in r.columns
        ],
    )


def assemble(
    plan: AssemblyPlan, results: Mapping[int, ExecutionResult]
) -> ExecutionResult:
    if plan.base_step not in results:
        plan = default_plan(results)
    base = results[plan.base_step]
    columns = list(base.columns)
    taken = {c.lower() for c in columns}
    rows = [list(r) for r in base.rows]
    used_steps = []
    for extra in plan.columns:
        source = results.get(extra.step)
        if extra.step == plan.base_step or source is None:
            continue
        if extra.column not in source.columns:
            continue
        j = source.columns.index(extra.column)
        if source.row_count == 1:
            pairs = [(extra.column, source.rows[0][j])]
        else:
            pairs = [
                (f"{_label(row, j)}_{extra.column}", row[j])
                for row in source.rows[:MAX_PIVOT_ROWS]
            ]
        for name, value in pairs:
            columns.append(_unique(name.replace(" ", "_"), taken))
            for row in rows:
                row.append(value)
        used_steps.append(extra.step)
    steps = sorted(set(used_steps))
    note = (f"-- assembled: rows from step {plan.base_step}"
            + (f", values from step(s) {', '.join(map(str, steps))}" if steps else ""))
    return ExecutionResult(
        ok=True, sql=f"{note}\n{base.sql}", columns=columns, rows=rows,
        row_count=len(rows), truncated=base.truncated,
    )


def stack(results: Sequence[ExecutionResult], steps: Sequence[int]) -> ExecutionResult:
    """One table from results with identical columns: their rows, in step order.

    Used for D's metric analyses (decompose_change, contribution_by_dimension),
    which share one schema, so a why-question's answer sees every analysis.
    """
    columns = list(results[0].columns)
    if any(list(r.columns) != columns for r in results):
        raise ValueError("only results with the same columns can be stacked")
    rows = [list(row) for r in results for row in r.rows]
    note = f"-- stacked: rows from step(s) {', '.join(map(str, steps))}"
    return ExecutionResult(
        ok=True, sql="\n".join([note, *(r.sql for r in results)]), columns=columns,
        rows=rows, row_count=len(rows),
    )
