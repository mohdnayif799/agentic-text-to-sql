"""Run A and D on the unseen college schema, as a visitor's upload.

    python scripts/run_unseen_schema.py --run college2                 # both copies
    python scripts/run_unseen_schema.py --run college2 --variant messy
    python scripts/run_unseen_schema.py --run college2 --offline       # cache only

For each copy of the data (clean names, messy names; evalrun/college.py) it
does what the app does, in the same order:
1. uploads the copy's CSV files through the real ingest path (convert to
   SQLite, infer joins) into a temporary store;
2. drafts the table and column descriptions with the configured model, as
   the app does after an upload. They are used exactly as drafted: nothing
   is edited by hand, and neither are the inferred joins;
3. answers each question in eval/college_questions.yaml with A (baseline)
   and D (full) on that dataset's tools and context;
4. grades each final result with the frozen strict grader against the rows
   of the question's gold SQL on the same converted file.

It refuses to start unless the questions and data match
eval/college_FREEZE.sha256 (scripts/check_college_gold.py --freeze), so a
run can only be made on gold that was verified and frozen first.

Model, temperature, budgets and "today" come from eval/run_config.yaml, so
the setup matches the frozen 50 except the database. Every response is
cached under eval/runs/<run>-<copy>-cache; rerunning resumes for free.
Results: eval/results/college/<run>.json. This is not part of the frozen
evaluation and changes none of its files.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.datasets.describe import draft_notes  # noqa: E402
from agentcrew.datasets.source import close_upload, upload_source  # noqa: E402
from agentcrew.datasets.store import DatasetStore  # noqa: E402
from agentcrew.llm import build_client  # noqa: E402
from evalkit.stats import wilson_interval  # noqa: E402
from evalrun import college  # noqa: E402
from evalrun.backoff import DailyLimitReached, ProviderUnavailable  # noqa: E402
from evalrun.cache import CacheMiss  # noqa: E402
from evalrun.runner import RunConfig, Runner, _git_commit  # noqa: E402

OUT = ROOT / "eval" / "results" / "college"
ARCHS = ("baseline", "full")
STOPS = (DailyLimitReached, CacheMiss, ProviderUnavailable)
KEPT = ("id", "arch", "primary", "path", "llm_calls", "first_failing_check", "tools",
        "status")


def score(items: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(bool(i["primary"]) for i in items)
    lo, hi = wilson_interval(passed, len(items)) if items else (0.0, 1.0)
    calls = sum(i["llm_calls"] for i in items) / max(len(items), 1)
    return {"passed": passed, "of": len(items),
            "wilson95": [round(lo, 3), round(hi, 3)], "avg_llm_calls": round(calls, 2)}


def summarise(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Per architecture: all questions, the single-query ones (co) and the
    multi-step ones (cm)."""
    out = {}
    for arch in ARCHS:
        mine = [i for i in items if i["arch"] == arch]
        hard = [i for i in mine if i["id"].startswith(college.HARD_PREFIX)]
        easy = [i for i in mine if not i["id"].startswith(college.HARD_PREFIX)]
        out[arch] = {"all": score(mine), "single_query": score(easy),
                     "multi_step": score(hard)}
    return out


