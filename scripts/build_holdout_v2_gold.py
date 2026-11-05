"""Build, verify and screen the gold of holdout_agent_v2. Offline, no LLM.

    python scripts/build_holdout_v2_gold.py            # write the gold file
    python scripts/build_holdout_v2_gold.py --check    # rebuild, compare and re-screen;
                                                       # exit 1 on drift or a problem

Each entry comes from the frozen builder's build_entry (scripts/build_gold.py, imported,
unchanged), so it is hand-written SQL that was executed, in the format the frozen grader
reads, with the ID-for-name rule enforced. On top of that:

  composition  the mix of types is fixed below, in the same types as holdout_agent
  verify       every gold, interpretation and why-evidence table has an independent
               second SQL under `verify`, and the two must agree
  similarity   TF-IDF cosine < 0.5 to every question in every other set, the dev probes
               and the example pool
  answer       no gold or evidence table of another set is contained in a table here,
               and no distinctive value (a decimal, a count >= 100, a product or
               customer name) is shared with one

The example pool must come out unchanged with this set registered: the pool lives in the
agent's context, which is frozen for this evaluation. tests/test_context.py checks that.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
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
    V2_GOLD_FILES,
    V2_SET_FILES,
    load_questions,
)

SET_NAME = "holdout_agent_v2"
#: Same types as holdout_agent (3 each of 7 types), scaled to 17.
COMPOSITION = {"multi_hop": 2, "term_matching": 2, "why": 3, "statistics": 3,
               "recovery": 2, "ambiguous": 2, "segment_comparison": 3}
SIMILARITY_LIMIT = 0.5
FLOAT_REL = 1e-9
PROBES = ROOT / "eval" / "dev_probes.yaml"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(conn: sqlite3.Connection, sql: str) -> list[list[Any]]:
    return [list(r) for r in conn.execute(sql).fetchall()]


def _key(row: list[Any]) -> list[str]:
    return [c.lower() if isinstance(c, str) else "" for c in row]


def _same(a: list[list[Any]], b: list[list[Any]]) -> bool:
    """Equal as multisets: text exactly, numbers within rounding."""
    a, b = sorted(a, key=_key), sorted(b, key=_key)
    if len(a) != len(b):
        return False
    for row_a, row_b in zip(a, b, strict=True):
        if len(row_a) != len(row_b):
            return False
        for x, y in zip(row_a, row_b, strict=True):
            if isinstance(x, str) or isinstance(y, str):
                if x != y:
                    return False
            elif abs(float(x) - float(y)) > FLOAT_REL * max(1.0, abs(float(y))):
                return False
    return True


def _verified(conn: sqlite3.Connection, where: str, rows: list[list[Any]],
              sql: str | None) -> list[str]:
    if not sql:
        return [f"{where}: no independent second SQL"]
    second = _rows(conn, sql)
    if _same(rows, second):
        return []
    return [f"{where}: second SQL disagrees: {rows} vs {second}"]


def verification_problems(conn: sqlite3.Connection, q: dict[str, Any],
                          entry: dict[str, Any]) -> list[str]:
    qid, problems = q["id"], []
    if q["type"] == "ambiguous":
        for interp in q["interpretations"]:
            name = interp["name"]
            rows = entry["interpretations"][name]["rows"]
            problems += _verified(conn, f"{qid}/{name}", rows, interp.get("verify"))
    elif q["type"] == "why":
        for name, table in entry["evidence"].items():
            problems += _verified(conn, f"{qid}/{name}", table["rows"],
                                  (q.get("verify") or {}).get(name))
    else:
        problems += _verified(conn, qid, entry["rows"], q.get("verify"))
    return problems


def build(conn: sqlite3.Connection) -> dict[str, Any]:
    builder = _script("build_gold")
    questions = load_questions(SET_NAME)
    problems = [p for q in questions for p in question_problems(q)]
    counts = dict(Counter(q["type"] for q in questions))
    if counts != COMPOSITION:
        problems.append(f"composition {counts} != {COMPOSITION}")
    gold = {}
    for q in questions:
        gold[q["id"]] = builder.build_entry(conn, q)
        problems += verification_problems(conn, q, gold[q["id"]])
    if problems:
        raise RuntimeError("invalid question set:\n  " + "\n  ".join(problems))
    return {
        "set": SET_NAME,
        "questions_file": V2_SET_FILES[SET_NAME].relative_to(ROOT).as_posix(),
        "questions_sha256": file_sha256(V2_SET_FILES[SET_NAME]),
        "db_fingerprint": database_fingerprint(conn),
        "gold": gold,
    }


def _other_questions() -> list[tuple[str, str]]:
    found = [(q["id"], q["question"]) for name in ALL_SET_FILES if name != SET_NAME
             for q in load_questions(name)]
    probes = yaml.safe_load(PROBES.read_text("utf-8"))
    found += [(p["id"], p["question"]) for p in probes]
    found += [(f"pool/{e.id}", e.question) for e in load_examples()]
    return found


def _tables(entry: dict[str, Any]) -> list[list[list[Any]]]:
    found = [entry["rows"]] if "rows" in entry else []
    found += [t["rows"] for t in entry.get("interpretations", {}).values()]
    found += [t["rows"] for t in entry.get("evidence", {}).values()]
    return found


def screen(conn: sqlite3.Connection, payload: dict[str, Any]) -> list[str]:
    """Problems found by the automatic screening (an empty list is a pass)."""
    pool = _script("build_example_pool")
    questions = load_questions(SET_NAME)
    problems = []
    others = _other_questions()
    ids = [i for i, _ in others if not i.startswith("pool/")]
    ids += [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        problems.append("question ids repeat across sets")
    sims = pairwise_similarity([q["question"] for q in questions],
                               [t for _, t in others])
    for q, row in zip(questions, sims, strict=True):
        best = max(range(len(others)), key=lambda j: row[j])
        if row[best] >= SIMILARITY_LIMIT:
            problems.append(
                f"{q['id']}: similarity {row[best]:.2f} to {others[best][0]}")
    tables = pool.gold_tables([n for n in ALL_SET_FILES if n != SET_NAME])
    names = {r[0] for r in conn.execute(
        "SELECT product_name FROM products UNION SELECT customer_name FROM customers")}
    for qid, entry in payload["gold"].items():
        for rows in _tables(entry):
            overlap = pool.answer_overlap(rows, tables, names)
            if overlap:
                hits = ", ".join(overlap)
                problems.append(f"{qid}: shares an answer value with {hits}")
    return sorted(set(problems))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    conn = connect_read_only(Settings().database_path)
    builder = _script("build_gold")
    payload = build(conn)
    problems = screen(conn, payload)
    for problem in problems:
        print(problem)
    path = V2_GOLD_FILES[SET_NAME]
    if args.check:
        stored = json.loads(path.read_text("utf-8")) if path.exists() else None
        if builder.canonical(stored) != builder.canonical(payload):
            print("gold drift")
            return 1
        return 1 if problems else 0
    if problems:
        return 1
    path.write_text(builder.serialise(payload), "utf-8", newline="\n")
    print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
