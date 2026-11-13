"""The unseen-schema test's files, uploads, gold and freeze manifest.

Shared by scripts/check_college_gold.py (verify and freeze), by
scripts/run_unseen_schema.py (run A and D) and by tests/test_college_set.py.
Not part of the frozen evaluation; lives in evalrun/ because that package is
the home of evaluation plumbing that is not itself frozen.

Two copies of the same rows (scripts/build_college_dataset.py):
    clean   eval/college/        readable names
    messy   eval/college_messy/  cryptic names and coded values
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from agentcrew.datasets.ingest import ingest
from agentcrew.datasets.store import Dataset, DatasetStore

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "eval" / "college_questions.yaml"
FREEZE = ROOT / "eval" / "college_FREEZE.sha256"
DATA = {"clean": ROOT / "eval" / "college", "messy": ROOT / "eval" / "college_messy"}
SQL_KEY = {"clean": "sql", "messy": "messy_sql"}
VARIANTS = tuple(DATA)
HARD_PREFIX = "cm"
"""Ids of the questions that need more than one step."""

#: The messy copy's codes, to compare its answers with the clean copy's.
CODES = {"CSE": "Computer Science", "ECE": "Electronics", "ME": "Mechanical",
         "CE": "Civil"}


def load_questions() -> list[dict[str, Any]]:
    return yaml.safe_load(QUESTIONS.read_text("utf-8"))


def csv_files(variant: str) -> list[Path]:
    return sorted(DATA[variant].glob("*.csv"))


def upload(variant: str, store: DatasetStore, settings: Any) -> Dataset:
    """Upload a copy's CSV files through the app's own ingest path."""
    files = [(p.name, p.read_bytes()) for p in csv_files(variant)]
    return ingest(store, owner="eval", files=files, settings=settings)


def gold_rows(db: Path, question: dict[str, Any], variant: str) -> dict[str, Any]:
    """The gold entry (columns, rows, ordered, type) of one question, from its
    gold SQL run on the converted upload."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cursor = conn.execute(question["gold"][SQL_KEY[variant]])
        entry = {
            "columns": [c[0] for c in cursor.description],
            "rows": [list(r) for r in cursor.fetchall()],
            "ordered": bool(question["gold"].get("ordered", False)),
            "type": question["type"],
        }
    finally:
        conn.close()
    if question["gold"].get("tolerance"):
        entry["tolerance"] = question["gold"]["tolerance"]
    return entry


def gold_for(db: Path, questions: list[dict[str, Any]], variant: str
             ) -> dict[str, dict[str, Any]]:
    return {q["id"]: gold_rows(db, q, variant) for q in questions}


# ---------------------------------------------------------------------------
# Freeze manifest
# ---------------------------------------------------------------------------
def frozen_files() -> list[Path]:
    return [QUESTIONS, *csv_files("clean"), *csv_files("messy")]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_text() -> str:
    """One line per frozen file, in the format of the other FREEZE files
    (`sha256sum -c` reads it)."""
    return "".join(f"{sha256(p)} *{p.relative_to(ROOT).as_posix()}\n"
                   for p in frozen_files())


def write_manifest() -> None:
    FREEZE.write_text(manifest_text(), encoding="utf-8", newline="\n")


def manifest_problems() -> list[str]:
    """Why the files do not match the manifest; empty when they do."""
    if not FREEZE.exists():
        return [f"{FREEZE.name} does not exist: run scripts/check_college_gold.py "
                "--freeze first"]
    want = dict(reversed(line.strip().split(" *", 1))
                for line in FREEZE.read_text("utf-8").splitlines() if line.strip())
    have = {p.relative_to(ROOT).as_posix(): sha256(p) for p in frozen_files()}
    problems = [f"{name} changed since it was frozen"
                for name in want if name in have and have[name] != want[name]]
    problems += [f"{name} is frozen but missing" for name in want if name not in have]
    problems += [f"{name} is not in the manifest" for name in have if name not in want]
    return problems
