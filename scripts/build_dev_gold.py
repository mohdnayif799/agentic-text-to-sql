"""Build, verify and screen the gold of the development sets. Offline, no LLM.

    python scripts/build_dev_gold.py            # write eval/gold/<dev set>.json
    python scripts/build_dev_gold.py --check    # rebuild, compare and re-screen;
                                                # exit 1 on drift or a problem

Development sets (evalrun/sets.py) are not frozen, but their gold is built
the same way as the protocol's: each entry comes from the frozen builder's
build_entry (scripts/build_gold.py, imported, unchanged), so it is
hand-written SQL that was executed, with the ID-for-name rule enforced.
On top of that:

  structure   evalkit's question checks; ids unique across every set
  verify      statistics answers are re-computed independently with pandas
              and must agree (and must not depend on the SD convention)
  similarity  TF-IDF cosine < 0.5 to every showcase and holdout question
  answer      no showcase or holdout gold or evidence table is contained in
              a dev gold, and no distinctive value (a decimal, a count >= 100,
              a product or customer name) is shared with one

The manual part of the screening (same template, event or fact as a holdout
question) is recorded per question under `screening` in the YAML.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.context.examples import pairwise_similarity  # noqa: E402
from evalkit.gold import (  # noqa: E402
    connect_read_only,
    database_fingerprint,
    file_sha256,
)
from evalkit.questions import SET_FILES, load_set, question_problems  # noqa: E402
from evalrun.sets import (  # noqa: E402
    ALL_SET_FILES,
    DEV_GOLD_FILES,
    DEV_SET_FILES,
    load_questions,
)

SIMILARITY_LIMIT = 0.5
FLOAT_REL = 1e-9


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _monthly_signups(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT substr(signup_date, 1, 7) AS month, COUNT(*) AS signups "
        "FROM customers WHERE signup_date BETWEEN '2023-01-01' AND '2024-12-31' "
        "GROUP BY month ORDER BY month", conn)


def verify_dv03(conn: sqlite3.Connection) -> list[list[Any]]:
    frame = _monthly_signups(conn)
    if len(frame) != 24:
        raise RuntimeError(f"expected 24 months of signups, got {len(frame)}")
    slope, intercept = np.polyfit(np.arange(1, 25, dtype=float), frame["signups"], 1)
    return [[m, float(intercept + slope * x)]
            for m, x in (("2025-01", 25), ("2025-02", 26), ("2025-03", 27))]


def verify_dv04(conn: sqlite3.Connection) -> list[list[Any]]:
    frame = pd.read_sql(
        "SELECT substr(created_date, 1, 7) AS month, "
        "julianday(resolved_date) - julianday(created_date) AS days "
        "FROM support_tickets WHERE resolved_date IS NOT NULL "
        "AND created_date BETWEEN '2024-01-01' AND '2025-12-31'", conn)
    monthly = frame.groupby("month")["days"].mean()
    if len(monthly) != 24:
        raise RuntimeError(f"expected 24 months, got {len(monthly)}")
    picks = {ddof: tuple(monthly.index[
        (monthly - monthly.mean()).abs() > 2 * monthly.std(ddof=ddof)])
        for ddof in (0, 1, 2)}
    if len(set(picks.values())) != 1:
        raise RuntimeError(f"answer depends on SD convention: {picks}")
    return [[m, float(monthly[m])] for m in picks[1]]


#: Independent re-computations of statistics golds, by question id.
VERIFY: dict[str, Callable[[sqlite3.Connection], list[list[Any]]]] = {
    "dv03": verify_dv03,
    "dv04": verify_dv04,
}


def _same(a: list[list[Any]], b: list[list[Any]]) -> bool:
    if len(a) != len(b):
        return False
    for row_a, row_b in zip(a, b, strict=True):
        for x, y in zip(row_a, row_b, strict=True):
            if isinstance(x, float) or isinstance(y, float):
                if abs(float(x) - float(y)) > FLOAT_REL * max(1.0, abs(float(y))):
                    return False
            elif x != y:
                return False
    return True


def build(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    builder = _script("build_gold")
    questions = load_questions(name)
    problems = [p for q in questions for p in question_problems(q)]
    if any(q["type"] != "why" and "pandas" in q.get("gold", {}) for q in questions):
        problems.append("dev gold must be SQL (the frozen pandas registry is not "
                        "extended); verify statistics in VERIFY instead")
    if problems:
        raise RuntimeError("invalid question set:\n  " + "\n  ".join(problems))
    gold = {}
    for q in questions:
        entry = builder.build_entry(conn, q)
        if q["id"] in VERIFY and not _same(entry["rows"], VERIFY[q["id"]](conn)):
            raise RuntimeError(f"{q['id']}: SQL gold and pandas re-computation differ")
        gold[q["id"]] = entry
    return {
        "set": name,
        "questions_file": DEV_SET_FILES[name].relative_to(ROOT).as_posix(),
        "questions_sha256": file_sha256(DEV_SET_FILES[name]),
        "db_fingerprint": database_fingerprint(conn),
        "gold": gold,
    }


def screen(conn: sqlite3.Connection, name: str, payload: dict[str, Any]) -> list[str]:
    """Problems found by the automatic screening (an empty list is a pass)."""
    pool = _script("build_example_pool")
    questions = load_questions(name)
    problems = []
    ids = [q["id"] for n in ALL_SET_FILES for q in load_questions(n)]
    if len(ids) != len(set(ids)):
        problems.append("question ids repeat across sets")
    protocol = [q for n in SET_FILES for q in load_set(n)]
    sims = pairwise_similarity([q["question"] for q in questions],
                               [q["question"] for q in protocol])
    for q, row in zip(questions, sims, strict=True):
        best = max(range(len(protocol)), key=lambda j: row[j])
        if row[best] >= SIMILARITY_LIMIT:
            problems.append(f"{q['id']}: similarity {row[best]:.2f} to "
                            f"{protocol[best]['id']}")
    tables = pool.gold_tables(list(SET_FILES))
    names = {r[0] for r in conn.execute(
        "SELECT product_name FROM products UNION SELECT customer_name FROM customers")}
    for qid, entry in payload["gold"].items():
        overlap = pool.answer_overlap(entry["rows"], tables, names)
        if overlap:
            problems.append(f"{qid}: shares an answer value with {', '.join(overlap)}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sets", nargs="*", default=list(DEV_SET_FILES))
    args = ap.parse_args()

    conn = connect_read_only(Settings().database_path)
    builder = _script("build_gold")
    failed = []
    for name in args.sets:
        payload = build(conn, name)
        problems = screen(conn, name, payload)
        for problem in problems:
            print(f"{name}: {problem}")
        failed += [name] if problems else []
        path = DEV_GOLD_FILES[name]
        if args.check:
            stored = json.loads(path.read_text("utf-8")) if path.exists() else None
            if builder.canonical(stored) != builder.canonical(payload):
                print(f"{name}: gold drift")
                failed.append(name)
            continue
        if not problems:
            path.write_text(builder.serialise(payload), "utf-8", newline="\n")
            print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
