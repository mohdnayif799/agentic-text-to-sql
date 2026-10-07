"""Strict result-set grader (eval protocol v2).

A result passes only if, for some one-to-one mapping of gold columns onto the
result's columns, the result's rows equal the gold rows as a multiset (or as a
sequence when the question asks for an order). Consequences, all deliberate:

* extra result columns are fine; a missing gold column is a failure;
* the row count must be exact, so a top-N question needs exactly N rows;
* integers (counts) must match exactly; floats match within
  max(abs, rel * |gold|);
* text is compared case- and whitespace-insensitively; numeric text is read
  as a number;
* there is no "number appears anywhere" and no "first cell only" shortcut.

For which/top-N questions `grade_top_n` also accepts a full ranking whose
outermost ORDER BY is the ranking metric and whose first N rows match
(protocol v2, rule 3). Transposed results are never accepted.

Column names are ignored: models alias freely, so matching is by values.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from evalkit.ranking import ranks_by_metric

DEFAULT_REL_TOL = 1e-3
"""0.1%: absorbs rounding of large values without hiding a wrong filter."""

DEFAULT_ABS_TOL = 0.01
"""Absorbs ROUND(x, 2) on money and on small ratios."""

_INT_TEXT = re.compile(r"[-+]?\d+")

Cell = int | float | str | None


@dataclass(frozen=True)
class Tolerance:
    rel: float = DEFAULT_REL_TOL
    abs: float = DEFAULT_ABS_TOL


@dataclass(frozen=True)
class Gold:
    """The expected result set. Only the columns that answer the question."""

    rows: tuple[tuple[Any, ...], ...]
    ordered: bool = False
    tolerance: Tolerance = field(default_factory=Tolerance)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    mapping: tuple[int, ...] = ()
    """For each gold column, the index of the result column it matched."""
    mode: str = ""
    """How a pass was reached: "exact" or "ranked_prefix" (top-N rule)."""


def normalize(value: Any) -> Cell:
    """One comparable form per cell: int, float, lowercase text or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    text = " ".join(str(value).split()).lower()
    if _INT_TEXT.fullmatch(text):
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def cell_matches(got: Cell, want: Cell, tol: Tolerance) -> bool:
    if want is None or got is None:
        return want is None and got is None
    if isinstance(want, str) or isinstance(got, str):
        return want == got
    if isinstance(want, int):
        # Counts are exact. A float that equals the integer is accepted.
        return float(got) == float(want)
    return abs(float(got) - want) <= max(tol.abs, tol.rel * abs(want))


def _has_perfect_matching(n: int, adjacency: list[list[int]]) -> bool:
    """Kuhn's algorithm: can every left node be paired with a distinct right?

    `n` is the number of right nodes; `adjacency[left]` lists the right nodes
    each left node may pair with.
    """
    owner = [-1] * n

    def assign(left: int, seen: list[bool]) -> bool:
        for right in adjacency[left]:
            if seen[right]:
                continue
            seen[right] = True
            if owner[right] == -1 or assign(owner[right], seen):
                owner[right] = left
                return True
        return False

    return all(assign(left, [False] * n) for left in range(len(adjacency)))


def _rows_match(
    got: list[list[Cell]],
    want: list[list[Cell]],
    mapping: Sequence[int],
    ordered: bool,
    tol: Tolerance,
) -> bool:
    def same(g_row: list[Cell], w_row: list[Cell]) -> bool:
        return all(
            cell_matches(g_row[k], w_row[j], tol) for j, k in enumerate(mapping)
        )

    if ordered:
        return all(same(g, w) for g, w in zip(got, want, strict=True))
    adjacency = [
        [i for i, g in enumerate(got) if same(g, w)] for w in want
    ]
    return _has_perfect_matching(len(got), adjacency)


def _column_candidates(
    got: list[list[Cell]],
    want: list[list[Cell]],
    ordered: bool,
    tol: Tolerance,
) -> list[list[int]]:
    """Result columns whose values alone could match each gold column.

    A cheap necessary condition that prunes the assignment search.
    """
    return [
        [
            k for k in range(len(got[0]))
            if _rows_match(
                [[row[k]] for row in got], [[row[j]] for row in want],
                (0,), ordered, tol,
            )
        ]
        for j in range(len(want[0]))
    ]


