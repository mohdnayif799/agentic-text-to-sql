"""Build the gold result files for eval protocol v2. Offline, no LLM.

    python scripts/build_gold.py            # write eval/gold/<set>.json
    python scripts/build_gold.py --check    # rebuild, compare, exit 1 on drift

Every gold answer is produced by executing hand-written SQL (in the question
YAML) or one of the pandas functions below against the read-only database.
Each file records the YAML's SHA-256 and the database content fingerprint,
so a changed question file or regenerated data is detectable.
"""

from __future__ import annotations

import argparse
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
from evalkit.gold import (  # noqa: E402
    connect_read_only,
    database_fingerprint,
    file_sha256,
)
from evalkit.questions import (  # noqa: E402
    GOLD_FILES,
    RESULT_TYPES,
    SET_FILES,
    load_set,
    set_problems,
)

Table = tuple[list[str], list[list[Any]]]

REVENUE = "oi.quantity * oi.unit_price * (1 - oi.discount)"

MONTHLY_REVENUE_SQL = f"""
    SELECT substr(o.order_date, 1, 7) AS month, SUM({REVENUE}) AS revenue
    FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = 'completed'
      AND o.order_date BETWEEN '2024-01-01' AND '2025-12-31'
    GROUP BY month ORDER BY month
"""


def _monthly_revenue(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql(MONTHLY_REVENUE_SQL, conn)
    if len(frame) != 24:
        raise RuntimeError(f"expected 24 months of revenue, got {len(frame)}")
    return frame


def corr_monthly_spend_vs_revenue(conn: sqlite3.Connection) -> Table:
    spend = pd.read_sql(
        "SELECT month, SUM(amount) AS spend FROM marketing_spend "
        "WHERE month BETWEEN '2024-01' AND '2025-12' GROUP BY month", conn
    )
    both = _monthly_revenue(conn).merge(spend, on="month", validate="one_to_one")
    r = float(np.corrcoef(both["spend"], both["revenue"])[0, 1])
    return ["pearson_r"], [[r]]


def median_completed_order_value_2025(conn: sqlite3.Connection) -> Table:
    values = pd.read_sql(
        f"""SELECT o.order_id, SUM({REVENUE}) AS order_value
            FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
            WHERE o.status = 'completed'
              AND o.order_date BETWEEN '2025-01-01' AND '2025-12-31'
            GROUP BY o.order_id""", conn
    )["order_value"]
    return ["median_order_value"], [[float(values.median())]]


def _trend(frame: pd.DataFrame) -> tuple[np.ndarray, float, float]:
    x = np.arange(len(frame), dtype=float)
    slope, intercept = np.polyfit(x, frame["revenue"].to_numpy(), 1)
    return x, float(slope), float(intercept)


def months_below_trend_2024_2025(conn: sqlite3.Connection) -> Table:
    frame = _monthly_revenue(conn)
    x, slope, intercept = _trend(frame)
    residuals = frame["revenue"].to_numpy() - (intercept + slope * x)
    picks = {
        ddof: frozenset(frame["month"][residuals / residuals.std(ddof=ddof) < -1.5])
        for ddof in (0, 1, 2)
    }
    if len(set(picks.values())) != 1:
        raise RuntimeError(f"answer depends on SD convention: {picks}")
    residual = dict(zip(frame["month"], residuals, strict=True))
    return ["month", "residual"], [[m, float(residual[m])] for m in sorted(picks[1])]


def trend_forecast_2026_01(conn: sqlite3.Connection) -> Table:
    frame = _monthly_revenue(conn)
    _, slope, intercept = _trend(frame)
    return ["forecast_revenue"], [[intercept + slope * len(frame)]]


PANDAS_GOLD: dict[str, Callable[[sqlite3.Connection], Table]] = {
    f.__name__: f
    for f in (
        corr_monthly_spend_vs_revenue,
        median_completed_order_value_2025,
        months_below_trend_2024_2025,
        trend_forecast_2026_01,
    )
}


def run_gold(conn: sqlite3.Connection, spec: dict[str, Any]) -> Table:
    if "pandas" in spec:
        return PANDAS_GOLD[spec["pandas"]](conn)
    cursor = conn.execute(spec["sql"])
    columns = [d[0] for d in cursor.description]
    return columns, [list(r) for r in cursor.fetchall()]


def _table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows}


#: Names are not unique (two product names and hundreds of customer names are
#: shared), so a gold that identifies an item by name must carry its ID too.
ID_FOR_NAME = {"product_name": "product_id", "customer_name": "customer_id"}


def _graded_table(where: str, spec: dict[str, Any], conn, top_n: int | None):
    columns, rows = run_gold(conn, spec)
    if not rows:
        raise RuntimeError(f"{where}: gold query returned no rows")
    for name, ident in ID_FOR_NAME.items():
        if name in columns and ident not in columns:
            raise RuntimeError(f"{where}: gold has {name} without {ident}")
    if top_n is not None and len(rows) != top_n:
        raise RuntimeError(f"{where}: top_n={top_n} but gold has {len(rows)} rows")
    table = _table(columns, rows)
    return {**table, "top_n": top_n} if top_n else table


def build_entry(conn: sqlite3.Connection, q: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": q["type"]}
    top_n = q.get("top_n")
    if q["type"] in RESULT_TYPES:
        entry.update(_graded_table(q["id"], q["gold"], conn, top_n),
                     ordered=bool(q.get("ordered", False)))
        if q.get("tolerance"):
            entry["tolerance"] = q["tolerance"]
    elif q["type"] == "ambiguous":
        entry["interpretations"] = {
            interp["name"]: _graded_table(
                f"{q['id']}/{interp['name']}", interp["gold"], conn, top_n
            )
            for interp in q["interpretations"]
        }
    if q.get("evidence"):
        entry["evidence"] = {
            name: _table(*run_gold(conn, {"sql": sql}))
            for name, sql in q["evidence"].items()
        }
    return entry


def build_set(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    questions = load_set(name)
    problems = set_problems(name, questions)
    if problems:
        raise RuntimeError("invalid question set:\n  " + "\n  ".join(problems))
    return {
        "set": name,
        "questions_file": SET_FILES[name].relative_to(ROOT).as_posix(),
        "questions_sha256": file_sha256(SET_FILES[name]),
        "db_fingerprint": database_fingerprint(conn),
        "gold": {q["id"]: build_entry(conn, q) for q in questions},
    }


def serialise(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=1, sort_keys=True) + "\n"


def canonical(value: Any, *, ordered: bool = False) -> Any:
    """Form used to compare gold builds across SQLite versions.

    Floats to 9 significant digits (summation order can differ in the last
    bits) and unordered tables as sorted rows (GROUP BY output order is not
    guaranteed).
    """
    if isinstance(value, float):
        return float(f"{value:.9g}")
    if isinstance(value, dict):
        if "rows" in value and "columns" in value:
            rows = [canonical(r) for r in value["rows"]]
            keep = ordered or value.get("ordered", False)
            return {
                **{k: canonical(v) for k, v in value.items() if k != "rows"},
                "rows": rows if keep else sorted(rows, key=repr),
            }
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sets", nargs="*", default=list(SET_FILES))
    args = ap.parse_args()

    conn = connect_read_only(Settings().database_path)
    drift = []
    for name in args.sets:
        payload = build_set(conn, name)
        path = GOLD_FILES[name]
        if args.check:
            stored = json.loads(path.read_text("utf-8")) if path.exists() else None
            if canonical(stored) != canonical(payload):
                drift.append(name)
            continue
        text = serialise(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    if drift:
        print(f"gold drift in: {drift}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
