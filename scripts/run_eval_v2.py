"""Run architectures on a protocol-v2 question set.

    python scripts/run_eval_v2.py --set showcase --arch baseline v1 --run NAME
    python scripts/run_eval_v2.py ... --plan-only     # print planned calls, no run
    python scripts/run_eval_v2.py ... --offline       # cache only, no API calls
    python scripts/run_eval_v2.py --set showcase --run NAME --rejudge
                                   # re-judge a run's stored why-answers only
    python scripts/run_eval_v2.py --set dev2 --arch full --run NAME --force-escalation
                                   # development smoke test: D escalates on
                                   # every question (refused on holdout sets;
                                   # results do not count)

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
from evalrun.runner import (  # noqa: E402
    RunConfig,
    Runner,
    check_force_escalation,
    planned_calls,
)
from evalrun.sets import ALL_SET_FILES, load_questions  # noqa: E402


def _print_summary(payload: dict) -> None:
    for arch, s in payload["summary"].items():
        errors = f", ERROR {s['errors']}" if s.get("errors") else ""
        print(f"{arch:9} primary {s['primary']}{errors} (Wilson {s['wilson95']}), "
              f"pending {s['pending']}, not evaluated {s.get('not_evaluated', 0)}, "
              f"avg calls {s['avg_llm_calls']}, "
              f"avg latency {s['avg_latency_s']}s "
              f"(as if live {s.get('avg_latency_live_s', '?')}s), "
              f"by type {s['by_type']}")
    for arch, e in payload.get("escalation", {}).items():
        print(f"{arch:9} escalation vs A: helped {e['helped']}, harmed "
              f"{e['harmed']}, no change {e['no change']}, unknown {e['unknown']}")
    missed = payload.get("missed_escalations")
    if missed:
        print(f"A failed {missed['baseline_failed']}: "
              + "; ".join(f"{arch} escalated {missed[arch]['escalated']}, "
                          f"not escalated {missed[arch]['not_escalated']}, "
                          f"error {missed[arch].get('error', 0)}"
                          for arch in ("router", "full")))
        for row in missed["questions"]:
            print(f"  {row['id']}: C {row['router']}, D {row['full']}")
    for row in payload.get("provider_resumes", []):
        print(f"provider errors: {row['id']}/{row['arch']} on {row['dates']} "
              f"({row['state']})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=sorted(ALL_SET_FILES))
    ap.add_argument("--arch", nargs="+", choices=ARCHS)
    ap.add_argument("--run", required=True, help="run name (folder under eval/runs)")
    ap.add_argument("--offline", action="store_true", help="cache only; no API calls")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--rejudge", action="store_true",
                    help="re-judge the run's stored why-question answers only")
    ap.add_argument("--force-escalation", action="store_true",
                    help="development smoke test: D escalates on every question "
                         "(refused on holdout sets; results do not count)")
    args = ap.parse_args()
    if not args.rejudge and not args.arch:
        ap.error("--arch is required unless --rejudge is given")
    if args.force_escalation:
        try:
            check_force_escalation(args.set, args.arch or [])
        except ValueError as exc:
            ap.error(str(exc))

    cfg = RunConfig.load()
    if args.plan_only:
        plan = planned_calls(load_questions(args.set), args.arch)
        print(f"Planned LLM calls: typical {plan['typical']}, at most {plan['max']} "
              f"{plan['per_arch']} + judge <= {plan['judge_max']}")
        return 0

    runner = Runner(cfg, live_factory=None if args.offline else build_client,
                    force_escalation=args.force_escalation)
    run_dir = ROOT / "eval" / "runs" / args.run
    if args.rejudge:
        _print_summary(runner.rejudge(args.set, run_dir))
        return 0
    payload = runner.run(args.set, args.arch, run_dir)
    _print_summary(payload)
    print(f"complete={payload['complete']}  results: eval/runs/{args.run}/results.json")
    return 0 if payload["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
