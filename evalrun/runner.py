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
from evalkit.judge import JudgeVerdict, build_judge_prompt
from evalkit.questions import GOLD_FILES, ROOT, load_set
from evalkit.scoring import score_question
from evalkit.stats import wilson_interval
from evalrun.backoff import DailyLimitReached
from evalrun.cache import CacheMiss, CachingClient
from evalrun.redact import redact, redact_text, secrets_of

RUN_CONFIG = ROOT / "eval" / "run_config.yaml"
RESULT_ROWS = 20

#: (typical, maximum) LLM calls per question, used for the pre-run estimate.
#: v1's maximum follows its budgets at 3 steps x 3 attempts.
CALL_ESTIMATE = {"baseline": (2, 3), "v1": (5, 20), "router": (3, 8), "full": (5, 14)}


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
    why = sum(1 for q in questions if q["type"] == "why")
    per_arch = {
        a: {"typical": CALL_ESTIMATE[a][0] * n, "max": CALL_ESTIMATE[a][1] * n}
        for a in archs
    }
    judge = why * len(archs)
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
        dirty = _git("status", "--porcelain")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("-dirty" if dirty else "")


def _result_dict(outcome: ArchOutcome) -> dict[str, Any] | None:
    r = outcome.selected
    if r is None:
        return None
    return {"sql": r.sql, "ok": r.ok, "columns": r.columns,
            "rows": r.rows[:RESULT_ROWS], "row_count": r.row_count}


def _judge(
    q: dict[str, Any], gold_entry: dict[str, Any], answer: str,
    client: CachingClient, temperature: float,
) -> dict[str, Any]:
    system, user = build_judge_prompt(
        q["question"], q["judge_rubric"], gold_entry["evidence"], answer
    )
    try:
        verdict, _ = client.complete_structured(
            system=system, user=user, schema=JudgeVerdict,
            max_tokens=512, temperature=temperature,
        )
    except StructuredOutputError as exc:
        return {"verdict": None, "reason": f"judge error: {exc}"}
    return {"verdict": verdict.verdict, "reason": verdict.reason}


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
        tracer = Tracer(f"{arch}-{q['id']}", trace_dir=None)
        started = time.perf_counter()
        try:
            outcome = run_architecture(
                arch, q["question"], settings=self.settings, llm=client,
                tools=self.tools, context_builder=self.builder, tracer=tracer,
            )
        except LLMError as exc:  # provider error (not quota): a failed run
            message = redact_text(f"{type(exc).__name__}: {exc}", self.secrets)
            outcome = ArchOutcome(arch=arch, question=q["question"],
                                  status="failed", answer="", selected=None,
                                  path="error", stop_reason="error", error=message)
        latency = time.perf_counter() - started - client.slept_seconds

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

        trace = redact({
            "question_id": q["id"], "type": q["type"], "arch": arch,
            "question": q["question"], "model": self.cfg.model,
            "today": self.cfg.today.isoformat(), "status": outcome.status,
            "path": outcome.path, "stop_reason": outcome.stop_reason,
            "error": outcome.error, "plan": list(outcome.plan),
            "sql_attempts": [asdict(a) for a in outcome.attempts],
            "steps": list(outcome.steps),
            "selected_result": _result_dict(outcome),
            "answer": outcome.answer, "key_numbers": list(outcome.key_numbers),
            "caveats": list(outcome.caveats), "judge": judge,
            "llm_calls": [asdict(r) for r in client.records],
            "judge_calls": (
                [asdict(r) for r in judge_client.records] if judge_client else []
            ),
            "latency_s": round(latency, 3),
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
            "llm_calls": client.calls, "live_calls": client.live_calls,
            "cache_hits": client.cache_hits,
            "judge_calls": judge_client.calls if judge_client else 0,
            "latency_s": round(latency, 3),
            "trace": path.relative_to(trace_dir.parent).as_posix(),
        }

    def run(self, set_name: str, archs: list[str], run_dir: Path) -> dict[str, Any]:
        questions = load_set(set_name)
        gold_file = load_gold(GOLD_FILES[set_name])
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
            "items": items,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(redact(payload, self.secrets), indent=1,
                                   default=str) + "\n", "utf-8", newline="\n")
        return payload


def _flag(primary: bool | None) -> str:
    return "PEND" if primary is None else ("PASS" if primary else "FAIL")


def _not_evaluated(q: dict[str, Any], arch: str, reason: str) -> dict[str, Any]:
    return {"id": q["id"], "type": q["type"], "arch": arch,
            "state": "not_evaluated", "primary": None, "detail": reason}


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
            "by_type": {t: f"{p}/{c}" for t, (p, c) in sorted(by_type.items())},
        }
    return out