def _search(
    candidates: list[list[int]],
    check: Callable[[tuple[int, ...]], bool],
) -> tuple[int, ...] | None:
    """Backtracking over one-to-one column assignments, tightest column first."""
    order = sorted(range(len(candidates)), key=lambda j: len(candidates[j]))
    chosen: dict[int, int] = {}

    def step(depth: int) -> tuple[int, ...] | None:
        if depth == len(order):
            mapping = tuple(chosen[j] for j in range(len(candidates)))
            return mapping if check(mapping) else None
        j = order[depth]
        used = set(chosen.values())
        for k in candidates[j]:
            if k in used:
                continue
            chosen[j] = k
            found = step(depth + 1)
            if found is not None:
                return found
            del chosen[j]
        return None

    return step(0)


def grade(gold: Gold, rows: Sequence[Sequence[Any]]) -> Verdict:
    """Compare a result's rows against the gold rows. Pure function."""
    want = [[normalize(v) for v in row] for row in gold.rows]
    got = [[normalize(v) for v in row] for row in rows]

    if len(got) != len(want):
        return Verdict(False, f"expected {len(want)} row(s), got {len(got)}")
    if not want:
        return Verdict(True, "empty result, as expected")

    n_gold, n_got = len(want[0]), len(got[0])
    if n_got < n_gold:
        return Verdict(
            False, f"result has {n_got} column(s); gold needs {n_gold}"
        )

    tol = gold.tolerance
    candidates = _column_candidates(got, want, gold.ordered, tol)
    missing = [j for j, c in enumerate(candidates) if not c]
    if missing:
        return Verdict(
            False,
            f"no result column matches gold column(s) {missing}; "
            f"first gold row {list(gold.rows[0])}",
        )

    mapping = _search(
        candidates,
        lambda m: _rows_match(got, want, m, gold.ordered, tol),
    )
    if mapping is None:
        return Verdict(False, "columns match individually but rows do not line up")
    order = " in order" if gold.ordered else ""
    return Verdict(True, f"{len(want)} row(s) match{order}", mapping, mode="exact")


def contains_gold(gold: Gold, rows: Sequence[Sequence[Any]]) -> bool:
    """Do all gold rows appear in the result, ignoring extra rows?

    Diagnostic only (never a pass): separates "right values, wrong shape"
    from "wrong values" when reporting failures.
    """
    want = [[normalize(v) for v in row] for row in gold.rows]
    got = [[normalize(v) for v in row] for row in rows]
    if not want:
        return True
    if len(got) < len(want) or len(got[0]) < len(want[0]):
        return False
    tol = gold.tolerance
    candidates = _column_candidates(got, want, False, tol)
    if not all(candidates):
        return False
    return _search(
        candidates, lambda m: _rows_match(got, want, m, False, tol)
    ) is not None


def grade_top_n(
    gold: Gold,
    rows: Sequence[Sequence[Any]],
    *,
    top_n: int,
    sql: str | None,
    columns: Sequence[str] | None,
) -> Verdict:
    """Which/top-N rule: exactly N matching rows, OR a result whose outermost
    query is ORDER BY the ranking metric and whose first N rows match.

    Transposed results are never accepted.
    """
    exact = grade(gold, rows)
    if exact.passed or len(rows) <= top_n:
        return exact
    if not sql or columns is None:
        return Verdict(False, f"{exact.reason}; no SQL to show the extra rows "
                              "are a ranking")
    ranked, why = ranks_by_metric(sql, columns, rows)
    if not ranked:
        return Verdict(False, f"{exact.reason}; extra rows are not a ranking: {why}")
    prefix = grade(gold, rows[:top_n])
    if not prefix.passed:
        return Verdict(False, f"{why}, but first {top_n} row(s) differ: "
                              f"{prefix.reason}")
    return Verdict(True, f"first {top_n} row(s) of a result {why} match",
                   prefix.mapping, mode="ranked_prefix")
