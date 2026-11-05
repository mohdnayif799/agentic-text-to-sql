"""Tables for the secondary model experiment. Offline, no LLM calls.

    python scripts/report_model_experiment.py \\
        --out eval/results/model-experiment/tables.md

SECONDARY, not pre-registered, and no replacement for the primary result in
eval/results/primary50_report.md. A and D run the frozen 50 on another model, with the
same agent code as the primary run and the judge left on flash-lite, so only the model
under test changes. The 50 are no longer blind, and the agent's author chose the models.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evalkit.stats import mcnemar_exact  # noqa: E402

LITE = "gemini-3.5-flash-lite"
MODELS = (LITE, "gemini-3.5-flash", "gemini-3.8-flash")
#: USD per 1M input and output tokens, thinking billed as output, read from
#: https://ai.google.dev/gemini-api/docs/pricing on 2026-11-05 (3.8's rate is its
#: price through 2026-12-31). gemini-3.5-flash is callable but not on that page, so its
#: cost is an estimate at the rate of the Flash models that are.
PRICES = {LITE: (0.30, 2.50), "gemini-3.5-flash": (0.75, 3.75),
          "gemini-3.8-flash": (0.75, 3.75)}
ESTIMATED = {"gemini-3.5-flash"}
SETS = {"holdout_agent": "holdout-agent", "holdout_easy": "holdout-easy",
        "holdout_agent_v2": "holdout-agent-v2"}
ARCHS = {"baseline": "A", "full": "D"}


def _report_50():
    spec = importlib.util.spec_from_file_location(
        "report_primary_50", ROOT / "scripts" / "report_primary_50.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R50 = _report_50()


def slug(model: str) -> str:
    return model.replace(".", "-")


def run_folder(results: Path, model: str, set_name: str) -> Path:
    if model == LITE:
        return results / "primary50" / f"primary50-{SETS[set_name]}"
    return results / "model-experiment" / f"modelexp-{slug(model)}-{SETS[set_name]}"


def cache_folder(runs: Path, model: str) -> Path:
    return runs / ("primary50-cache" if model == LITE
                   else f"modelexp-{slug(model)}-cache")


def load(results: Path, model: str) -> dict[str, Any]:
    items: dict[tuple[str, str], dict[str, Any]] = {}
    ids: dict[str, list[str]] = {}
    retries: list[dict[str, str]] = []
    meta: list[dict[str, Any]] = []
    for name in SETS:
        path = run_folder(results, model, name) / "results.json"
        payload = json.loads(path.read_text("utf-8"))
        rows = payload["items"]
        ids[name] = list(dict.fromkeys(row["id"] for row in rows))
        items.update({(row["id"], row["arch"]): row for row in rows})
        retries += payload.get("retries", [])
        meta.append({k: payload.get(k) for k in ("code_commit", "complete", "model",
                                                 "judge_model")})
    return {"items": items, "ids": ids, "retries": retries, "meta": meta}


def scores_table(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["| Model | Split | A (Wilson 95%) | D (Wilson 95%) | Only A | Only D "
             "| D vs A, exact McNemar p |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for model, run in runs.items():
        for label, qids in R50._splits(run["ids"])[:3]:  # noqa: SLF001
            t = R50.totals(run["items"], qids)
            lines.append(
                f"| {model} | {label.split(' (')[0]} | {R50._pct(t['a'], t['n'])} | "  # noqa: SLF001
                f"{R50._pct(t['d'], t['n'])} | {t['only_a']} | {t['only_b']} | "  # noqa: SLF001
                f"{t['p']:.3f} |")
    return lines


def against_lite(runs: dict[str, dict[str, Any]], model: str, arch: str,
                 qids: list[str]) -> dict[str, Any]:
    lite, other = runs[LITE]["items"], runs[model]["items"]
    only_lite = [q for q in qids if R50.is_pass(lite.get((q, arch)))
                 and not R50.is_pass(other.get((q, arch)))]
    only_model = [q for q in qids if R50.is_pass(other.get((q, arch)))
                  and not R50.is_pass(lite.get((q, arch)))]
    return {"only_lite": only_lite, "only_model": only_model,
            "p": mcnemar_exact(len(only_lite), len(only_model))}


def against_lite_table(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["| Model | Arch | Split | Only flash-lite | Only this model | "
             "Exact McNemar p |", "| --- | --- | --- | --- | --- | --- |"]
    for model in runs:
        if model == LITE:
            continue
        for arch, tag in ARCHS.items():
            for label, qids in R50._splits(runs[LITE]["ids"])[:3]:  # noqa: SLF001
                c = against_lite(runs, model, arch, qids)
                lines.append(
                    f"| {model} | {tag} | {label.split(' (')[0]} | "
                    f"{', '.join(c['only_lite']) or '-'} | "
                    f"{', '.join(c['only_model']) or '-'} | {c['p']:.3f} |")
    return lines


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def speed_row(run: dict[str, Any], arch: str) -> dict[str, Any]:
    rows = [i for (_, a), i in run["items"].items() if a == arch]
    done = [i for i in rows if i["state"] == "evaluated"]
    latency = [i["latency_live_s"] for i in done]
    return {
        "n": len(rows), "not_evaluated": len(rows) - len(done),
        "errors": sum(1 for i in done if i.get("status") == "ERROR"),
        "latency": _mean(latency), "slowest": max(latency, default=0.0),
        "calls": _mean([i["llm_calls"] for i in done]),
    }


def speed_table(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["| Model | Arch | Avg latency s per question | Slowest s | Avg calls | "
             "Not evaluated | ERROR |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for model, run in runs.items():
        for arch, tag in ARCHS.items():
            s = speed_row(run, arch)
            lines.append(f"| {model} | {tag} | {s['latency']:.2f} | "
                         f"{s['slowest']:.1f} | {s['calls']:.2f} | "
                         f"{s['not_evaluated']} | {s['errors']} |")
    return lines


def retry_counts(retries: Iterable[dict[str, str]]) -> dict[str, int]:
    counts = {"provider": 0, "rate_limit": 0, "503": 0, "timeout": 0}
    for r in retries:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        message = r.get("message", "").lower()
        counts["503"] += "503" in message or "unavailable" in message
        counts["timeout"] += "timed out" in message or "timeout" in message
    return counts


def priced(model: str, use: dict[str, Any]) -> float:
    rate_in, rate_out = PRICES[model]
    return (use["input"] * rate_in
            + (use["output"] + use["thoughts"]) * rate_out) / 1e6


def cost_row(cache: Path, model: str) -> dict[str, Any]:
    """Usage of the model under test and of the judge, which stays on flash-lite."""
    own = R50.cache_usage(sorted((cache / model).rglob("*.json")))
    judge: dict[str, Any] = {"calls": 0, "input": 0, "output": 0, "thoughts": 0}
    if model != LITE:
        judge = R50.cache_usage(sorted((cache / LITE).rglob("*.json")))
    return {"own": own, "judge": judge, "own_cost": priced(model, own),
            "judge_cost": priced(LITE, judge),
            "total": priced(model, own) + priced(LITE, judge)}


def cost_table(runs: dict[str, dict[str, Any]], runs_dir: Path) -> list[str]:
    lines = ["| Model | Live calls | Input tokens | Output tokens | Thinking tokens | "
             "Provider retries (503s) | Rate-limit retries | Judge calls | Cost |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for model, run in runs.items():
        c = cost_row(cache_folder(runs_dir, model), model)
        u, r = c["own"], retry_counts(run["retries"])
        mark = " (estimate)" if model in ESTIMATED else ""
        recorded = "not recorded" if model == LITE else f"{r['provider']} ({r['503']})"
        limits = "not recorded" if model == LITE else str(r["rate_limit"])
        judge = "in the calls" if model == LITE else str(c["judge"]["calls"])
        lines.append(f"| {model} | {u['calls']} | {u['input']:,} | {u['output']:,} | "
                     f"{u['thoughts']:,} | {recorded} | {limits} | {judge} | "
                     f"${c['total']:.3f}{mark} |")
    return lines


def question_table(runs: dict[str, dict[str, Any]]) -> list[str]:
    head = "".join(f" {m} {t} |" for m in runs for t in ARCHS.values())
    lines = [f"| Question |{head}", "| --- |" + " --- |" * (len(runs) * len(ARCHS))]
    for name in SETS:
        for q in runs[LITE]["ids"][name]:
            cells = [R50.verdict(run["items"].get((q, arch)))
                     for run in runs.values() for arch in ARCHS]
            if name != "holdout_easy" or len(set(cells)) > 1:
                lines.append(f"| {q} | " + " | ".join(cells) + " |")
    return lines


def render(results: Path, runs_dir: Path) -> str:
    runs = {model: load(results, model) for model in MODELS}
    commits = {model: sorted({m["code_commit"] for m in run["meta"]})
               for model, run in runs.items()}
    return "\n".join([
        "# Model experiment: tables (secondary)", "",
        "Code commits: " + "; ".join(f"{m} {', '.join(c)}" for m, c in commits.items())
        + ".", "",
        "## Scores and D vs A within each model", "", *scores_table(runs), "",
        "## Each model against flash-lite, same architecture", "",
        *against_lite_table(runs), "",
        "## Latency, calls and errors", "", *speed_table(runs), "",
        "## Tokens, retries and cost", "", *cost_table(runs, runs_dir), "",
        "## Per question (easy questions shown only where a verdict differs)", "",
        *question_table(runs), "",
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
