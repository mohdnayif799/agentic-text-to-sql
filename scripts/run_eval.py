"""Evaluation harness: AgentCrew vs a single-agent baseline.

Usage
-----
    python scripts/run_eval.py                     # both arms, all questions
    python scripts/run_eval.py --arm agent         # one arm only
    python scripts/run_eval.py --ids q04 q11       # a subset
    python scripts/run_eval.py --provider fake     # offline smoke run

Grading is execution-based. For each question the harness runs a *reference
SQL query* against the same database and compares the agent's returned rows to
it. Nothing is graded by an LLM, and no expected numbers are hardcoded, so the
scores stay valid if the seed data is regenerated.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.baseline import run_baseline  # noqa: E402
from agentcrew.config import Settings  # noqa: E402
from agentcrew.db.engine import execute_select, read_only_engine  # noqa: E402
from agentcrew.graph.build import build_deps, run_question  # noqa: E402
from agentcrew.llm import QuotaExhaustedError, is_quota_error  # noqa: E402
from agentcrew.schemas import ExecutionResult  # noqa: E402
from agentcrew.tracer import Tracer  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def _numbers(result: ExecutionResult) -> list[float]:
    out: list[float] = []
    for row in result.rows:
        for cell in row:
            if isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float)):
                out.append(float(cell))
            else:
                try:
                    out.append(float(str(cell)))
                except (TypeError, ValueError):
                    pass
    return out


def _labels(result: ExecutionResult) -> list[str]:
    out: list[str] = []
    for row in result.rows:
        for cell in row:
            if isinstance(cell, str) and not cell.replace(".", "", 1).isdigit():
                out.append(cell.strip().lower())
    return out


def grade(
    check: str,
    agent: ExecutionResult | None,
    reference: ExecutionResult,
    *,
    tolerance: float = 0.0,
) -> tuple[bool, str]:
    """Compare an agent result against the reference result."""
    if check == "must_not_mutate":
        # Handled by the caller (it re-reads the table afterwards).
        return True, "handled separately"
    if agent is None or not agent.ok:
        return False, "no successful query"
    if not agent.rows:
        return False, "empty result"

    if check == "nonempty":
        return True, f"{agent.row_count} row(s)"

    if check == "scalar":
        expected = _numbers(reference)
        if not expected:
            return False, "reference produced no number"
        target = expected[0]
        got = _numbers(agent)
        if not got:
            return False, "agent produced no number"
        for value in got:
            if target == 0:
                if abs(value) <= max(tolerance, 1e-9):
                    return True, f"matched {target}"
            elif abs(value - target) / abs(target) <= max(tolerance, 1e-9):
                return True, f"matched {target}"
        return False, f"expected {target}, got {got[:5]}"

    if check == "top_label":
        expected = _labels(reference)
        got = _labels(agent)
        if not expected:
            return False, "reference produced no label"
        if not got:
            return False, "agent produced no label"
        return (
            (True, f"top label {expected[0]}")
            if got[0] == expected[0]
            else (False, f"expected top '{expected[0]}', got '{got[0]}'")
        )

    if check == "label_set":
        expected_set = set(_labels(reference))
        got_set = set(_labels(agent))
        return (
            (True, "label sets match")
            if expected_set and expected_set <= got_set
            else (False, f"expected {sorted(expected_set)}, got {sorted(got_set)}")
        )

    return False, f"unknown check '{check}'"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
EVALUATED = "evaluated"
NOT_EVALUATED_QUOTA = "not_evaluated_quota"


@dataclass
class ItemResult:
    """One (question, arm) outcome.

    `state` separates "we measured this" from "we never got to ask". `success`
    is deliberately `None` - not `False` - for anything not evaluated, so an
    unevaluated question cannot be silently averaged in as a failure.
    """

    id: str
    question: str
    difficulty: str
    arm: str
    state: str
    success: bool | None
    detail: str
    sql_ran: bool = False
    attempts: int = 0
    duration_s: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    repairs: int = 0
    status: str = ""
    sql: str = ""


def summarise(items: list[ItemResult], arm: str) -> dict[str, Any]:
    """Aggregate one arm over EVALUATED items only.

    Two denominators, deliberately:
      * `questions_evaluated` - how many actually ran;
      * `questions_scored` / `denominator` - how many were machine-scorable.
    They differ because the ambiguity probe has no reference SQL: it runs, but
    is reported under manual review rather than scored. Counting it as a
    failure would understate accuracy; counting it as a pass would overstate it.

    Computed from the item list rather than accumulated during the run, so a
    resumed run summarises carried-forward and fresh results identically.
    """
    ran = [i for i in items if i.arm == arm and i.state == EVALUATED]
    rows = [i for i in ran if i.success is not None]
    n = len(rows)
    solved = sum(1 for i in rows if i.success)
    executed = sum(1 for i in rows if i.sql_ran)
    durations = [i.duration_s for i in ran]
    return {
        "arm": arm,
        "questions_evaluated": len(ran),
        "questions_scored": n,
        "task_success": f"{solved}/{n}" + (f" ({solved / n:.0%})" if n else ""),
        "sql_execution_success": f"{executed}/{n}",
        "median_latency_s": (
            round(statistics.median(durations), 2) if durations else 0.0
        ),
        "total_llm_calls": sum(i.llm_calls for i in ran),
        "total_tokens": sum(i.input_tokens + i.output_tokens for i in ran),
        "repairs": sum(i.repairs for i in ran),
        "denominator": n,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def load_questions(ids: list[str] | None) -> list[dict[str, Any]]:
    items = yaml.safe_load(QUESTIONS.read_text())
    if ids:
        wanted = set(ids)
        items = [q for q in items if q["id"] in wanted]
    return items


def table_checksum(engine, table: str) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - name from a fixed list
    r = execute_select(engine, sql, max_rows=1)
    return int(r.rows[0][0]) if r.ok and r.rows else -1


def make_offline_client(reference_sql: str):
    """Scripted client for `--provider fake`.

    IMPORTANT: this measures the *harness*, not the model. It replays the
    reference query, so a green run proves the plumbing works - grading,
    comparison, budget accounting, the safety re-check - and proves nothing
    about SQL quality. Real numbers require a real provider.
    """
    from agentcrew.llm import FakeClient

    def responder(system: str, user: str):
        if "planning stage" in system:
            return {
                "intent": "offline self-test",
                "ambiguous": False,
                "clarifying_question": None,
                "steps": ["answer the question"],
            }
        if "SQL authoring stage" in system or "repair stage" in system:
            return {
                "sql": reference_sql,
                "rationale": "reference query (harness self-test)",
                "expected_shape": "reference shape",
            }
        if "verification stage" in system:
            return {"verdict": "pass", "reason": "self-test", "repair_hint": None}
        if "final stage" in system:
            return {
                "answer": "Offline self-test answer.",
                "key_numbers": [],
                "caveats": ["Harness self-test; no model was consulted."],
            }
        if "SQL analyst" in system:  # baseline arm
            return {
                "sql": reference_sql,
                "rationale": "reference query",
                "expected_shape": "reference shape",
            }
        raise AssertionError(f"unrecognised prompt: {system[:120]}")

    return FakeClient(responder=responder)


class QuotaStop(Exception):
    """Internal signal: quota exhausted, unwind and stop cleanly."""

    def __init__(self, where: str, message: str) -> None:
        super().__init__(message)
        self.where = where
        self.message = message


def _not_evaluated(q: dict, arm: str, reason: str) -> ItemResult:
    return ItemResult(
        id=q["id"], question=q["question"], difficulty=q.get("difficulty", "?"),
        arm=arm, state=NOT_EVALUATED_QUOTA, success=None, detail=reason,
    )


def _run_one_arm(q, arm, ref, settings, client_factory) -> ItemResult:
    """Run a single arm. Raises QuotaStop if the provider refuses on quota."""
    offline = client_factory(q) if client_factory else None
    deps = build_deps(
        settings, llm=offline, tracer=Tracer(f"eval-{q['id']}-{arm}", trace_dir=None)
    )
    started = time.perf_counter()

    if arm == "agent":
        try:
            out = run_question(
                q["question"], settings=settings, deps=deps, persist_trace=False
            )
        except QuotaExhaustedError as exc:
            raise QuotaStop(f"{q['id']}/{arm}", str(exc)) from exc
        counters = out.trace["counters"]
        agent_result = out.step_results[-1].result if out.step_results else None
        item = ItemResult(
            id=q["id"], question=q["question"], difficulty=q.get("difficulty", "?"),
            arm=arm, state=EVALUATED, success=False, detail="",
            sql_ran=bool(agent_result and agent_result.ok),
            attempts=out.total_attempts,
            duration_s=round(time.perf_counter() - started, 3),
            llm_calls=counters["llm_calls"],
            input_tokens=counters["input_tokens"],
            output_tokens=counters["output_tokens"],
            repairs=counters.get("repairs", 0),
            status=out.status,
            sql=out.sql_statements[-1] if out.sql_statements else "",
        )
    else:
        b = run_baseline(q["question"], deps, settings)
        # run_baseline catches broadly and keeps only the message, so quota has
        # to be classified from the text here. baseline.py is unchanged.
        if is_quota_error(b.error) or is_quota_error(b.result.error_message):
            raise QuotaStop(f"{q['id']}/{arm}", b.error or b.result.error_message or "")
        agent_result = b.result
        item = ItemResult(
            id=q["id"], question=q["question"], difficulty=q.get("difficulty", "?"),
            arm=arm, state=EVALUATED, success=False, detail="",
            sql_ran=b.result.ok, attempts=b.attempts, duration_s=b.duration_s,
            llm_calls=b.llm_calls, input_tokens=b.input_tokens,
            output_tokens=b.output_tokens,
            status="done" if b.succeeded else "failed", sql=b.sql,
        )

    if ref is not None:
        ok, detail = grade(
            q["check"], agent_result, ref, tolerance=float(q.get("tolerance", 0.0))
        )
        if q["check"] == "must_not_mutate":
            detail = "verified by post-run table checksums"
        item.success, item.detail = ok, detail
    else:
        item.success, item.detail = None, "manual review (no reference SQL)"
    return item


def build_payload(items, arms, questions, complete, stopped_at, stopped_reason,
                  settings, mutated) -> dict[str, Any]:
    evaluated_ids = {
        i.id for i in items
        if i.state == EVALUATED
        and all(any(j.id == i.id and j.arm == a and j.state == EVALUATED
                    for j in items) for a in arms)
    }
    not_eval_ids = sorted({i.id for i in items if i.state == NOT_EVALUATED_QUOTA})
    return {
        "provider": settings.provider,
        "model": settings.resolved_model(),
        "complete": complete,
        "stopped_reason": stopped_reason,
        "stopped_at": stopped_at,
        "coverage": {
            "total": len(questions),
            "evaluated": len(evaluated_ids),
            "not_evaluated_quota": len(not_eval_ids),
            "not_evaluated_ids": not_eval_ids,
        },
        "summaries": {a: summarise(items, a) for a in arms},
        "mutated_tables": mutated,
        "items": [asdict(i) for i in items],
    }


def save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def load_previous(path: Path, arms: list[str]) -> tuple[list[ItemResult], set[str]]:
    """Read a prior results.json for --resume.

    A question is resumable-complete only when EVERY requested arm recorded an
    evaluated result for it - the same pairing rule the live run enforces.
    """
    if not path.exists():
        return [], set()
    try:
        prev = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return [], set()

    fields = {f.name for f in dataclass_fields(ItemResult)}
    items = [ItemResult(**{k: v for k, v in raw.items() if k in fields})
             for raw in prev.get("items", [])]

    done: set[str] = set()
    for item in items:
        if all(
            any(j.id == item.id and j.arm == a and j.state == EVALUATED for j in items)
            for a in arms
        ):
            done.add(item.id)
    keep = [i for i in items if i.id in done and i.state == EVALUATED]
    return keep, done


def run_evaluation(questions, settings, arms, out_path, *, resume=False,
                   client_factory=None, verbose=True) -> dict[str, Any]:
    """Execute the suite, stopping cleanly if the provider runs out of quota."""
    engine = read_only_engine(settings.database_path)
    guarded = ["customers", "orders", "order_items", "products", "regions"]
    before = {t: table_checksum(engine, t) for t in guarded}

    items: list[ItemResult] = []
    already: set[str] = set()
    if resume:
        items, already = load_previous(out_path, arms)
        if verbose and already:
            print(
                f"Resuming: {len(already)} question(s) already evaluated, "
                "skipping.\n"
            )

    manual: list[dict[str, Any]] = []
    quota_hit = False
    stopped_at = None
    stopped_reason = None

    if verbose:
        print(
            f"Running {len(questions)} question(s) x {len(arms)} arm(s) "
            f"[provider={settings.provider} model={settings.resolved_model()}]\n"
        )

    for q in questions:
        if q["id"] in already:
            continue
        if quota_hit:
            items.extend(_not_evaluated(q, a, "quota exhausted earlier in run")
                         for a in arms)
            continue

        ref = None
        if q.get("reference_sql"):
            ref = execute_select(engine, q["reference_sql"],
                                 max_rows=settings.max_result_rows)
            if not ref.ok and verbose:
                print(f"  !! reference SQL failed for {q['id']}: {ref.error_message}")

        # Both arms run together and are committed together: a question counts
        # as evaluated only if every arm finished. A half-finished pair would
        # bias the comparison, so it is discarded and marked not evaluated.
        pair: list[ItemResult] = []
        try:
            for arm in arms:
                pair.append(_run_one_arm(q, arm, ref, settings, client_factory))
        except QuotaStop as stop:
            quota_hit = True
            stopped_at, stopped_reason = q["id"], "quota_exhausted"
            items.extend(
                _not_evaluated(q, a, f"API quota exhausted at {stop.where}")
                for a in arms
            )
            if verbose:
                print(f"\n  QUOTA EXHAUSTED at {stop.where} - stopping cleanly.")
                print(f"  {stop.message[:150]}\n")
            continue

        items.extend(pair)
        if ref is None:
            manual.append({"id": q["id"], "question": q["question"],
                           "arms": {i.arm: i.status for i in pair}})
        if verbose:
            for item in pair:
                flag = "MANUAL" if item.success is None else (
                    "PASS" if item.success else "FAIL")
                print(f"  [{flag:6}] {q['id']:4} {item.arm:8} "
                      f"{q['question'][:50]:52} {item.detail[:38]}")

        # Incremental save after every fully evaluated question, so a crash or
        # a killed process never destroys completed work.
        save(out_path, build_payload(items, arms, questions, not quota_hit,
                                     stopped_at, stopped_reason, settings, []))

    after = {t: table_checksum(engine, t) for t in guarded}
    mutated = [t for t in guarded if before[t] != after[t]]

    payload = build_payload(items, arms, questions, not quota_hit, stopped_at,
                            stopped_reason, settings, mutated)
    payload["manual_review"] = manual
    save(out_path, payload)

    if verbose:
        report(payload, arms)
    assert not mutated, "SAFETY VIOLATION: the database was modified during evaluation"
    return payload


def report(payload: dict[str, Any], arms: list[str]) -> None:
    cov = payload["coverage"]
    print("\n" + "=" * 78)
    if payload["complete"]:
        print(f"COVERAGE: {cov['evaluated']}/{cov['total']} questions evaluated "
              f"- COMPLETE RUN")
    else:
        print(f"COVERAGE: {cov['evaluated']}/{cov['total']} questions evaluated")
        print(f"          {cov['not_evaluated_quota']} NOT EVALUATED - API quota "
              f"exhausted at {payload['stopped_at']}")
        print("          Scores below cover the evaluated questions ONLY.")
        print("          *** THIS IS A PARTIAL EVALUATION ***")
    print("=" * 78)

    for arm in arms:
        s = payload["summaries"][arm]
        print(f"\n  {arm.upper()}")
        for k, v in s.items():
            if k != "arm":
                print(f"    {k:26} {v}")

    if cov["not_evaluated_ids"]:
        print(f"\n  NOT EVALUATED (quota): {', '.join(cov['not_evaluated_ids'])}")
        print("  Re-run with --resume once quota resets to finish these.")
    if payload.get("manual_review"):
        print("\n  MANUAL REVIEW (no machine-checkable ground truth)")
        for m in payload["manual_review"]:
            print(f"    {m['id']:4} {m['arms']}")
    mutated = payload["mutated_tables"] or "none"
    print(f"\n  SAFETY: tables mutated during eval  {mutated}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["agent", "baseline", "both"], default="both")
    ap.add_argument("--ids", nargs="*", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--resume", action="store_true",
                    help="skip questions already fully evaluated in --out")
    ap.add_argument("--out", default=str(ROOT / "eval" / "results.json"))
    args = ap.parse_args()

    overrides: dict[str, Any] = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.model:
        overrides["model"] = args.model
    settings = Settings(**overrides)

    questions = load_questions(args.ids)
    arms = ["agent", "baseline"] if args.arm == "both" else [args.arm]
    factory = (
        (lambda q: make_offline_client(q.get("reference_sql") or "SELECT 1"))
        if settings.provider == "fake"
        else None
    )

    payload = run_evaluation(questions, settings, arms, Path(args.out),
                             resume=args.resume, client_factory=factory)
    print(f"\n  Wrote {args.out}")
    return 0 if payload["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
