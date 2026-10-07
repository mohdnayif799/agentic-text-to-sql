"""Build the verified-example pool for the shared context. Offline, no LLM.

    python scripts/build_example_pool.py

Candidates are the legacy questions that have a verified reference query.
A candidate is excluded if ANY rule fires (eval/PROTOCOL.md, protocol v2,
plus the stricter Phase 2 rule that no example may paraphrase or share an
answer with any showcase, development or holdout question):

  similarity  TF-IDF cosine >= 0.5 to any v2 question (pre-registered)
  answer      its result contains a v2 gold table, or shares a distinctive
              gold value (a decimal, a count >= 100, a product or customer
              name) with a v2 gold or evidence table
  manual      reviewed by hand: a paraphrase, the same query template as a
              v2 question or hop (only literals changed or one filter
              added), or the same event/fact as a v2 question

Writes agentcrew/context/examples.yaml: the pool (question and SQL only, no
results) and every exclusion with its reason.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.context.examples import EXAMPLES_PATH, pairwise_similarity  # noqa: E402
from evalkit.gold import connect_read_only  # noqa: E402
from evalkit.grader import Gold, contains_gold  # noqa: E402
from evalrun.sets import ALL_GOLD_FILES, ALL_SET_FILES, load_questions  # noqa: E402

SOURCES = {
    "orig": "eval/questions.yaml",
    "heldout": "eval/heldout.yaml",
    "diag": "eval/questions_diagnostic.yaml",
    "hold1": "eval/questions_holdout.yaml",
}
SIMILARITY_LIMIT = 0.5

MANUAL: dict[str, str] = {
    "diag/d13":
        "same template as he03 and ha04 (ticket count filtered by severity)",
    "diag/d19":
        "same 'never did X' anti-join template as ha14",
    "hold1/h09":
        "same 'never did X' anti-join template as ha14",
    "orig/q02":
        "same template as he01 (customer count for a region; only the region "
        "differs)",
    "orig/q04":
        "shares ha08's answer (APAC concentrates the Q3 2025 churn)",
    "orig/q05":
        "same template as he02 (completed revenue for a period; only the period "
        "differs)",
    "orig/q07":
        "shares a fact with ha21 (APAC opened the most 2025 tickets)",
    "orig/q09":
        "shares ha01's hop-1 answer (Mid-Market)",
    "orig/q10":
        "same event as ha07/ha09/ha11 (Q2 vs Q3 2025 revenue)",
    "orig/q11":
        "same event as ha07/ha09 (Q2 to Q3 2025 decline by category)",
    "orig/q12":
        "same event as ha07/ha09 (why revenue fell in Q3 2025)",
    "orig/q13":
        "shares he07's answer (cancelled and refunded order counts for 2025)",
    "orig/q14":
        "same template as sc05's spend half (marketing spend for a region in "
        "2025)",
    "orig/q15":
        "same template as sc01 hop 1 and ha01 hop 2 (top customers by spend)",
    "heldout/h02":
        "paraphrase-level match to ha10 (average vs median completed order "
        "value, 2025)",
    "heldout/h03":
        "same template as ha20's follow-up (refund rate by region in 2025)",
    "diag/d01":
        "shares sc03's answer fact (order volume by channel)",
    "diag/d02":
        "paraphrase-level match to he12 and shares its answer (highest list "
        "price)",
    "diag/d06":
        "same template as he10 (signups in a year; only the year differs)",
    "diag/d09":
        "shares sc03's answer fact (orders per channel); same template as he07",
    "diag/d11":
        "same template as ha06 (units sold in completed orders for a year)",
    "diag/d12":
        "same template as ha02 hop 1 (2025 month by marketing spend; sort "
        "reversed)",
    "diag/d14":
        "shares ha08's driver fact (churn concentrated in APAC)",
    "diag/d18":
        "same event as ha07 (regional revenue fall Q2 to Q3 2025)",
    "diag/d20":
        "same fact as ha08 (churn vs support tickets)",
    "diag/d21":
        "shares sc05's answer (LATAM 2025 marketing spend)",
    "diag/d22":
        "same event as ha09 (unit drop Q2 to Q3 2025)",
    "hold1/h02":
        "same template as he03 (ticket count by severity in 2025; only severity "
        "differs)",
}


def candidates() -> list[dict[str, Any]]:
    out = []
    for tag, rel in SOURCES.items():
        for q in yaml.safe_load((ROOT / rel).read_text("utf-8")):
            if q.get("reference_sql") and q.get("check") != "must_refuse":
                out.append({"id": f"{tag}/{q['id']}", "question": q["question"],
                            "sql": " ".join(q["reference_sql"].split())})
    return out


def gold_tables(sets: list[str] | None = None) -> dict[str, list[list[list[Any]]]]:
    """Gold, interpretation and evidence rows by question id (default: every
    set, development sets included)."""
    tables: dict[str, list[list[list[Any]]]] = {}
    for name in sets or ALL_SET_FILES:
        gold = json.loads(ALL_GOLD_FILES[name].read_text("utf-8"))["gold"]
        for qid, entry in gold.items():
            found = ([entry] if "rows" in entry else [])
            found += list(entry.get("interpretations", {}).values())
            found += list(entry.get("evidence", {}).values())
            tables[qid] = [t["rows"] for t in found]
    return tables


def _distinctive(cell: Any, names: set[str]) -> bool:
    if isinstance(cell, str):
        return cell in names
    if cell is None or isinstance(cell, bool):
        return False
    return (isinstance(cell, float) and cell != int(cell)) or abs(cell) >= 100


def _key(cell: Any) -> Any:
    return cell if isinstance(cell, str) else round(float(cell), 2)


def answer_overlap(rows, tables, names) -> list[str]:
    hits = set()
    for qid, gold_list in tables.items():
        cells = {_key(c) for g in gold_list for r in g for c in r
                 if _distinctive(c, names)}
        for g in gold_list:
            if g and contains_gold(Gold(rows=tuple(map(tuple, g))), rows):
                hits.add(qid)
        if any(_key(c) in cells for r in rows for c in r if _distinctive(c, names)):
            hits.add(qid)
    return sorted(hits)


def build(conn: sqlite3.Connection) -> dict[str, Any]:
    cands = candidates()
    v2 = [q for name in ALL_SET_FILES for q in load_questions(name)]
    sims = pairwise_similarity([c["question"] for c in cands],
                               [q["question"] for q in v2])
    names = {r[0] for r in conn.execute(
        "SELECT product_name FROM products UNION SELECT customer_name FROM customers"
    )}
    tables = gold_tables()
    pool, excluded = [], []
    for cand, row in zip(cands, sims, strict=True):
        best = max(range(len(v2)), key=lambda j: row[j])
        reasons = []
        if row[best] >= SIMILARITY_LIMIT:
            reasons.append(f"similarity {row[best]:.2f} to {v2[best]['id']}")
        result = [list(r) for r in conn.execute(cand["sql"]).fetchall()]
        overlap = answer_overlap(result, tables, names)
        # Full product lists name every product incidentally; only a top-row
        # overlap counts for those, which the manual review covers.
        if overlap and len(result) < 30:
            reasons.append(f"shares an answer value with {', '.join(overlap)}")
        if cand["id"] in MANUAL:
            reasons.append(MANUAL[cand["id"]])
        if reasons:
            excluded.append({"id": cand["id"], "question": cand["question"],
                             "reasons": reasons})
        else:
            pool.append(cand)
    return {"pool": pool, "excluded": excluded}


HEADER = """\
# Verified example pool for the shared context (question and SQL only).
# Generated by scripts/build_example_pool.py from the legacy question files;
# do not edit by hand. No example paraphrases or shares an answer with any
# showcase or holdout question; every exclusion and its reason is listed.
"""


def main() -> int:
    payload = build(connect_read_only(Settings().database_path))
    text = HEADER + yaml.safe_dump(payload, sort_keys=False, width=88,
                                   allow_unicode=True)
    EXAMPLES_PATH.write_text(text, "utf-8", newline="\n")
    print(f"pool {len(payload['pool'])}, excluded {len(payload['excluded'])} "
          f"-> {EXAMPLES_PATH.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
