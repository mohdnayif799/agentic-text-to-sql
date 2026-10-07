"""Rule-based checks on the final answer text (eval protocol v2).

These are secondary to the result-set grade: they catch "correct SQL, wrong
final answer". The checks for a question are written in its YAML entry before
any run. Check kinds:

    {text: "APAC"}                    phrase present (case/hyphen-insensitive)
    {number: 342941.73}               a number in the text matches (tolerant)
    {number: 0.14, rel: 0, abs: 0.005}
    {pattern: "fewer\\s+orders"}      case-insensitive regex
    {any_of: [<check>, <check>]}      at least one passes

Numbers in prose are rounded, so the tolerance is looser than the result
grader's: max(1% relative, 0.005 absolute). The sign is ignored ("fell 43%"
matches -42.96), a percentage also matches its fraction, and k/M/million/
billion suffixes are expanded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ANSWER_REL_TOL = 0.01
ANSWER_ABS_TOL = 0.005

_SCALE = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mn": 1e6, "million": 1e6,
    "bn": 1e9, "billion": 1e9,
}
_NUMBER = re.compile(
    r"(?<![\w.])-?\$?(\d[\d,]*(?:\.\d+)?|\.\d+)"
    r"(?:\s*(%|percent\b|k\b|thousand\b|mn\b|m\b|million\b|bn\b|billion\b))?",
    re.IGNORECASE,
)

ASSUMPTION_MARKERS = (
    r"\bassum",
    r"\binterpret",
    r"\bdefin(e|ed|ing|ition)\b",
    r"\bbased on\b",
    r"\bmeasured (by|as|in|on)\b",
    r"\bif you mean\b",
    r"\bif by\b",
    r"\bi (used|took|treated|counted|chose|read)\b",
    r"\bin terms of\b",
    r"\btaking\b.{0,40}\bas\b",
    r"\bby (total |completed |net )?(revenue|sales|units|orders|order count"
    r"|order value|spend)\b",
)


@dataclass(frozen=True)
class FoundNumber:
    value: float
    percent: bool


@dataclass(frozen=True)
class AnswerVerdict:
    passed: bool
    failed: tuple[str, ...] = ()


def extract_numbers(text: str) -> list[FoundNumber]:
    found = []
    for match in _NUMBER.finditer(text or ""):
        digits, unit = match.group(1).replace(",", ""), (match.group(2) or "")
        try:
            value = float(digits)
        except ValueError:
            continue
        unit = unit.lower()
        is_percent = unit in ("%", "percent")
        value *= 1.0 if is_percent or not unit else _SCALE[unit]
        found.append(FoundNumber(value=value, percent=is_percent))
    return found


def number_present(
    text: str,
    target: float,
    *,
    rel: float = ANSWER_REL_TOL,
    abs_: float = ANSWER_ABS_TOL,
) -> bool:
    want = abs(float(target))
    slack = max(abs_, rel * want)
    for n in extract_numbers(text):
        options = [n.value, n.value / 100] if n.percent else [n.value]
        if any(abs(v - want) <= slack for v in options):
            return True
    return False


def _fold(text: str) -> str:
    return " ".join((text or "").replace("-", " ").lower().split())


def text_present(text: str, phrase: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(_fold(phrase)) + r"(?!\w)"
    return re.search(pattern, _fold(text)) is not None


def states_assumption(text: str) -> bool:
    return any(re.search(p, text or "", re.IGNORECASE) for p in ASSUMPTION_MARKERS)


def _label(check: Mapping[str, Any]) -> str:
    kind = next(iter(check))
    return f"{kind}: {check[kind]}"


def _passes(answer: str, check: Mapping[str, Any]) -> bool:
    if "text" in check:
        return text_present(answer, check["text"])
    if "number" in check:
        return number_present(
            answer, check["number"],
            rel=check.get("rel", ANSWER_REL_TOL),
            abs_=check.get("abs", ANSWER_ABS_TOL),
        )
    if "pattern" in check:
        return re.search(check["pattern"], answer or "", re.IGNORECASE) is not None
    if "any_of" in check:
        return any(_passes(answer, c) for c in check["any_of"])
    raise ValueError(f"unknown answer check: {dict(check)}")


def check_answer(
    answer: str, checks: Sequence[Mapping[str, Any]]
) -> AnswerVerdict:
    failed = tuple(_label(c) for c in checks if not _passes(answer, c))
    return AnswerVerdict(passed=not failed, failed=failed)
