"""Run architectures on a protocol-v2 question set.

    python scripts/run_eval_v2.py --set showcase --arch baseline v1 --run NAME
    python scripts/run_eval_v2.py ... --plan-only     # print planned calls, no run
    python scripts/run_eval_v2.py ... --offline       # cache only, no API calls

Configuration (provider, model, fixed "today", budgets) comes from
eval/run_config.yaml. Results: eval/runs/<run>/results.json; traces:
eval/runs/<run>/traces/<arch>/<question id>.json. Rerunning the same command
resumes: evaluated pairs are skipped and cached responses cost nothing.
Exit code 0 = complete, 2 = stopped (daily limit or cache miss).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.archs import ARCHS  # noqa: E402
from agentcrew.llm import build_client  # noqa: E402
from evalkit.questions import SET_FILES, load_set  # noqa: E402
from evalrun.runner import RunConfig, Runner, planned_calls  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=sorted(SET_FILES))
    ap.add_argument("--arch", nargs="+", required=True, choices=ARCHS)
    ap.add_argument("--run", required=True, help="run name (folder under eval/runs)")
    ap.add_argument("--offline", action="store_true", help="cache only; no API calls")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()

    cfg = RunConfig.load()
    if args.plan_only:
        plan = planned_calls(load_set(args.set), args.arch)
        print(f"Planned LLM calls: typical {plan['typical']}, at most {plan['max']} "
              f"{plan['per_arch']} + judge <= {plan['judge_max']}")
        return 0

    runner = Runner(cfg, live_factory=None if args.offline else build_client)
    payload = runner.run(args.set, args.arch, ROOT / "eval" / "runs" / args.run)
    for arch, s in payload["summary"].items():
        print(f"{arch:9} primary {s['primary']} (Wilson {s['wilson95']}), "
              f"pending {s['pending']}, avg calls {s['avg_llm_calls']}, "
              f"avg latency {s['avg_latency_s']}s, by type {s['by_type']}")
    print(f"complete={payload['complete']}  results: eval/runs/{args.run}/results.json")
    return 0 if payload["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
