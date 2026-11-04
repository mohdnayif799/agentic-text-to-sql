"""Run the ungraded dev probes (eval/dev_probes.yaml) through architecture D.

    python scripts/run_dev_probes.py --run NAME --cache-dir eval/runs/NAME-cache

Probes try fixes on the real model before a holdout run. They are read by hand:
there is no gold, no grader and no score, and they are never part of a holdout.
Each probe's trace goes to eval/runs/<run>/<probe id>.json, with API keys redacted.
Uses the same configuration and response caching as evaluated runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.archs import run_architecture  # noqa: E402
from agentcrew.llm import build_client  # noqa: E402
from evalrun.redact import redact  # noqa: E402
from evalrun.runner import RunConfig, Runner  # noqa: E402

PROBES = ROOT / "eval" / "dev_probes.yaml"
PROBE_PREFIX = "probe-"
TABLE_ROWS = 40


def load_probes(path: Path = PROBES) -> list[dict[str, Any]]:
    probes = yaml.safe_load(path.read_text("utf-8"))
    for probe in probes:
        if not probe["id"].startswith(PROBE_PREFIX):
            raise ValueError(f"{probe['id']} is not marked as a dev probe")
    return probes


def run_probe(runner: Runner, probe: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """D on one probe, with the same client split as an evaluated run."""
    qid = probe["id"]
    clients = {"full": runner._client("full", qid),  # noqa: SLF001
               "baseline (step 0)": runner._client("baseline", qid),  # noqa: SLF001
               "router (shared with C)": runner._client("router", qid)}  # noqa: SLF001
    outcome = run_architecture(
        "full", probe["question"], settings=runner.settings, llm=clients["full"],
        tools=runner.tools, context_builder=runner.builder,
        step0_llm=clients["baseline (step 0)"],
        shared_llm=clients["router (shared with C)"],
    )
    selected = outcome.selected
    trace = redact({
        "note": "dev probe: ungraded, never part of any holdout",
        "id": qid, "question": probe["question"], "expect": probe.get("expect"),
        "status": outcome.status, "path": outcome.path, "answer": outcome.answer,
        "steps": list(outcome.steps),
        "attempts": [asdict(a) for a in outcome.attempts],
        "selected": None if selected is None else {
            "sql": selected.sql, "columns": selected.columns,
            "rows": selected.rows[:TABLE_ROWS]},
        "llm_calls": [{**asdict(r), "cache_namespace": name}
                      for name, c in clients.items() for r in c.records],
    }, runner.secrets)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(trace, indent=1, default=str) + "\n"
    (out_dir / f"{qid}.json").write_text(text, "utf-8", newline="\n")
    return trace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run name (folder under eval/runs)")
    ap.add_argument("--cache-dir", help="response cache folder")
    args = ap.parse_args()
    cfg = RunConfig.load()
    if args.cache_dir:
        cfg = replace(cfg, cache_dir=ROOT / args.cache_dir)
    runner = Runner(cfg, live_factory=build_client)
    for probe in load_probes():
        trace = run_probe(runner, probe, ROOT / "eval" / "runs" / args.run)
        print(f"{trace['id']}: {trace['status']}, {trace['path']}, "
              f"tools {[s.get('tools') for s in trace['steps']]}")
        print(f"  {trace['answer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
