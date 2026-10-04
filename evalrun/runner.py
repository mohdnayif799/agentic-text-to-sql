"""Run architectures on a question set and score them (eval protocol v2).

Every evaluated run uses eval/run_config.yaml: one provider and model, a
fixed "today", the demo's budgets. Each LLM response is cached on disk, so a
rerun spends no quota. A daily-limit stop saves progress; `--resume`-style
reruns skip pairs already evaluated. Per question and architecture the
runner writes a trace (plan, every SQL attempt with a preview, the selected
result, the answer, each LLM call, latency, the score) with API keys
redacted.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from agentcrew.archs import ArchOutcome, run_architecture
from agentcrew.config import Settings
from agentcrew.context import ContextBuilder
from agentcrew.db.engine import read_only_engine
from agentcrew.llm import LLMClient, LLMError, StructuredOutputError
from agentcrew.tools import ToolContext
from agentcrew.tracer import Tracer
from evalkit.answer_checks import check_answer
from evalkit.gold import connect_read_only, database_fingerprint, load_gold
from evalkit.judge import (
    CheckVerdict,
    build_disqualifying_prompt,
    build_fact_prompt,
    counts_as_yes,
    judge_passes,
    quote_in_answer,
)
from evalkit.questions import ROOT
from evalkit.scoring import result_verdict, score_question
from evalkit.stats import wilson_interval
from evalrun.backoff import DailyLimitReached
from evalrun.cache import CacheMiss, CachingClient
from evalrun.redact import redact, redact_text, secrets_of
from evalrun.sets import ALL_GOLD_FILES, load_questions

RUN_CONFIG = ROOT / "eval" / "run_config.yaml"
RESULT_ROWS = 20

#: (typical, maximum) LLM calls per question, used for the pre-run estimate.
#: v1's maximum follows its budgets at 3 steps x 3 attempts. The router's
#: maximum is step 0 + 2 repairs + decision + 2 follow-ups with 2 repairs each
#: + assembly + answer; its step 0 (and direct answer) are A's cached calls.
CALL_ESTIMATE = {"baseline": (2, 3), "v1": (5, 20), "router": (3, 12), "full": (3, 13)}
#: D's maximum: step 0 + 2 repairs + decision (C's) + 2 more step-0 repairs on a
#: verification signal + 2 decide_next_action + 2 x 2 run_sql repairs is capped
#: by max_steps=3 and max_attempts=3; with assembly and answer it is 13.

#: Architectures whose step 0 is A's first call, served from A's cache.
SHARES_STEP0 = ("router", "full")


def judge_calls_per_answer(q: dict[str, Any]) -> int:
    """One call per required fact plus one for disqualifying claims."""
    return len(q["judge_rubric"]["required_facts"]) + 1 if q["type"] == "why" else 0


@dataclass(frozen=True)
class RunConfig:
    provider: str
    model: str
    temperature: float
    today: date
    max_steps: int
    max_attempts_per_step: int
    cache_dir: Path

    @classmethod
    def load(cls, path: Path = RUN_CONFIG) -> RunConfig:
        raw = yaml.safe_load(path.read_text("utf-8"))
        return cls(
            provider=raw["provider"], model=raw["model"],
            temperature=float(raw["temperature"]), today=raw["today"],
            max_steps=int(raw["max_steps"]),
            max_attempts_per_step=int(raw["max_attempts_per_step"]),
            cache_dir=ROOT / raw["cache_dir"],
        )

    def settings(self, **overrides: Any) -> Settings:
        return Settings(**{
            "provider": self.provider, "model": self.model,
            "temperature": self.temperature, "today": self.today,
            "max_steps": self.max_steps,
            "max_attempts_per_step": self.max_attempts_per_step,
            **overrides,
        })


def planned_calls(questions: list[dict[str, Any]], archs: list[str]) -> dict[str, Any]:
    n = len(questions)
    per_arch = {
        a: {"typical": CALL_ESTIMATE[a][0] * n, "max": CALL_ESTIMATE[a][1] * n}
        for a in archs
    }
    judge = sum(judge_calls_per_answer(q) for q in questions) * len(archs)
    return {
        "per_arch": per_arch,
        "judge_max": judge,
        "typical": sum(v["typical"] for v in per_arch.values()) + judge,
        "max": sum(v["max"] for v in per_arch.values()) + judge,
    }


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=ROOT, capture_output=True, text=True,  # noqa: S607
        check=True,
    ).stdout.strip()


def _git_commit() -> str:
    try:
        head = _git("rev-parse", "--short", "HEAD")
        # Untracked scratch files do not make the code dirty.
        dirty = _git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("-dirty" if dirty else "")


def _result_dict(outcome: ArchOutcome) -> dict[str, Any] | None:
    r = outcome.selected
    if r is None:
        return None
    return {"sql": r.sql, "ok": r.ok, "columns": r.columns,
            "rows": r.rows[:RESULT_ROWS], "row_count": r.row_count}


def _check(client: CachingClient, prompt: tuple[str, str], temperature: float,
           answer: str) -> dict[str, Any]:
    system, user = prompt
    verdict, _ = client.complete_structured(
        system=system, user=user, schema=CheckVerdict,
        max_tokens=512, temperature=temperature,
    )
    return {"verdict": verdict.verdict, "quote": verdict.quote,
            "quote_verified": quote_in_answer(verdict.quote, answer),
            "counts_as_yes": counts_as_yes(verdict, answer),
            "reason": verdict.reason}


def _judge(
    q: dict[str, Any], gold_entry: dict[str, Any], answer: str,
    client: CachingClient, temperature: float,
) -> dict[str, Any]:
    """One strict call per required fact, one for disqualifying claims."""
    rubric, evidence = q["judge_rubric"], gold_entry["evidence"]
    try:
        facts = [
            {"fact": fact, **_check(client, build_fact_prompt(
                q["question"], fact, evidence, answer), temperature, answer)}
            for fact in rubric["required_facts"]
        ]
        disqualifying = _check(client, build_disqualifying_prompt(
            q["question"], rubric["disqualifying"], rubric.get("allowed", []),
            evidence, answer), temperature, answer)
    except StructuredOutputError as exc:
        return {"verdict": None, "reason": f"judge error: {exc}"}
    passed = judge_passes([f["counts_as_yes"] for f in facts],
                          disqualifying["counts_as_yes"])
    missing = [i + 1 for i, f in enumerate(facts) if not f["counts_as_yes"]]
    reason = (
        "all required facts stated; no disqualifying claim" if passed
        else f"required fact(s) not stated: {missing}" if missing
        else "disqualifying claim present"
    )
    return {"verdict": "pass" if passed else "fail", "reason": reason,
            "facts": facts, "disqualifying": disqualifying}


class Runner:
    def __init__(
        self,
        cfg: RunConfig,
        *,
        live_factory: Callable[[Settings], LLMClient] | None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.cfg = cfg
        self.settings = cfg.settings()
        self.secrets = secrets_of(self.settings)
        self._factory = live_factory
        self._live: LLMClient | None = None
        self._sleep = sleep
        self._log = log
        engine = read_only_engine(self.settings.database_path)
        self.tools = ToolContext.create(
            engine, max_rows=self.settings.max_result_rows,
            timeout=self.settings.sql_timeout_seconds,
        )
        self.builder = ContextBuilder.from_database(
            engine, self.tools.catalog, today=cfg.today
        )
        self.db_fingerprint = database_fingerprint(
            connect_read_only(self.settings.database_path)
        )

    def _live_client(self) -> LLMClient:
        if self._live is None:
            self._live = self._factory(self.settings)  # type: ignore[misc]
        return self._live

    def _client(self, arch: str, qid: str) -> CachingClient:
        return CachingClient(
            self._live_client if self._factory else None,
            cache_dir=self.cfg.cache_dir, arch=arch, qid=qid,
            model=self.cfg.model, sleep=self._sleep,
        )

    def run_one(self, q: dict[str, Any], arch: str, gold_entry: dict[str, Any],
                trace_dir: Path) -> dict[str, Any]:
        client = self._client(arch, q["id"])
        # Step 0 of C and D is A's first call: same cache entry as A.
        step0 = self._client("baseline", q["id"]) if arch in SHARES_STEP0 else None
        # D's C-identical calls (step-0 repairs, follow-up decision) are C's.
        shared = self._client("router", q["id"]) if arch == "full" else None
        clients = ([(arch, client)]
                   + ([("baseline (step 0)", step0)] if step0 else [])
                   + ([("router (shared with C)", shared)] if shared else []))
        tracer = Tracer(f"{arch}-{q['id']}", trace_dir=None)
        started = time.perf_counter()
        try:
            outcome = run_architecture(
                arch, q["question"], settings=self.settings, llm=client,
                tools=self.tools, context_builder=self.builder, tracer=tracer,
                step0_llm=step0, shared_llm=shared,
            )
        except LLMError as exc:  # provider error (not quota): a failed run
            message = redact_text(f"{type(exc).__name__}: {exc}", self.secrets)
            outcome = ArchOutcome(arch=arch, question=q["question"],
                                  status="failed", answer="", selected=None,
                                  path="error", stop_reason="error", error=message)
        latency = (time.perf_counter() - started
                   - sum(c.slept_seconds for _, c in clients))
        # As if every call were live: replayed calls (including C's and D's
        # shared step 0 from A's cache) count with their original latency.
        replayed = [r for _, c in clients for r in c.records if r.cache_hit]
        latency_live = latency + sum(r.original_latency_ms or 0.0
                                     for r in replayed) / 1000
        unmeasured = sum(1 for r in replayed if r.original_latency_ms is None)

        ok = outcome.selected is not None and outcome.selected.ok
        selected = outcome.selected if ok else None
        judge = None
        judge_client = None
        named = q["type"] == "why" and check_answer(
            outcome.answer, q["driver_checks"]
        ).passed
        if named:
            judge_client = self._client(f"judge-{arch}", q["id"])
            judge = _judge(q, gold_entry, outcome.answer, judge_client,
                           self.cfg.temperature)
        judge_pass = None if not judge or judge["verdict"] is None else (
            judge["verdict"] == "pass"
        )
        score = score_question(
            q, gold_entry, selected.rows if selected else None, outcome.answer,
            judge_pass, sql=selected.sql if selected else None,
            columns=selected.columns if selected else None,
        )
        repairs = [
            {**_repair_summary(r), "effect": _repair_effect(q, gold_entry, r, outcome)}
            for r in outcome.repairs
        ]

        trace = redact({
            "question_id": q["id"], "type": q["type"], "arch": arch,
            "question": q["question"], "model": self.cfg.model,
            "today": self.cfg.today.isoformat(), "status": outcome.status,
            "path": outcome.path, "stop_reason": outcome.stop_reason,
            "error": outcome.error, "plan": list(outcome.plan),
            "sql_attempts": [asdict(a) for a in outcome.attempts],
            "steps": list(outcome.steps),
            "repairs": repairs,
            "selected_step": outcome.selected_step,
            "selected_result": _result_dict(outcome),
            "answer": outcome.answer, "key_numbers": list(outcome.key_numbers),
            "caveats": list(outcome.caveats), "judge": judge,
            "llm_calls": [{**asdict(r), "cache_namespace": name}
                          for name, c in clients for r in c.records],
            "judge_calls": (
                [asdict(r) for r in judge_client.records] if judge_client else []
            ),
            "latency_s": round(latency, 3),
            "latency_live_s": round(latency_live, 3),
            "latency_unmeasured_calls": unmeasured,
            "score": asdict(score),
        }, self.secrets)
        path = trace_dir / arch / f"{q['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(trace, indent=1, default=str) + "\n", "utf-8",
                        newline="\n")

        return {
            "id": q["id"], "type": q["type"], "arch": arch, "state": "evaluated",
            "primary": score.primary, "answer_ok": score.answer_ok,
            "end_to_end": score.end_to_end,
            "first_failing_check": "" if score.primary else score.detail,
            "detail": score.detail, "status": outcome.status, "path": outcome.path,
            "judge": judge,
            "llm_calls": sum(c.calls for _, c in clients),
            "live_calls": sum(c.live_calls for _, c in clients),
            "cache_hits": sum(c.cache_hits for _, c in clients),
            "judge_calls": judge_client.calls if judge_client else 0,
            "latency_s": round(latency, 3),
            "latency_live_s": round(latency_live, 3),
            "latency_unmeasured_calls": unmeasured,
            "repairs": len(repairs),
            "repair_fixes": sum(1 for r in repairs if r["effect"] == "fix"),
            "repair_breaks": sum(1 for r in repairs if r["effect"] == "break"),
            "tools": [s.get("tools", []) for s in outcome.steps],
            "trace": path.relative_to(trace_dir.parent).as_posix(),
        }

    def rejudge(self, set_name: str, run_dir: Path) -> dict[str, Any]:
        """Re-judge the stored answers of a run's why-questions (judge calls only).

        No architecture is re-run: answers come from the run's traces.
        """
        questions = {q["id"]: q for q in load_questions(set_name)}
        gold = load_gold(ALL_GOLD_FILES[set_name])["gold"]
        results_path = run_dir / "results.json"
        payload = json.loads(results_path.read_text("utf-8"))
        targets = [i for i in payload["items"] if i["state"] == "evaluated"
                   and questions[i["id"]]["type"] == "why"]
        planned = sum(judge_calls_per_answer(questions[i["id"]]) for i in targets)
        self._log(f"Re-judging {len(targets)} answer(s): at most {planned} judge "
                  "call(s); no architecture calls.")
        for item in targets:
            q = questions[item["id"]]
            trace_path = run_dir / item["trace"]
            trace = json.loads(trace_path.read_text("utf-8"))
            judge, client = None, None
            if check_answer(trace["answer"], q["driver_checks"]).passed:
                client = self._client(f"judge-{item['arch']}", q["id"])
                try:
                    judge = _judge(q, gold[q["id"]], trace["answer"], client,
                                   self.cfg.temperature)
                except (DailyLimitReached, CacheMiss) as exc:
                    self._log(f"STOPPED re-judging: {type(exc).__name__}")
                    break
            judge_pass = None if not judge or judge["verdict"] is None else (
                judge["verdict"] == "pass")
            score = score_question(q, gold[q["id"]], None, trace["answer"], judge_pass)
            item.update({
                "primary": score.primary, "answer_ok": score.answer_ok,
                "end_to_end": score.end_to_end, "detail": score.detail,
                "first_failing_check": "" if score.primary else score.detail,
                "judge": judge, "judge_calls": client.calls if client else 0,
            })
            trace.update({"judge": judge, "score": asdict(score),
                          "judge_calls": [asdict(r) for r in client.records]
                          if client else []})
            trace_path.write_text(json.dumps(redact(trace, self.secrets), indent=1,
                                             default=str) + "\n", "utf-8", newline="\n")
            self._log(f"  {q['id']} {item['arch']:9} {_flag(score.primary)}  "
                      f"{judge['reason'] if judge else score.detail}")
        payload["rejudged_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        payload["summary"] = summarise(payload["items"], payload["archs"])
        payload["escalation"] = escalation_effects(payload["items"])
        payload["missed_escalations"] = missed_escalations(payload["items"])
        results_path.write_text(json.dumps(redact(payload, self.secrets), indent=1,
                                           default=str) + "\n", "utf-8", newline="\n")
        return payload

    def run(self, set_name: str, archs: list[str], run_dir: Path) -> dict[str, Any]:
        questions = load_questions(set_name)
        gold_file = load_gold(ALL_GOLD_FILES[set_name])
        if gold_file["db_fingerprint"] != self.db_fingerprint:
            raise RuntimeError("database differs from the one the gold was built on")
        gold = gold_file["gold"]

        results_path = run_dir / "results.json"
        previous = (json.loads(results_path.read_text("utf-8"))
                    if results_path.exists() else {"items": []})
        done = {(i["id"], i["arch"]): i for i in previous["items"]
                if i["state"] == "evaluated"}
        plan = planned_calls(questions, archs)
        self._log(
            f"Planned LLM calls for {len(questions)} question(s) x {archs}: "
            f"typical {plan['typical']}, at most {plan['max']} (cached responses "
            f"and {len(done)} already-evaluated pair(s) cost nothing)."
        )

        meta = {
            "set": set_name, "archs": archs, "provider": self.cfg.provider,
            "model": self.cfg.model, "temperature": self.cfg.temperature,
            "today": self.cfg.today.isoformat(),
            "max_steps": self.cfg.max_steps,
            "max_attempts_per_step": self.cfg.max_attempts_per_step,
            "questions_sha256": gold_file["questions_sha256"],
            "code_commit": _git_commit(),
            "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "planned_calls": plan,
        }
        items: list[dict[str, Any]] = []
        stopped = None
        for q in questions:
            for arch in archs:
                if (q["id"], arch) in done:
                    items.append(done[(q["id"], arch)])
                    continue
                if stopped:
                    items.append(_not_evaluated(q, arch, stopped))
                    continue
                try:
                    item = self.run_one(q, arch, gold[q["id"]], run_dir / "traces")
                except (DailyLimitReached, CacheMiss) as exc:
                    text = f"{type(exc).__name__}: {exc}"
                    stopped = redact_text(text, self.secrets)[:300]
                    self._log(f"STOPPED at {q['id']}/{arch}: {stopped}")
                    items.append(_not_evaluated(q, arch, stopped))
                    continue
                items.append(item)
                self._log(
                    f"  {q['id']:5} {arch:9} {_flag(item['primary']):7} "
                    f"calls={item['llm_calls']} live={item['live_calls']} "
                    f"{item['latency_s']:6.1f}s  {item['first_failing_check'][:60]}"
                )
                self._save(results_path, meta, items, None, questions, archs)
        return self._save(results_path, meta, items, stopped, questions, archs)

    def _save(self, path, meta, items, stopped, questions, archs) -> dict[str, Any]:
        payload = {
            **meta,
            "finished_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "complete": stopped is None and len(items) == len(questions) * len(archs),
            "stopped_reason": stopped,
            "summary": summarise(items, archs),
            "escalation": escalation_effects(items),
            "missed_escalations": missed_escalations(items),
            "items": items,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(redact(payload, self.secrets), indent=1,
                                   default=str) + "\n", "utf-8", newline="\n")
        return payload


def _flag(primary: bool | None) -> str:
    return "PEND" if primary is None else ("PASS" if primary else "FAIL")


def _repair_summary(r: Any) -> dict[str, Any]:
    def brief(result: Any) -> dict[str, Any] | None:
        if result is None:
            return None
        return {"sql": result.sql, "ok": result.ok, "rows": result.row_count,
                "error": result.error_message}

    return {"step": r.step, "signal": r.signal, "detail": r.detail,
            "accepted": r.accepted, "attempts": r.attempts,
            "before": brief(r.before), "after": brief(r.after)}


def _correct(q: dict[str, Any], gold_entry: dict[str, Any], result: Any) -> bool:
    if result is None or not result.ok:
        return False
    if q["type"] == "ambiguous":
        return any(result_verdict(e, result.rows, result.sql, result.columns).passed
                   for e in gold_entry["interpretations"].values())
    return result_verdict(gold_entry, result.rows, result.sql, result.columns).passed


def _repair_effect(q: dict[str, Any], gold_entry: dict[str, Any], r: Any,
                   outcome: ArchOutcome) -> str:
    """fix = wrong -> right, break = right -> wrong, judged against gold.

    Only defined for an accepted repair on the step that produced the graded
    result of a result-graded question; anything else is "n/a".
    """
    if (q["type"] == "why" or not r.accepted
            or r.step != outcome.selected_step):
        return "n/a"
    before, after = _correct(q, gold_entry, r.before), _correct(q, gold_entry, r.after)
    if before == after:
        return "neutral"
    return "fix" if after else "break"


def _not_evaluated(q: dict[str, Any], arch: str, reason: str) -> dict[str, Any]:
    return {"id": q["id"], "type": q["type"], "arch": arch,
            "state": "not_evaluated", "primary": None, "detail": reason}


def escalation_effects(items: list[dict[str, Any]]) -> dict[str, Any]:
    """For each escalated question of C and D, did escalation help or harm?

    helped = A failed and the architecture passed; harmed = A passed and it
    failed; no change otherwise. Direct-path questions are A's own run, so
    only escalated ones can differ.
    """
    evaluated = {(i["id"], i["arch"]): i for i in items if i["state"] == "evaluated"}
    out: dict[str, Any] = {}
    for arch in SHARES_STEP0:
        rows = []
        for (qid, name), item in evaluated.items():
            if name != arch or item.get("path") != "escalated":
                continue
            base = evaluated.get((qid, "baseline"))
            a, c = (base or {}).get("primary"), item.get("primary")
            effect = ("unknown" if a is None or c is None
                      else "helped" if c and not a
                      else "harmed" if a and not c else "no change")
            rows.append({"id": qid, "baseline": a, arch: c, "effect": effect})
        if rows:
            out[arch] = {
                **{k: sum(1 for r in rows if r["effect"] == k)
                   for k in ("helped", "harmed", "no change", "unknown")},
                "questions": rows,
            }
    return out


def missed_escalations(items: list[dict[str, Any]]) -> dict[str, Any]:
    """For each question A failed: did C and D escalate, and did they pass?

    A direct C or D run is A's run, so an A failure that C or D did not
    escalate is one they could not fix: a missed escalation.
    """
    evaluated = {(i["id"], i["arch"]): i for i in items if i["state"] == "evaluated"}
    failed = [qid for (qid, arch), i in evaluated.items()
              if arch == "baseline" and i.get("primary") is False]
    out: dict[str, Any] = {"baseline_failed": len(failed)}
    rows: list[dict[str, str]] = [{"id": qid} for qid in failed]
    for arch in SHARES_STEP0:
        counts = {"escalated": 0, "not_escalated": 0, "not_run": 0}
        for row in rows:
            item = evaluated.get((row["id"], arch))
            if item is None or item.get("primary") is None:
                counts["not_run"] += 1
                row[arch] = "not run"
                continue
            escalated = item.get("path") == "escalated"
            counts["escalated" if escalated else "not_escalated"] += 1
            row[arch] = (f"{'escalated' if escalated else 'direct'} "
                         f"({'pass' if item['primary'] else 'fail'})")
        out[arch] = counts
    out["questions"] = rows
    return out


def summarise(items: list[dict[str, Any]], archs: list[str]) -> dict[str, Any]:
    out = {}
    for arch in archs:
        mine = [i for i in items if i["arch"] == arch and i["state"] == "evaluated"]
        graded = [i for i in mine if i["primary"] is not None]
        k, n = sum(1 for i in graded if i["primary"]), len(graded)
        lo, hi = wilson_interval(k, n)
        by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for i in graded:
            by_type[i["type"]][0] += int(bool(i["primary"]))
            by_type[i["type"]][1] += 1
        out[arch] = {
            "primary": f"{k}/{n}", "wilson95": [round(lo, 3), round(hi, 3)],
            "pending": len(mine) - n,
            "answer_ok": sum(1 for i in mine if i.get("answer_ok")),
            "end_to_end": sum(1 for i in mine if i.get("end_to_end")),
            "avg_llm_calls": round(sum(i["llm_calls"] for i in mine) / len(mine), 2)
            if mine else 0,
            "avg_latency_s": round(sum(i["latency_s"] for i in mine) / len(mine), 2)
            if mine else 0,
            "avg_latency_live_s": round(
                sum(i.get("latency_live_s", i["latency_s"]) for i in mine) / len(mine),
                2) if mine else 0,
            "by_type": {t: f"{p}/{c}" for t, (p, c) in sorted(by_type.items())},
        }
    return out
