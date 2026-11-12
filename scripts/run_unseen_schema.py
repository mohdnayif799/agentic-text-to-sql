"""Run A and D on the unseen college schema, as a visitor's upload.

    python scripts/run_unseen_schema.py --run college1
    python scripts/run_unseen_schema.py --run college1 --offline   # cache only

What it does, in the order the app does it:
1. uploads the four CSV files in eval/college/ through the real ingest path
   (convert to SQLite, infer joins) into a temporary store;
2. drafts the table and column descriptions with the configured model, as
   the app does after an upload (one call, cached like every other);
3. answers each question in eval/college_questions.yaml with A (baseline)
   and D (full) on that dataset's tools and context;
4. grades each final result with the frozen strict grader against the rows
   of the question's gold SQL on the same converted file.

Model, temperature, budgets and "today" come from eval/run_config.yaml, so
the setup matches the frozen 50 except the database. Every response is
cached under eval/runs/<run>-cache; rerunning resumes for free. Results go to
eval/results/college/<run>.json and a short report beside it. This is not
part of the frozen evaluation and changes none of its files.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.datasets.describe import draft_notes  # noqa: E402
from agentcrew.datasets.ingest import ingest  # noqa: E402
from agentcrew.datasets.source import close_upload, upload_source  # noqa: E402
from agentcrew.datasets.store import DatasetStore  # noqa: E402
from agentcrew.llm import build_client  # noqa: E402
from evalkit.stats import wilson_interval  # noqa: E402
from evalrun.backoff import DailyLimitReached, ProviderUnavailable  # noqa: E402
from evalrun.cache import CacheMiss  # noqa: E402
from evalrun.runner import RunConfig, Runner, _git_commit  # noqa: E402

DATA = ROOT / "eval" / "college"
QUESTIONS = ROOT / "eval" / "college_questions.yaml"
OUT = ROOT / "eval" / "results" / "college"
ARCHS = ("baseline", "full")


def gold_for(path: Path, questions: list[dict]) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        gold = {}
        for q in questions:
            cursor = conn.execute(q["gold"]["sql"])
            gold[q["id"]] = {
                "columns": [c[0] for c in cursor.description],
                "rows": [list(r) for r in cursor.fetchall()],
                "ordered": bool(q["gold"].get("ordered", False)),
                "type": q["type"],
            }
        return gold
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--offline", action="store_true",
                    help="cached responses only; stop at the first miss")
    args = ap.parse_args()

    cache = ROOT / "eval" / "runs" / f"{args.run}-cache"
    cfg = replace(RunConfig.load(), cache_dir=cache)
    runner = Runner(cfg, live_factory=None if args.offline else build_client)
    settings = runner.settings
    questions = yaml.safe_load(QUESTIONS.read_text("utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        settings = settings.model_copy(update={"upload_dir": Path(tmp)})
        store = DatasetStore(Path(tmp), max_bytes=10**9, ttl_seconds=10**6,
                             sweep=False)
        files = [(p.name, p.read_bytes()) for p in sorted(DATA.glob("*.csv"))]
        dataset = ingest(store, owner="eval", files=files, settings=settings)
        print(f"uploaded {dataset.label}: tables {dataset.tables}; inferred joins: "
              + "; ".join(j.sql() for j in dataset.joins))

        tools = upload_source(dataset, settings).tool_context(settings)
        try:
            notes = draft_notes(runner._client("describe", "college"), tools,
                                max_tokens=settings.max_output_tokens,
                                temperature=settings.temperature)
        except (DailyLimitReached, CacheMiss, ProviderUnavailable) as exc:
            print(f"STOPPED drafting descriptions: {type(exc).__name__}: {exc}")
            return 2
        dataset = store.update(dataset.id, "eval", notes=notes)
        print(f"descriptions: {len(notes.tables)} tables, {len(notes.columns)} columns")
        source = upload_source(dataset, settings)
        runner.tools = source.tool_context(settings)
        runner.builder = source.context_builder(runner.tools, cfg.today, settings)
        gold = gold_for(dataset.path, questions)

        trace_dir = ROOT / "eval" / "runs" / args.run / "traces"
        items, stopped = [], None
        for q in questions:
            for arch in ARCHS:
                try:
                    item = runner.run_one(q, arch, gold[q["id"]], trace_dir)
                except (DailyLimitReached, CacheMiss, ProviderUnavailable) as exc:
                    stopped = f"{q['id']}/{arch}: {type(exc).__name__}"
                    break
                items.append(item)
                print(f"{q['id']} {arch:8} {'PASS' if item['primary'] else 'FAIL'}"
                      f"  path={item['path']} calls={item['llm_calls']}"
                      f"  {item['first_failing_check'] or ''}")
            if stopped:
                break
        close_upload(dataset)

    summary = {}
    for arch in ARCHS:
        mine = [i for i in items if i["arch"] == arch]
        passed = sum(bool(i["primary"]) for i in mine)
        lo, hi = wilson_interval(passed, len(mine)) if mine else (0.0, 1.0)
        summary[arch] = {"passed": passed, "of": len(mine),
                         "wilson95": [round(lo, 3), round(hi, 3)],
                         "avg_llm_calls": round(sum(i["llm_calls"] for i in mine)
                                                / max(len(mine), 1), 2)}
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": "unseen-schema test, not part of the frozen evaluation",
        "run": args.run, "model": cfg.model, "today": cfg.today.isoformat(),
        "code_commit": _git_commit(), "complete": stopped is None,
        "stopped": stopped, "joins": [j.sql() for j in dataset.joins],
        "descriptions": {"tables": dict(notes.tables),
                         "columns": {f"{t}.{c}": d
                                     for (t, c), d in notes.columns.items()}},
        "summary": summary,
        "items": [{k: i[k] for k in ("id", "arch", "primary", "path", "llm_calls",
                                     "first_failing_check", "tools")
                   if k in i} for i in items],
    }
    (OUT / f"{args.run}.json").write_text(json.dumps(payload, indent=2, default=str),
                                          encoding="utf-8")
    for arch, s in summary.items():
        print(f"{arch:8} {s['passed']}/{s['of']} (Wilson {s['wilson95']}), "
              f"avg calls {s['avg_llm_calls']}")
    print(f"complete={stopped is None}  results: {OUT / (args.run + '.json')}")
    return 0 if stopped is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
