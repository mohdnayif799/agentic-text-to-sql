"""Render a protocol-v2 run (eval/runs/<run>/results.json) as a Markdown report.

    python scripts/report_v2.py --run NAME [--out PATH]

Every report has the same parts: per architecture the primary score with its
Wilson interval; the paired comparison of each architecture against A
(discordant counts, exact McNemar p, and the easy-set rule "not worse than
A": questions only A passes <= questions only it passes), on the questions
both have evaluated; per question and architecture pass/fail with the first
failing check, LLM calls (live), latency, direct or escalated path, tools per
step and repair fix/break counts; escalation help/harm against A; and missed
escalations (questions A failed, and whether C and D escalated). Offline, no
LLM calls.

Latency is shown twice: as measured in the run, and "as if live", where
every replayed call counts with the latency of the live call that filled the
cache. Replayed calls cost about nothing, so only the second is comparable
across architectures (C and D replay A's step 0).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evalkit.stats import mcnemar_exact, paired_counts  # noqa: E402
from evalrun.runner import (  # noqa: E402
    RunConfig,
    escalation_effects,
    missed_escalations,
    provider_resumes,
)

ARCH_LABEL = {"baseline": "A", "v1": "B", "router": "C", "full": "D"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_NAMESPACE = {"baseline (step 0)": "baseline", "router (shared with C)": "router"}


def _live_latency(item: dict[str, Any], run_dir: Path, cache_dir: Path,
                  model: str) -> float | None:
    """latency_live_s for runs made before the runner recorded it."""
    if "latency_live_s" in item:
        return item["latency_live_s"]
    if "trace" not in item:
        return None
    trace = json.loads((run_dir / item["trace"]).read_text("utf-8"))
    total = item["latency_s"]
    for record in trace.get("llm_calls", []):
        if not record["cache_hit"]:
            continue
        namespace = record.get("cache_namespace", item["arch"])
        path = (cache_dir / _UNSAFE.sub("_", model)
                / _UNSAFE.sub("_", _NAMESPACE.get(namespace, namespace))
                / item["id"] / f"{record['prompt_hash']}.json")
        if not path.exists():
            return None
        latency = json.loads(path.read_text("utf-8")).get("latency_ms")
        if latency is None:
            return None
        total += latency / 1000
    return round(total, 3)


def paired_against_a(items: list[dict[str, Any]], arch: str) -> dict[str, Any] | None:
    """Paired outcome of `arch` against A on the questions both evaluated."""
    graded = {(i["id"], i["arch"]): bool(i["primary"]) for i in items
              if i["state"] == "evaluated" and i.get("primary") is not None}
    shared = sorted(q for q, a in graded if a == "baseline"
                    and (q, arch) in graded)
    if not shared:
        return None
    counts = paired_counts({q: graded[(q, "baseline")] for q in shared},
                           {q: graded[(q, arch)] for q in shared})
    return {"n": len(shared), **counts,
            "mcnemar_p": mcnemar_exact(counts["only_a"], counts["only_b"]),
            "not_worse_than_a": counts["only_a"] <= counts["only_b"]}


def _tools(steps: list[list[str]]) -> str:
    return " > ".join("+".join(step) for step in steps) or "-"


def _cell(text: Any) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def render(payload: dict[str, Any], run_dir: Path, cache_dir: Path) -> str:
    items = payload["items"]
    archs = payload["archs"]
    lines = [
        f"# Run {run_dir.name}",
        "",
        f"Set `{payload['set']}`; model `{payload['model']}`, temperature "
        f"{payload['temperature']}, today {payload['today']}, max steps "
        f"{payload['max_steps']}, max attempts {payload['max_attempts_per_step']}; "
        f"code `{payload['code_commit']}`; complete: {payload['complete']}.",
        *(["", "**Forced escalation (development smoke test): these results do "
                "not count.**"] if payload.get("force_escalation") else []),
        "",
        "## Summary",
        "",
        "| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) "
        "| by type |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    live: dict[tuple[str, str], float | None] = {
        (i["id"], i["arch"]): _live_latency(i, run_dir, cache_dir, payload["model"])
        for i in items if i["state"] == "evaluated"
    }
    for arch in archs:
        s = payload["summary"][arch]
        mine = [v for (_, a), v in live.items() if a == arch]
        avg_live = (f"{sum(mine) / len(mine):.2f}" if mine and None not in mine
                    else "n/a")
        by_type = ", ".join(f"{t} {v}" for t, v in s["by_type"].items())
        errors = (f" ({s['errors']} ERROR)" if s.get("errors") else "")
        lines.append(
            f"| {ARCH_LABEL[arch]} {arch} | {s['primary']}{errors} | "
            f"{s['wilson95'][0]:.2f}-{s['wilson95'][1]:.2f} | {s['avg_llm_calls']} | "
            f"{s['avg_latency_s']} ({avg_live}) | {by_type} |")

    paired = [(arch, paired_against_a(items, arch)) for arch in archs
              if arch != "baseline"]
    paired = [(arch, p) for arch, p in paired if p]
    if paired:
        lines += [
            "",
            "## Paired comparison against A",
            "",
            "| arch | questions | both pass | neither | only A | only this arch "
            "| exact McNemar p | not worse than A |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {ARCH_LABEL[arch]} {arch} | {p['n']} | {p['both']} | {p['neither']} | "
            f"{p['only_a']} | {p['only_b']} | {p['mcnemar_p']:.3f} | "
            f"{'yes' if p['not_worse_than_a'] else 'no'} |"
            for arch, p in paired
        ]
    lines += [
        "",
        "## Per question",
        "",
        "| question | type | arch | result | first failing check | LLM calls (live) "
        "| latency s (as if live) | path | tools by step | repairs fix/break |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        arch = f"{ARCH_LABEL[item['arch']]} {item['arch']}"
        if item["state"] != "evaluated":
            lines.append(f"| {item['id']} | {item['type']} | {arch} | not evaluated "
                         f"| {_cell(item.get('detail', ''))[:80]} | | | | | |")
            continue
        result = ("ERROR" if item.get("status") == "ERROR"
                  else "PASS" if item["primary"] else "FAIL")
        as_live = live[(item["id"], item["arch"])]
        shown = "n/a" if as_live is None else f"{as_live:.2f}"
        lines.append(
            f"| {item['id']} | {item['type']} | {arch} | {result} | "
            f"{_cell(item['first_failing_check']) or '-'} | "
            f"{item['llm_calls']} ({item['live_calls']}) | "
            f"{item['latency_s']:.2f} ({shown})"
            f" | {item['path']} | {_tools(item.get('tools', []))} | "
            f"{item['repairs']}: {item['repair_fixes']}/{item['repair_breaks']} |")

    escalation = payload.get("escalation") or escalation_effects(items)
    lines += ["", "## Escalation against A (help / harm)", ""]
    if escalation:
        lines += ["| arch | escalated | helped | harmed | no change | unknown "
                  "| questions |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for arch, e in escalation.items():
            questions = ", ".join(f"{q['id']} {q['effect']}" for q in e["questions"])
            lines.append(
                f"| {ARCH_LABEL[arch]} {arch} | {len(e['questions'])} | "
                f"{e['helped']} | {e['harmed']} | {e['no change']} | "
                f"{e['unknown']} | {questions} |")
    else:
        lines.append("No C or D run escalated.")

    missed = payload.get("missed_escalations") or missed_escalations(items)
    lines += ["", "## Missed escalations (questions A failed)", ""]
    if missed["baseline_failed"]:
        lines += [
            f"A failed {missed['baseline_failed']} question(s). "
            + "; ".join(f"{ARCH_LABEL[a]} escalated {missed[a]['escalated']}, "
                        f"did not escalate {missed[a]['not_escalated']}, ERROR "
                        f"{missed[a].get('error', 0)}, not run {missed[a]['not_run']}"
                        for a in ("router", "full")) + ".",
            "",
            "| question | C | D |",
            "| --- | --- | --- |",
        ]
        lines += [f"| {r['id']} | {r['router']} | {r['full']} |"
                  for r in missed["questions"]]
    else:
        lines.append("A failed no question.")

    resumes = payload.get("provider_resumes") or provider_resumes(items)
    lines += ["", "## Provider errors and resumes (pre-run amendment 3)", ""]
    if resumes:
        lines += ["| question | arch | now | provider errors on | last stage |",
                  "| --- | --- | --- | --- | --- |"]
        for r in resumes:
            now = ("not evaluated (resumes later)" if r["state"] != "evaluated"
                   else "resumed: " + ("PASS" if r["primary"] else "FAIL"))
            lines.append(f"| {r['id']} | {ARCH_LABEL[r['arch']]} {r['arch']} | {now} | "
                         f"{', '.join(r['dates'])} | {r['last_stage']} |")
    else:
        lines.append("No question hit a persistent provider error.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run name (folder under eval/runs)")
    ap.add_argument("--out", type=Path, help="write the report here instead of stdout")
    args = ap.parse_args()
    run_dir = ROOT / "eval" / "runs" / args.run
    payload = json.loads((run_dir / "results.json").read_text("utf-8"))
    text = render(payload, run_dir, RunConfig.load().cache_dir)
    if args.out:
        args.out.write_text(text, "utf-8", newline="\n")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
