"""Build, verify and screen the gold of the romanized Hindi and Telugu sets.
Offline, no LLM.

    python scripts/build_romanized_gold.py            # write the two gold files
    python scripts/build_romanized_gold.py --check    # rebuild, compare, re-screen;
                                                      # exit 1 on drift or a problem

The two sets (eval/romanized_hi.yaml, eval/romanized_te.yaml) are the same ten
NorthStar questions, once in Hindi and once in Telugu, both typed in English
letters. Each entry comes from the frozen builder's build_entry
(scripts/build_gold.py, imported, unchanged). On top of that:

  pairs       question n of one set and question n of the other have the same
              gold SQL and the same English gloss, so the two languages are
              scored against one answer
  verify      every gold has an independent second SQL under `verify`, and the
              two must agree (the check of scripts/build_holdout_v2_gold.py,
              imported, unchanged)
  similarity  the English gloss is TF-IDF cosine < 0.5 to every question in
              every other set, the dev probes and the example pool, so none is
              a frozen-50 question in another language
  answer      no gold table of another set is contained in a gold here, and no
              distinctive value is shared with one
  script      the question text is plain ASCII: no native script
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.context.examples import load_examples, pairwise_similarity  # noqa: E402
from evalkit.gold import (  # noqa: E402
    connect_read_only,
    database_fingerprint,
    file_sha256,
)
from evalkit.questions import question_problems  # noqa: E402
from evalrun.sets import (  # noqa: E402
    ALL_SET_FILES,
    ROMANIZED_GOLD_FILES,
    ROMANIZED_SET_FILES,
    load_questions,
)

QUESTIONS_PER_SET = 10
SIMILARITY_LIMIT = 0.5
PROBES = ROOT / "eval" / "dev_probes.yaml"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pair_problems() -> list[str]:
    """The two sets must be the same questions against the same gold."""
    sets = [load_questions(name) for name in ROMANIZED_SET_FILES]
    problems = [f"{name}: {len(qs)} questions, not {QUESTIONS_PER_SET}"
                for name, qs in zip(ROMANIZED_SET_FILES, sets, strict=True)
                if len(qs) != QUESTIONS_PER_SET]
    first, *rest = sets
    for other in rest:
        for a, b in zip(first, other, strict=False):
            where = f"{a['id']}/{b['id']}"
            if " ".join(a["gold"]["sql"].split()) != " ".join(b["gold"]["sql"].split()):
                problems.append(f"{where}: gold SQL differs between the languages")
            if a.get("english") != b.get("english"):
                problems.append(f"{where}: English gloss differs")
            if a["question"] == b["question"]:
                problems.append(f"{where}: the two languages have the same text")
    return problems


def build(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    builder, v2 = _script("build_gold"), _script("build_holdout_v2_gold")
    questions = load_questions(name)
    problems = [p for q in questions for p in question_problems(q)]
    gold = {}
    for q in questions:
        if q["type"] != "easy":
            problems.append(f"{q['id']}: romanized questions are type easy")
        if not q.get("english"):
            problems.append(f"{q['id']}: missing English gloss")
        if not q["question"].isascii():
            problems.append(f"{q['id']}: question is not plain ASCII")
        gold[q["id"]] = builder.build_entry(conn, q)
        problems += v2._verified(conn, q["id"], gold[q["id"]]["rows"], q.get("verify"))
    if problems:
        raise RuntimeError("invalid question set:\n  " + "\n  ".join(problems))
    return {
        "set": name,
        "questions_file": ROMANIZED_SET_FILES[name].relative_to(ROOT).as_posix(),
        "questions_sha256": file_sha256(ROMANIZED_SET_FILES[name]),
        "db_fingerprint": database_fingerprint(conn),
        "gold": gold,
    }


def _other_questions() -> list[tuple[str, str]]:
    found = [(q["id"], q["question"]) for name in ALL_SET_FILES
             if name not in ROMANIZED_SET_FILES for q in load_questions(name)]
    found += [(p["id"], p["question"])
              for p in yaml.safe_load(PROBES.read_text("utf-8"))]
    found += [(f"pool/{e.id}", e.question) for e in load_examples()]
    return found


def screen(conn: sqlite3.Connection, name: str, payload: dict[str, Any]) -> list[str]:
    """Problems found by the automatic screening (an empty list is a pass)."""
    pool = _script("build_example_pool")
    questions = load_questions(name)
    problems = []
    ids = [q["id"] for n in ALL_SET_FILES for q in load_questions(n)]
    if len(ids) != len(set(ids)):
        problems.append("question ids repeat across sets")
    others = _other_questions()
    sims = pairwise_similarity([q["english"] for q in questions],
                               [text for _, text in others])
    for q, row in zip(questions, sims, strict=True):
        best = max(range(len(others)), key=lambda j: row[j])
        if row[best] >= SIMILARITY_LIMIT:
            problems.append(
                f"{q['id']}: similarity {row[best]:.2f} to {others[best][0]}")
    tables = pool.gold_tables([n for n in ALL_SET_FILES
                               if n not in ROMANIZED_SET_FILES])
    names = {r[0] for r in conn.execute(
        "SELECT product_name FROM products UNION SELECT customer_name FROM customers")}
    for qid, entry in payload["gold"].items():
        overlap = pool.answer_overlap(entry["rows"], tables, names)
        if overlap:
            problems.append(f"{qid}: shares an answer value with {', '.join(overlap)}")
    return sorted(set(problems))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    conn = connect_read_only(Settings().database_path)
    builder = _script("build_gold")
    failed = False
    for problem in pair_problems():
        print(problem)
        failed = True
    payloads = {name: build(conn, name) for name in ROMANIZED_SET_FILES}
    rows = [[entry["rows"] for entry in payload["gold"].values()]
            for payload in payloads.values()]
    if any(r != rows[0] for r in rows):
        print("the two languages do not have the same gold rows")
        failed = True
    for name, payload in payloads.items():
        problems = screen(conn, name, payload)
        for problem in problems:
            print(f"{name}: {problem}")
        failed = failed or bool(problems)
        path = ROMANIZED_GOLD_FILES[name]
        if args.check:
            stored = json.loads(path.read_text("utf-8")) if path.exists() else None
            if builder.canonical(stored) != builder.canonical(payload):
                print(f"{name}: gold drift")
                failed = True
    if args.check or failed:
        return 1 if failed else 0
    for name, payload in payloads.items():
        path = ROMANIZED_GOLD_FILES[name]
        path.write_text(builder.serialise(payload), "utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