def run_variant(variant: str, run: str, offline: bool,
                questions: list[dict[str, Any]]) -> dict[str, Any]:
    cache = ROOT / "eval" / "runs" / f"{run}-{variant}-cache"
    cfg = replace(RunConfig.load(), cache_dir=cache)
    runner = Runner(cfg, live_factory=None if offline else build_client)
    stopped = None
    items: list[dict[str, Any]] = []
    # ignore_cleanup_errors: on Windows a converted file can still be open.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = runner.settings.model_copy(update={"upload_dir": Path(tmp)})
        store = DatasetStore(Path(tmp), max_bytes=10**9, ttl_seconds=10**6,
                             sweep=False)
        dataset = college.upload(variant, store, settings)
        joins = [j.sql() for j in dataset.joins]
        print(f"[{variant}] uploaded {dataset.label}: tables {dataset.tables}")
        print(f"[{variant}] inferred joins: {'; '.join(joins) or '(none)'}")

        tools = upload_source(dataset, settings).tool_context(settings)
        try:
            notes = draft_notes(runner._client("describe", f"college-{variant}"),
                                tools, max_tokens=settings.max_output_tokens,
                                temperature=settings.temperature)
        except STOPS as exc:
            close_upload(dataset)
            return {"complete": False,
                    "stopped": f"descriptions: {type(exc).__name__}", "items": []}
        dataset = store.update(dataset.id, "eval", notes=notes)
        print(f"[{variant}] descriptions drafted: {len(notes.tables)} tables, "
              f"{len(notes.columns)} columns (used as drafted)")
        source = upload_source(dataset, settings)
        runner.tools = source.tool_context(settings)
        runner.builder = source.context_builder(runner.tools, cfg.today, settings)
        gold = college.gold_for(dataset.path, questions, variant)

        trace_dir = ROOT / "eval" / "runs" / f"{run}-{variant}" / "traces"
        for q in questions:
            for arch in ARCHS:
                try:
                    item = runner.run_one(q, arch, gold[q["id"]], trace_dir)
                except STOPS as exc:
                    stopped = f"{q['id']}/{arch}: {type(exc).__name__}"
                    break
                items.append(item)
                print(f"[{variant}] {q['id']} {arch:8} "
                      f"{'PASS' if item['primary'] else 'FAIL'}  path={item['path']} "
                      f"calls={item['llm_calls']}  {item['first_failing_check'] or ''}")
            if stopped:
                break
        close_upload(dataset)
        gc.collect()
    return {
        "complete": stopped is None, "stopped": stopped, "model": cfg.model,
        "today": cfg.today.isoformat(), "tables": list(dataset.tables),
        "joins": joins,
        "descriptions": {"tables": dict(notes.tables),
                         "rows": dict(notes.grains),
                         "columns": {f"{t}.{c}": d
                                     for (t, c), d in notes.columns.items()}},
        "summary": summarise(items),
        "items": [{k: i[k] for k in KEPT if k in i} for i in items],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--variant", choices=[*college.VARIANTS, "both"], default="both")
    ap.add_argument("--offline", action="store_true",
                    help="cached responses only; stop at the first miss")
    args = ap.parse_args()

    problems = college.manifest_problems()
    if problems:
        print("REFUSED: the questions or data do not match the freeze manifest:")
        for p in problems:
            print(f"  - {p}")
        return 3
    questions = college.load_questions()
    variants = college.VARIANTS if args.variant == "both" else (args.variant,)

    OUT.mkdir(parents=True, exist_ok=True)
    out_file = OUT / f"{args.run}.json"
    payload: dict[str, Any] = (json.loads(out_file.read_text("utf-8"))
                               if out_file.exists() else {})
    payload.update({
        "note": "unseen-schema test, not part of the frozen evaluation",
        "run": args.run, "code_commit": _git_commit(),
        "freeze": college.manifest_text().splitlines(),
        "questions": len(questions),
    })
    payload.setdefault("variants", {})
    for variant in variants:
        payload["variants"][variant] = run_variant(variant, args.run, args.offline,
                                                   questions)
        out_file.write_text(json.dumps(payload, indent=2, default=str),
                            encoding="utf-8")

    complete = all(v["complete"] for v in payload["variants"].values())
    print()
    for variant, result in payload["variants"].items():
        if not result["complete"]:
            print(f"{variant}: STOPPED at {result['stopped']}")
        for arch, s in result.get("summary", {}).items():
            print(f"{variant:6} {arch:8} {s['all']['passed']}/{s['all']['of']}  "
                  f"(single-query {s['single_query']['passed']}/"
                  f"{s['single_query']['of']}, multi-step "
                  f"{s['multi_step']['passed']}/{s['multi_step']['of']}), "
                  f"avg calls {s['all']['avg_llm_calls']}")
    print(f"complete={complete}  results: {out_file}")
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
