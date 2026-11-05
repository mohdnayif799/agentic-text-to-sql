"""Tables for the pre-registered 50-question evaluation. Offline, no LLM calls.

    python scripts/report_primary_50.py --out eval/results/primary50/tables.md

Reads the three run folders (results.json), the safety run and the response caches.
Fixed before the run, as the amendment in eval/PROTOCOL.md describes:

  primary metric  passes out of 50 (33 old + 17 new)
  always split    the old 33 (seen during development) and the new 17 (unseen)
  comparison      D vs A, paired, exact McNemar, with Wilson 95% intervals

A question that was not evaluated counts as not passing and is listed. Earlier results
are read for the regression table only; they never enter the totals.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evalkit.stats import mcnemar_exact, paired_counts, wilson_interval  # noqa: E402

RUNS = {"holdout_agent": "primary50-holdout-agent",
        "holdout_easy": "primary50-holdout-easy",
        "holdout_agent_v2": "primary50-holdout-agent-v2"}
SAFETY_RUN = "primary50-safety"
CACHES = {"A and D on all 50": "primary50-cache",
          "Safety set on D": "primary50-safety-cache"}
OLD_SETS = ("holdout_agent", "holdout_easy")
NEW_SETS = ("holdout_agent_v2",)
#: Earlier runs on the 33, as (label, folder pattern, architecture).
EARLIER = (
    ("Phase 5 A", "phase5/phase5-{}", "baseline"),
    ("Phase 5 D", "phase5/phase5-{}", "full"),
    ("non-primary A", "fixes-nonprimary/fixes-nonprimary-{}", "baseline"),
    ("non-primary D", "fixes-nonprimary/fixes-nonprimary-{}", "full"),
    ("frozen-first D", "fixes-nonprimary/fixes-frozenfirst-{}", "full"),
)
#: https://ai.google.dev/gemini-api/docs/pricing, thinking billed as output.
PRICE_IN, PRICE_OUT = 0.30 / 1e6, 2.50 / 1e6
Items = dict[tuple[str, str], dict[str, Any]]


def load_run(path: Path) -> list[dict[str, Any]]:
    return json.loads((path / "results.json").read_text("utf-8"))["items"]


def load_primary(root: Path) -> tuple[Items, dict[str, list[str]]]:
    """All items by (question, architecture), and the question ids of each set."""
    items: Items = {}
    ids: dict[str, list[str]] = {}
    for name, run in RUNS.items():
        rows = load_run(root / run)
        ids[name] = list(dict.fromkeys(row["id"] for row in rows))
        items.update({(row["id"], row["arch"]): row for row in rows})
    return items, ids


def is_pass(item: dict[str, Any] | None) -> bool:
    return bool(item and item["state"] == "evaluated" and item.get("primary"))


def verdict(item: dict[str, Any] | None) -> str:
    if item is None or item["state"] != "evaluated":
        return "not evaluated"
    if item.get("status") == "ERROR":
        return "ERROR"
    return "pass" if item.get("primary") else "FAIL"


def totals(items: Items, ids: list[str]) -> dict[str, Any]:
    a = {q: is_pass(items.get((q, "baseline"))) for q in ids}
    d = {q: is_pass(items.get((q, "full"))) for q in ids}
    counts = paired_counts(a, d)
    return {"n": len(ids), "a": sum(a.values()), "d": sum(d.values()), **counts,
            "p": mcnemar_exact(counts["only_a"], counts["only_b"])}


def _pct(k: int, n: int) -> str:
    low, high = wilson_interval(k, n)
    return f"{k}/{n} ({100 * low:.0f}-{100 * high:.0f}%)"


def _splits(ids: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    old = [q for name in OLD_SETS for q in ids[name]]
    new = [q for name in NEW_SETS for q in ids[name]]
    return [("All 50 (primary)", old + new),
            ("Old 33 (seen during development)", old),
            ("New 17 (unseen)", new),
            ("Inside the 33: holdout_agent", ids["holdout_agent"]),
            ("Inside the 33: holdout_easy", ids["holdout_easy"])]


def totals_table(items: Items, ids: dict[str, list[str]]) -> list[str]:
    lines = ["| Split | A (Wilson 95%) | D (Wilson 95%) | Both | Neither | Only A "
             "| Only D | Exact McNemar p |", "| --- | --- | --- | --- | --- | --- "
             "| --- | --- |"]
    for label, qids in _splits(ids):
        t = totals(items, qids)
        lines.append(f"| {label} | {_pct(t['a'], t['n'])} | {_pct(t['d'], t['n'])} | "
                     f"{t['both']} | {t['neither']} | {t['only_a']} | {t['only_b']} | "
                     f"{t['p']:.3f} |")
    return lines


def _cell(text: Any) -> str:
    return str(text or "-").replace("|", "/").replace("\n", " ")


def question_table(items: Items, ids: dict[str, list[str]]) -> list[str]:
    lines = ["| Question | Split | Type | A | D | D path | A's first failing check "
             "| D's first failing check |", "| --- | --- | --- | --- | --- | --- "
             "| --- | --- |"]
    for name in (*OLD_SETS, *NEW_SETS):
        split = "new 17" if name in NEW_SETS else "old 33"
        for q in ids[name]:
            a, d = items.get((q, "baseline")), items.get((q, "full"))
            kind = (d or a or {}).get("type", "?")
            lines.append(
                f"| {q} | {split} | {kind} | {verdict(a)} | {verdict(d)} | "
                f"{_cell((d or {}).get('path'))} | "
                f"{_cell((a or {}).get('first_failing_check'))} | "
                f"{_cell((d or {}).get('first_failing_check'))} |")
    return lines


def earlier_results(results: Path, set_name: str) -> dict[str, dict[str, bool]]:
    """{label: {question: passed}} for the earlier runs on one set."""
    found: dict[str, dict[str, bool]] = {}
    slug = set_name.replace("_", "-")
    for label, pattern, arch in EARLIER:
        rows = load_run(results / pattern.format(slug))
        found[label] = {r["id"]: is_pass(r) for r in rows if r["arch"] == arch}
    return found


def regressions(items: Items, ids: dict[str, list[str]],
                results: Path) -> list[dict[str, Any]]:
    """Each old question and architecture that passed earlier and does not pass now,
    or the reverse."""
    rows = []
    for name in OLD_SETS:
        earlier = earlier_results(results, name)
        for q in ids[name]:
            for arch, tag in (("baseline", "A"), ("full", "D")):
                now = is_pass(items.get((q, arch)))
                labels = [label for label in earlier if label.endswith(f" {tag}")]
                before = {label: earlier[label].get(q, False) for label in labels}
                if any(before.values()) and not now:
                    change = "regression"
                elif now and not any(before.values()):
                    change = "gain"
                else:
                    continue
                rows.append({"id": q, "arch": tag, "change": change, "before": before})
    return rows


def regression_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No question changed against every earlier run of its architecture."]
    lines = ["| Question | Arch | Change | Earlier passes |",
             "| --- | --- | --- | --- |"]
    for r in rows:
        passed = [label for label, ok in r["before"].items() if ok]
        lines.append(f"| {r['id']} | {r['arch']} | {r['change']} | "
                     f"{', '.join(passed) or 'none'} |")
    return lines


def safety_table(path: Path) -> list[str]:
    payload = json.loads((path / "results.json").read_text("utf-8"))
    same = payload["db_fingerprint_before"] == payload["db_fingerprint_after"]
    lines = [f"Database unchanged: {'yes' if same else 'NO'}.", "",
             "| Item | Kind | Status | Path | Pass | SQL attempts |",
             "| --- | --- | --- | --- | --- | --- |"]
    for item in payload["items"]:
        lines.append(f"| {item['id']} | {item['kind']} | {item['status']} | "
                     f"{item['path']} | {'pass' if item['passed'] else 'FAIL'} | "
                     f"{len(item['attempts'])} |")
    return lines


def cache_usage(files: Iterable[Path]) -> dict[str, Any]:
    use = {"calls": 0, "input": 0, "output": 0, "thoughts": 0}
    for path in files:
        data = json.loads(path.read_text("utf-8"))
        use["calls"] += 1
        use["input"] += data["input_tokens"]
        use["output"] += data["output_tokens"]
        use["thoughts"] += data.get("thoughts_tokens", 0)
    use["cost"] = (use["input"] * PRICE_IN
                   + (use["output"] + use["thoughts"]) * PRICE_OUT)
    return use


def cost_table(root: Path) -> list[str]:
    lines = ["| Run | Live calls | Input tokens | Output tokens | Thinking tokens "
             "| Cost |", "| --- | --- | --- | --- | --- | --- |"]
    grand = 0.0
    for label, folder in CACHES.items():
        u = cache_usage(sorted((root / folder).rglob("*.json")))
        grand += u["cost"]
        lines.append(f"| {label} | {u['calls']} | {u['input']:,} | {u['output']:,} | "
                     f"{u['thoughts']:,} | ${u['cost']:.3f} |")
    lines.append(f"| **Total** | | | | | **${grand:.3f}** |")
    return lines


def not_evaluated(items: Items) -> list[str]:
    return sorted(f"{q}/{arch}" for (q, arch), i in items.items()
                  if i["state"] != "evaluated" or i.get("status") == "ERROR")


def render(root: Path, results: Path) -> str:
    items, ids = load_primary(root)
    missing = not_evaluated(items)
    return "\n".join([
        "# Primary 50-question evaluation: tables", "",
        "## Totals", "", *totals_table(items, ids), "",
        "Not evaluated or errored: " + (", ".join(missing) or "none") + ".", "",
        "## Per question", "", *question_table(items, ids), "",
        "## Against earlier runs on the 33", "",
        *regression_table(regressions(items, ids, results)), "",
        "## Safety set (D only, not part of the 50)", "",
        *safety_table(root / SAFETY_RUN), "",
        "## Tokens and cost", "", *cost_table(root), "",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=ROOT / "eval" / "runs")
    ap.add_argument("--results", type=Path, default=ROOT / "eval" / "results")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    text = render(args.runs, args.results)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, "utf-8", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
