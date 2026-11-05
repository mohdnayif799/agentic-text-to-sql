"""Tables for the post-fix rerun of the 50 questions. Offline, no LLM calls.

    python scripts/report_postfix_50.py --out eval/results/postfix50/tables.md

NOT a primary result. Three of the fixes it measures were written after reading failures
in these same 50 questions, and all 50 have now been seen. The primary run stays as
published in eval/results/primary50_report.md, and this script only reads it.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evalkit.stats import mcnemar_exact  # noqa: E402

SETS = {"holdout_agent": "holdout-agent", "holdout_easy": "holdout-easy",
        "holdout_agent_v2": "holdout-agent-v2"}
ARCHS = {"baseline": "A", "full": "D"}
CACHES = {"Post-fix rerun, A and D on all 50": "postfix50-cache",
          "hv08 and hv04 checks, run 1": "fixcheck-1-cache",
          "hv08 and hv04 checks, run 2": "fixcheck-2-cache",
          "Safety set on D": "postfix-safety-cache",
          "Dev sets, A and D": "postfix-dev-cache"}


def _report_50():
    spec = importlib.util.spec_from_file_location(
        "report_primary_50", ROOT / "scripts" / "report_primary_50.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R50 = _report_50()


def load(folder: Path, prefix: str) -> dict[str, Any]:
    items: dict[tuple[str, str], dict[str, Any]] = {}
    ids: dict[str, list[str]] = {}
    for name, part in SETS.items():
        rows = R50.load_run(folder / f"{prefix}-{part}")
        ids[name] = list(dict.fromkeys(row["id"] for row in rows))
        items.update({(row["id"], row["arch"]): row for row in rows})
    return {"items": items, "ids": ids}


def changes(primary: dict[str, Any], rerun: dict[str, Any],
            arch: str) -> dict[str, list[str]]:
    """Questions whose verdict moved between the primary run and the rerun."""
    qids = [q for name in SETS for q in primary["ids"][name]]
    was = {q: R50.is_pass(primary["items"].get((q, arch))) for q in qids}
    now = {q: R50.is_pass(rerun["items"].get((q, arch))) for q in qids}
    return {"regressions": [q for q in qids if was[q] and not now[q]],
            "gains": [q for q in qids if now[q] and not was[q]]}


def against_primary_table(primary: dict[str, Any], rerun: dict[str, Any]) -> list[str]:
    lines = ["| Arch | Split | Primary | Post-fix | Regressions | Gains | "
             "Exact McNemar p |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for arch, tag in ARCHS.items():
        moved = changes(primary, rerun, arch)
        for label, qids in R50._splits(primary["ids"])[:3]:
            before = sum(R50.is_pass(primary["items"].get((q, arch))) for q in qids)
            after = sum(R50.is_pass(rerun["items"].get((q, arch))) for q in qids)
            down = [q for q in moved["regressions"] if q in qids]
            up = [q for q in moved["gains"] if q in qids]
            lines.append(f"| {tag} | {label.split(' (')[0]} | {before}/{len(qids)} | "
                         f"{after}/{len(qids)} | {', '.join(down) or '-'} | "
                         f"{', '.join(up) or '-'} | "
                         f"{mcnemar_exact(len(down), len(up)):.3f} |")
    return lines


def question_table(primary: dict[str, Any], rerun: dict[str, Any]) -> list[str]:
    lines = ["| Question | Split | Type | A primary | A post-fix | D primary | "
             "D post-fix | D path | D's first failing check |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name in SETS:
        split = "new 17" if name == "holdout_agent_v2" else "old 33"
        for q in primary["ids"][name]:
            d = rerun["items"].get((q, "full")) or {}
            lines.append(
                f"| {q} | {split} | {d.get('type', '?')} | "
                f"{R50.verdict(primary['items'].get((q, 'baseline')))} | "
                f"{R50.verdict(rerun['items'].get((q, 'baseline')))} | "
                f"{R50.verdict(primary['items'].get((q, 'full')))} | "
                f"{R50.verdict(d)} | "
                f"{d.get('path', '-')} | "
                f"{str(d.get('first_failing_check') or '-').replace('|', '/')[:140]} |")
    return lines


def cost_table(runs: Path) -> list[str]:
    lines = ["| Run | Live calls | Input tokens | Output tokens | Thinking tokens "
             "| Cost |",
             "| --- | --- | --- | --- | --- | --- |"]
    total = 0.0
    for label, folder in CACHES.items():
        use = R50.cache_usage(sorted((runs / folder).rglob("*.json")))
        total += use["cost"]
        lines.append(f"| {label} | {use['calls']} | {use['input']:,} | "
                     f"{use['output']:,} | {use['thoughts']:,} | ${use['cost']:.3f} |")
    lines.append(f"| **Total** | | | | | **${total:.3f}** |")
    return lines


def speed_lines(rerun: dict[str, Any]) -> list[str]:
    lines = []
    for arch, tag in ARCHS.items():
        rows = [i for (_, a), i in rerun["items"].items() if a == arch]
        latency = [i["latency_live_s"] for i in rows]
        calls = [i["llm_calls"] for i in rows]
        lines.append(f"- {tag}: {sum(calls) / len(calls):.2f} calls per question, "
                     f"{sum(latency) / len(latency):.2f} s on average, slowest "
                     f"{max(latency):.1f} s.")
    return lines


def render(results: Path, runs: Path) -> str:
    primary = load(results / "primary50", "primary50")
    rerun = load(results / "postfix50", "postfix50")
    missing = R50.not_evaluated(rerun["items"])
    return "\n".join([
        "# Post-fix rerun of the 50 questions: tables", "",
        "**Not a primary result.** The primary run stays on record unchanged.", "",
        "## Totals and D vs A, post-fix", "",
        *R50.totals_table(rerun["items"], rerun["ids"]), "",
        "Not evaluated or errored: " + (", ".join(missing) or "none") + ".", "",
        "## Against the primary run", "", *against_primary_table(primary, rerun), "",
        "## Per question", "", *question_table(primary, rerun), "",
        "## Calls and latency, post-fix", "", *speed_lines(rerun), "",
        "## Tokens and cost", "", *cost_table(runs), "",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=ROOT / "eval" / "results")
    ap.add_argument("--runs", type=Path, default=ROOT / "eval" / "runs")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    text = render(args.results, args.runs)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, "utf-8", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
