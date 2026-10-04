"""Question sets for eval protocol v2: file locations, schema and composition.

Validation is structural only (fields, types, counts per type). Whether a gold
answer is right is established by executing it (scripts/build_gold.py) and by
review, not here.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

AGENT_TYPES = (
    "multi_hop",
    "term_matching",
    "why",
    "statistics",
    "recovery",
    "ambiguous",
    "segment_comparison",
)
EASY = "easy"
RESULT_TYPES = frozenset(
    {"multi_hop", "term_matching", "statistics", "recovery",
     "segment_comparison", EASY}
)
"""Types graded by result-set match (plus secondary answer checks)."""

SET_FILES = {
    "showcase": ROOT / "eval" / "showcase.yaml",
    "holdout_agent": ROOT / "eval" / "holdout_agent.yaml",
    "holdout_easy": ROOT / "eval" / "holdout_easy.yaml",
}
GOLD_FILES = {name: ROOT / "eval" / "gold" / f"{name}.json" for name in SET_FILES}

SET_COMPOSITION: dict[str, dict[str, int]] = {
    "showcase": {"multi_hop": 1, "term_matching": 1, "why": 1,
                 "statistics": 1, "recovery": 1},
    "holdout_agent": dict.fromkeys(AGENT_TYPES, 3),
    "holdout_easy": {EASY: 12},
}

_CHECK_KINDS = {"text", "number", "pattern", "any_of", "gold_number", "gold_text"}
_CHECK_OPTIONS = {"rel", "abs"}


def load_set(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load(SET_FILES[name].read_text("utf-8")) or []


def _check_problems(where: str, checks: Any) -> list[str]:
    if not isinstance(checks, list):
        return [f"{where}: answer checks must be a list"]
    problems = []
    for check in checks:
        kinds = set(check) & _CHECK_KINDS if isinstance(check, Mapping) else set()
        extra = set(check) - _CHECK_KINDS - _CHECK_OPTIONS if kinds else set()
        if len(kinds) != 1 or extra:
            problems.append(f"{where}: malformed check {check!r}")
            continue
        (kind,) = kinds
        if kind == "pattern":
            try:
                re.compile(check["pattern"])
            except re.error as exc:
                problems.append(f"{where}: bad regex {check['pattern']!r}: {exc}")
        if kind == "any_of":
            problems += _check_problems(where, check["any_of"])
        if kind in ("gold_number", "gold_text"):
            ref = check[kind]
            if not (isinstance(ref, list) and len(ref) == 2
                    and all(isinstance(i, int) for i in ref)):
                problems.append(f"{where}: {kind} must be [row, col]")
    return problems


def _gold_problems(where: str, gold: Any) -> list[str]:
    if not isinstance(gold, Mapping) or len(set(gold) & {"sql", "pandas"}) != 1:
        return [f"{where}: gold needs exactly one of sql/pandas"]
    return []


def question_problems(q: Mapping[str, Any]) -> list[str]:
    qid = q.get("id", "?")
    kind = q.get("type")
    problems = []
    if not q.get("question"):
        problems.append(f"{qid}: missing question text")
    top_n = q.get("top_n")
    if top_n is not None and not (isinstance(top_n, int) and top_n >= 1):
        problems.append(f"{qid}: top_n must be a positive integer")
    if kind in RESULT_TYPES:
        problems += _gold_problems(qid, q.get("gold"))
        problems += _check_problems(qid, q.get("answer_checks"))
    elif kind == "why":
        if not q.get("driver_checks"):
            problems.append(f"{qid}: why-question needs driver_checks")
        problems += _check_problems(qid, q.get("driver_checks") or [])
        if not q.get("judge_rubric"):
            problems.append(f"{qid}: why-question needs judge_rubric")
        if not q.get("evidence"):
            problems.append(f"{qid}: why-question needs evidence queries")
    elif kind == "ambiguous":
        interps = q.get("interpretations") or []
        names = [i.get("name") for i in interps]
        if len(interps) < 2 or len(set(names)) != len(names):
            problems.append(f"{qid}: needs >= 2 uniquely named interpretations")
        for interp in interps:
            where = f"{qid}/{interp.get('name')}"
            problems += _gold_problems(where, interp.get("gold"))
            checks = interp.get("answer_checks")
            if not checks:
                problems.append(f"{where}: interpretation needs answer_checks")
            problems += _check_problems(where, checks or [])
    else:
        problems.append(f"{qid}: unknown type {kind!r}")
    return problems


def set_problems(name: str, questions: Sequence[Mapping[str, Any]]) -> list[str]:
    problems = []
    ids = [q.get("id") for q in questions]
    dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
    if dupes:
        problems.append(f"{name}: duplicate ids {dupes}")
    counts = Counter(q.get("type") for q in questions)
    if dict(counts) != SET_COMPOSITION[name]:
        problems.append(
            f"{name}: composition {dict(counts)} != {SET_COMPOSITION[name]}"
        )
    for q in questions:
        problems += question_problems(q)
    return problems
