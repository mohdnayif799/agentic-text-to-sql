"""Run chosen questions of a set through one architecture, live, as a check of a fix.

    python scripts/run_selected.py --set holdout_agent_v2 --ids hv08 hv04 --arch full \\
        --run NAME --cache-dir eval/runs/NAME-cache

Graded by the frozen grader, but it is a check of what a fix does, never a score for the
set: the fix was written after reading these failures. Results and traces go to
eval/runs/<run>/. The cache folder should be new, so every call is live.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.llm import build_client  # noqa: E402
from evalkit.gold import load_gold  # noqa: E402
from evalrun.runner import RunConfig, Runner, _git_commit  # noqa: E402
from evalrun.sets import ALL_GOLD_FILES, ALL_SET_FILES, load_questions  # noqa: E402


def chosen(set_name: str, ids: list[str]) -> list[dict]:
    questions = {q["id"]: q for q in load_questions(set_name)}
    unknown = [i for i in ids if i not in questions]
    if unknown:
        raise ValueError(f"not in {set_name}: {unknown}")
    return [questions[i] for i in ids]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=sorted(ALL_SET_FILES))
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--arch", default="full")
    ap.add_argument("--run", required=True)
    ap.add_argument("--cache-dir", required=True)
    args = ap.parse_args()
    questions = chosen(args.set, args.ids)
    cfg = replace(RunConfig.load(), cache_dir=ROOT / args.cache_dir)
    runner = Runner(cfg, live_factory=build_client)
    gold = load_gold(ALL_GOLD_FILES[args.set])["gold"]
    run_dir = ROOT / "eval" / "runs" / args.run
    items = []
    for q in questions:
        item = runner.run_one(q, args.arch, gold[q["id"]], run_dir / "traces")
        items.append(item)
        trace = json.loads((run_dir / item["trace"]).read_text("utf-8"))
        print(f"{q['id']} {args.arch}: {'PASS' if item['primary'] else 'FAIL'}, "
              f"path {item['path']}, tools {item['tools']}")
        print(f"  check: {item['first_failing_check'] or '-'}")
        print(f"  answer: {trace['answer']}")
    payload = {"note": "check of a fix: not a score for the set", "set": args.set,
               "model": cfg.model, "code_commit": _git_commit(), "items": items,
               "retries": runner.retries}
    text = json.dumps(payload, indent=1, default=str) + "\n"
    (run_dir / "results.json").write_text(text, "utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
