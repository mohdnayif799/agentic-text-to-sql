"""Per-question scoring rules for eval protocol v2. Pure functions.

primary     the metric compared across architectures (McNemar, Wilson):
              result types  strict result-set match (evalkit.grader); for a
                            which/top-N question (gold `top_n`) a ranked result
                            is also accepted (rule 3) and the answer must name
                            the gold item(s)
              ambiguous     result matches an accepted interpretation AND the
                            answer names that interpretation's metric, states
                            an assumption and names the item
              why           driver check passes AND the rubric judge passes
                            (None while the judge verdict is pending)
answer_ok   the rule-based answer checks (secondary)
end_to_end  primary and answer_ok for result types; equal to primary otherwise
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evalkit.answer_checks import check_answer, states_assumption
from evalkit.grader import Gold, Tolerance, Verdict, grade, grade_top_n
from evalkit.questions import RESULT_TYPES

_YEAR_MONTH = re.compile(r"(\d{4})-(\d{2})")


@dataclass(frozen=True)
class Score:
    primary: bool | None
    answer_ok: bool | None
    end_to_end: bool | None
    detail: str


def gold_from_entry(entry: Mapping[str, Any]) -> Gold:
    tol = entry.get("tolerance") or {}
    return Gold(
        rows=tuple(tuple(r) for r in entry["rows"]),
        ordered=bool(entry.get("ordered", False)),
        tolerance=Tolerance(**tol) if tol else Tolerance(),
    )


def resolve_checks(
    checks: Sequence[Mapping[str, Any]], gold_rows: Sequence[Sequence[Any]]
) -> list[dict[str, Any]]:
    """Replace {gold_number: [r, c]} / {gold_text: [r, c]} with literal checks."""
    resolved = []
    for check in checks:
        options = {k: v for k, v in check.items() if k in ("rel", "abs")}
        if "gold_number" in check:
            r, c = check["gold_number"]
            resolved.append({"number": gold_rows[r][c], **options})
        elif "gold_text" in check:
            r, c = check["gold_text"]
            resolved.append({"text": str(gold_rows[r][c])})
        elif "any_of" in check:
            resolved.append({"any_of": resolve_checks(check["any_of"], gold_rows)})
        else:
            resolved.append(dict(check))
    return resolved


def item_name_checks(gold_rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """The answer must name every text cell of the gold rows (the items).

    A 'YYYY-MM' label may also be written as 'May 2025'.
    """
    checks: list[dict[str, Any]] = []
    for row in gold_rows:
        for cell in row:
            if not isinstance(cell, str):
                continue
            month = _YEAR_MONTH.fullmatch(cell)
            if month:
                name = calendar.month_name[int(month[2])]
                checks.append({"any_of": [
                    {"text": cell},
                    {"pattern": rf"\b{name[:3]}[a-z]*\.?\s+{month[1]}\b"},
                ]})
            else:
                checks.append({"text": cell})
    return checks


def result_verdict(
    entry: Mapping[str, Any],
    rows: Sequence[Sequence[Any]] | None,
    sql: str | None = None,
    columns: Sequence[str] | None = None,
) -> Verdict:
    if rows is None:
        return Verdict(False, "no selected result")
    gold = gold_from_entry(entry)
    top_n = entry.get("top_n")
    if top_n:
        return grade_top_n(gold, rows, top_n=top_n, sql=sql, columns=columns)
    return grade(gold, rows)


def _names_items(entry: Mapping[str, Any], answer: str) -> tuple[bool, tuple[str, ...]]:
    if not entry.get("top_n"):
        return True, ()
    verdict = check_answer(answer, item_name_checks(entry["rows"]))
    return verdict.passed, verdict.failed


def score_question(
    question: Mapping[str, Any],
    gold_entry: Mapping[str, Any],
    rows: Sequence[Sequence[Any]] | None,
    answer: str | None,
    judge_pass: bool | None = None,
    *,
    sql: str | None = None,
    columns: Sequence[str] | None = None,
) -> Score:
    """Score one architecture's outcome on one question.

    `rows`, `sql` and `columns` describe the selected result (the one the
    final answer was built from); `sql` is None when it came from a compute
    tool rather than a query.
    """
    kind = question["type"]
    answer = answer or ""

    if kind in RESULT_TYPES:
        verdict = result_verdict(gold_entry, rows, sql, columns)
        named, unnamed = _names_items(gold_entry, answer)
        primary = verdict.passed and named
        checks = resolve_checks(question.get("answer_checks") or [], gold_entry["rows"])
        answer_verdict = check_answer(answer, checks)
        detail = verdict.reason
        if verdict.passed and not named:
            detail += f"; answer does not name {list(unnamed)}"
        if not answer_verdict.passed:
            detail += f"; answer missing {list(answer_verdict.failed)}"
        return Score(primary, answer_verdict.passed,
                     primary and answer_verdict.passed, detail)

    if kind == "ambiguous":
        assumed = states_assumption(answer)
        matched, named = [], []
        for interp in question["interpretations"]:
            entry = gold_entry["interpretations"][interp["name"]]
            result_ok = result_verdict(entry, rows, sql, columns).passed
            checks = resolve_checks(interp["answer_checks"], entry["rows"])
            answer_ok = (
                assumed
                and check_answer(answer, checks).passed
                and _names_items(entry, answer)[0]
            )
            if answer_ok:
                named.append(interp["name"])
            if result_ok and answer_ok:
                matched.append(interp["name"])
        primary = bool(matched)
        detail = (
            f"matched interpretation {matched[0]}" if primary
            else f"no interpretation matched (assumption stated: {assumed}; "
                 f"answer fits: {named or 'none'})"
        )
        return Score(primary, bool(named), primary, detail)

    if kind == "why":
        driver = check_answer(answer, question["driver_checks"])
        if not driver.passed:
            return Score(False, False, False,
                         f"driver not named: {list(driver.failed)}")
        if judge_pass is None:
            return Score(None, True, None, "driver named; judge verdict pending")
        return Score(judge_pass, True, judge_pass,
                     f"driver named; judge {'pass' if judge_pass else 'fail'}")

    raise ValueError(f"unknown question type {kind!r}")
