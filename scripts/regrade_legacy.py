"""Re-grade the stored legacy runs with the strict v2 grader. Offline.

    python scripts/regrade_legacy.py

No LLM calls: each arm's stored final SQL is re-executed against the
read-only database and graded against gold derived mechanically from the
frozen reference queries (rules in eval/legacy_strict_specs.yaml). Which/top-N
questions use rule 3 (exactly N rows, or a result ranked by a metric whose
first N rows match). Its answer-naming half cannot be applied: the legacy
baseline wrote no prose and the original run stored none, so it is reported
for the agent's stored answers only and never changes a grade.

Every failure is classified: "shape only" (the gold rows are present, the
shape is wrong) or "wrong values" (they are not). For which-questions the
supporting metric is also compared with the reference query's metric column.
Writes eval/results/legacy_v1_strict.json and a .md summary next to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.db.engine import execute_select, read_only_engine  # noqa: E402
from evalkit.answer_checks import check_answer  # noqa: E402
from evalkit.grader import Gold, contains_gold, grade, grade_top_n  # noqa: E402
from evalkit.scoring import item_name_checks  # noqa: E402
from evalkit.stats import mcnemar_exact, paired_counts, wilson_interval  # noqa: E402

SPECS = ROOT / "eval" / "legacy_strict_specs.yaml"
OUT_JSON = ROOT / "eval" / "results" / "legacy_v1_strict.json"
OUT_MD = OUT_JSON.with_suffix(".md")
MAX_ROWS = 500
ARMS = ("agent", "baseline")


def derive_gold(reference_rows: list[list[Any]], spec: dict[str, Any]) -> Gold:
    """Apply the mechanical legacy rule: take top_n rows, keep `columns`."""
    rows = reference_rows[: spec["top_n"]] if "top_n" in spec else reference_rows
    cols = spec.get("columns")
    picked = [tuple(r[c] for c in cols) if cols else tuple(r) for r in rows]
    return Gold(rows=tuple(picked))


def classify(passed: bool, gold_present: bool) -> str:
    if passed:
        return "pass"
    return "shape only" if gold_present else "wrong values"


def _absolute(rows) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(abs(c) if isinstance(c, int | float) and not isinstance(c, bool)
              else c for c in row)
        for row in rows
    )


def metric_status(reference: Gold, rows: list[list[Any]], n_columns: int) -> str:
    """Does the result also carry the reference's supporting values?

    "opposite sign" covers change-vs-decline conventions; "not returned"
    means the result has fewer columns than the reference (label only).
    """
    if contains_gold(reference, rows):
        return "yes"
    if contains_gold(Gold(rows=_absolute(reference.rows)), _absolute(rows)):
        return "yes (opposite sign)"
    if n_columns < len(reference.rows[0]):
        return "not returned"
    return "no"


def grade_item(q, spec, item, ref, engine) -> dict[str, Any]:
    gold = derive_gold(ref.rows, spec)
    reference = derive_gold(ref.rows, {k: v for k, v in spec.items() if k != "columns"})
    got = execute_select(engine, item["sql"], max_rows=MAX_ROWS)
    if not got.ok:
        verdict_passed, reason, mode = False, f"SQL error: {got.error_message}", ""
        gold_present, metric = False, "no result"
    else:
        top_n = spec.get("top_n")
        verdict = (
            grade_top_n(gold, got.rows, top_n=top_n, sql=item["sql"],
                        columns=got.columns)
            if top_n else grade(gold, got.rows)
        )
        verdict_passed, reason, mode = verdict.passed, verdict.reason, verdict.mode
        gold_present = contains_gold(gold, got.rows)
        metric = metric_status(reference, got.rows, len(got.columns))

    answer = item.get("answer") or ""
    names_item = (
        check_answer(answer, item_name_checks(gold.rows)).passed
        if answer and "top_n" in spec else None
    )
    return {
        "id": q["id"],
        "arm": item["arm"],
        "question": q["question"],
        "legacy_check": q["check"],
        "legacy_pass": item["success"],
        "strict_pass": verdict_passed,
        "mode": mode,
        "outcome": classify(verdict_passed, gold_present),
        "first_failing_check": "" if verdict_passed else reason,
        "gold_values_present": gold_present,
        "supporting_metric_vs_reference": metric,
        "answer_names_item": names_item,
        "gold_rows": [list(r) for r in gold.rows],
        "result_columns": got.columns if got.ok else [],
        "result_preview": got.rows[:6] if got.ok else [],
        "sql": item["sql"],
    }


def regrade_set(block: dict[str, Any], engine) -> dict[str, Any]:
    questions = {
        q["id"]: q
        for q in yaml.safe_load((ROOT / block["questions"]).read_text("utf-8"))
    }
    run = json.loads((ROOT / block["source"]).read_text("utf-8"))
    stored = {(i["id"], i["arm"]): i for i in run["items"]}

    items, excluded = [], []
    for qid, spec in block["specs"].items():
        if "skip" in spec:
            excluded.append({"id": qid, "reason": spec["skip"]})
            continue
        q = questions[qid]
        ref = execute_select(engine, q["reference_sql"], max_rows=MAX_ROWS)
        if not ref.ok:
            raise RuntimeError(f"reference SQL failed for {qid}: {ref.error_message}")
        items += [grade_item(q, spec, stored[(qid, arm)], ref, engine) for arm in ARMS]

    summary = {}
    for arm in ARMS:
        mine = [i for i in items if i["arm"] == arm]
        k, n = sum(i["strict_pass"] for i in mine), len(mine)
        lo, hi = wilson_interval(k, n)
        summary[arm] = {
            "strict": f"{k}/{n}",
            "strict_accuracy": round(k / n, 4) if n else None,
            "wilson95": [round(lo, 4), round(hi, 4)],
            "passes_ranked_prefix": sum(i["mode"] == "ranked_prefix" for i in mine),
            "fail_shape_only": sum(i["outcome"] == "shape only" for i in mine),
            "fail_wrong_values": sum(i["outcome"] == "wrong values" for i in mine),
            "legacy_lenient_same_questions":
                f"{sum(bool(i['legacy_pass']) for i in mine)}/{n}",
        }
    by_arm = {
        arm: {i["id"]: i["strict_pass"] for i in items if i["arm"] == arm}
        for arm in ARMS
    }
    table = paired_counts(by_arm["agent"], by_arm["baseline"])
    return {
        "source_run": block["source"],
        "model": run.get("model"),
        "summary": summary,
        "paired_agent_vs_baseline": {
            **table,
            "mcnemar_exact_p": mcnemar_exact(table["only_a"], table["only_b"]),
        },
        "excluded": excluded,
        "items": items,
    }


def _yes_no(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Legacy v1 runs, strict re-grade (protocol v2, rule 3)",
        "",
        "Generated by `scripts/regrade_legacy.py`; no LLM calls. Stored SQL was",
        "re-executed and graded with `evalkit.grader` against gold derived from",
        "the frozen reference queries (`eval/legacy_strict_specs.yaml`).",
        "Which-questions accept exactly N rows or a metric-ranked result whose",
        "first N rows match; the answer-naming half of rule 3 is not applied",
        "(no baseline prose exists). 'gold values' = the gold rows appear in the",
        "result; 'metric vs reference' = whether the result also carries the",
        "reference query's supporting values (not graded; diagnostic only).",
        "",
    ]
    for name, block in payload["sets"].items():
        lines += [f"## {name} (`{block['source_run']}`, {block['model']})", "",
                  "| arm | strict | Wilson 95% | ranked-prefix passes | "
                  "shape-only fails | wrong-value fails | legacy (lenient) |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for arm, s in block["summary"].items():
            lo, hi = s["wilson95"]
            lines.append(
                f"| {arm} | {s['strict']} | {lo:.2f}-{hi:.2f} | "
                f"{s['passes_ranked_prefix']} | {s['fail_shape_only']} | "
                f"{s['fail_wrong_values']} | {s['legacy_lenient_same_questions']} |"
            )
        p = block["paired_agent_vs_baseline"]
        lines += ["", f"Paired: only agent {p['only_a']}, only baseline {p['only_b']}, "
                  f"both {p['both']}, neither {p['neither']}; "
                  f"exact McNemar p = {p['mcnemar_exact_p']:.3f}.", "",
                  "| id | arm | outcome | gold values | metric vs reference | "
                  "first failing check / mode |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for i in block["items"]:
            check = i["first_failing_check"] or i["mode"]
            lines.append(
                f"| {i['id']} | {i['arm']} | {i['outcome']} | "
                f"{_yes_no(i['gold_values_present'])} | "
                f"{i['supporting_metric_vs_reference']} | {check[:90]} |"
            )
        lines += ["", "Excluded: " + "; ".join(
            f"{e['id']} ({e['reason']})" for e in block["excluded"]) or "none", ""]
    return "\n".join(lines)


def main() -> int:
    specs = yaml.safe_load(SPECS.read_text("utf-8"))
    engine = read_only_engine(Settings().database_path)
    payload = {
        "grader": "evalkit.grader, protocol v2 default tolerance, rule 3 for top-N",
        "sets": {name: regrade_set(block, engine) for name, block in specs.items()},
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, default=str) + "\n"
    OUT_JSON.write_text(text, "utf-8", newline="\n")
    OUT_MD.write_text(to_markdown(payload), "utf-8", newline="\n")
    for name, block in payload["sets"].items():
        print(name, json.dumps(block["summary"]), block["paired_agent_vs_baseline"])
    print(f"Wrote {OUT_JSON.relative_to(ROOT)} and {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
