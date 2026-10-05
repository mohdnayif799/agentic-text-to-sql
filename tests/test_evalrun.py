"""Eval runner: response cache, backoff, redaction and a full offline run."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentcrew.llm import FakeClient, LLMError, QuotaExhaustedError
from evalrun.backoff import (
    DailyLimitReached,
    ProviderUnavailable,
    call_with_backoff,
    is_daily_limit,
    is_provider_error,
)
from evalrun.cache import CacheMiss, CachingClient, prompt_hash
from evalrun.redact import redact
from evalrun.runner import RunConfig, Runner, planned_calls


class TestCache:
    def test_miss_then_hit(self, tmp_path):
        live = FakeClient(["first", "second"])
        a = CachingClient(lambda: live, cache_dir=tmp_path, arch="baseline",
                          qid="q1", model="m")
        assert a.complete(system="s", user="u").text == "first"
        b = CachingClient(lambda: live, cache_dir=tmp_path, arch="baseline",
                          qid="q1", model="m")
        assert b.complete(system="s", user="u").text == "first"
        assert (a.live_calls, b.live_calls, b.cache_hits) == (1, 0, 1)

    @pytest.mark.parametrize("field", ["arch", "qid", "model"])
    def test_key_includes_arch_question_and_model(self, tmp_path, field):
        live = FakeClient(["one", "two"])
        base = dict(cache_dir=tmp_path, arch="baseline", qid="q1", model="m")
        CachingClient(lambda: live, **base).complete(system="s", user="u")
        other = CachingClient(lambda: live, **{**base, field: "other"})
        assert other.complete(system="s", user="u").text == "two"

    def test_hit_reports_the_original_latency(self, tmp_path):
        live = FakeClient(["first"])
        base = dict(cache_dir=tmp_path, arch="baseline", qid="q1", model="m")
        first = CachingClient(lambda: live, **base)
        first.complete(system="s", user="u")
        again = CachingClient(None, **base)
        again.complete(system="s", user="u")
        (miss,), (hit,) = first.records, again.records
        assert hit.cache_hit and hit.latency_ms == 0.0
        assert hit.original_latency_ms == miss.latency_ms

    def test_offline_miss_raises(self, tmp_path):
        client = CachingClient(None, cache_dir=tmp_path, arch="a", qid="q", model="m")
        with pytest.raises(CacheMiss):
            client.complete(system="s", user="u")

    def test_prompt_hash_depends_on_every_input(self):
        base = prompt_hash("s", "u", 10, 0.0)
        assert base != prompt_hash("s", "u2", 10, 0.0)
        assert base != prompt_hash("s", "u", 11, 0.0)


class TestBackoff:
    def test_per_minute_limit_waits_and_retries(self):
        slept, calls = [], []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise QuotaExhaustedError("429 PerMinute quota. Please retry in 7.5s.")
            return "ok"

        value, total = call_with_backoff(fn, sleep=slept.append)
        assert value == "ok" and slept == [8.5] and total == 8.5

    def test_daily_limit_stops_immediately(self):
        slept = []

        def fn():
            raise QuotaExhaustedError("GenerateRequestsPerDayPerProjectPerModel")

        with pytest.raises(DailyLimitReached):
            call_with_backoff(fn, sleep=slept.append)
        assert slept == []

    def test_persistent_rate_limit_becomes_a_stop(self):
        slept = []

        def fn():
            raise QuotaExhaustedError("429 too many requests")

        with pytest.raises(DailyLimitReached):
            call_with_backoff(fn, sleep=slept.append, delays=(1.0, 2.0))
        assert slept == [1.0, 2.0]

    def test_transient_overload_is_retried(self):
        calls, slept = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise LLMError("Gemini call failed: 503 UNAVAILABLE high demand")
            return "ok"

        assert call_with_backoff(flaky, sleep=slept.append) == ("ok", 40.0)
        assert slept == [10.0, 30.0]

    def test_persistent_overload_and_other_errors_are_raised(self):
        def overloaded():
            raise LLMError("503 UNAVAILABLE")

        slept = []
        with pytest.raises(ProviderUnavailable):
            call_with_backoff(overloaded, sleep=slept.append)
        assert slept == [10.0, 30.0, 60.0]

        def broken():
            raise LLMError("400 invalid argument")

        slept.clear()
        with pytest.raises(LLMError):
            call_with_backoff(broken, sleep=slept.append)
        assert slept == []

    def test_daily_detection(self):
        assert is_daily_limit("quotaId: GenerateRequestsPerDay-FreeTier")
        assert not is_daily_limit("GenerateRequestsPerMinutePerProjectPerModel")


def test_redact_nested():
    secret = "AQ.secret-value-123"
    assert redact({"a": [f"key {secret}"], "b": 1}, (secret,)) == {"a": ["key ***"], "b": 1}


# --- full offline run ---------------------------------------------------------
def responder(system: str, user: str):
    if "SQL analyst" in system:
        return {"sql": "SELECT COUNT(*) AS n FROM customers", "rationale": "x",
                "expected_shape": ""}
    if "final answer" in system:
        return {"answer": "There are 1400 customers; Hardware drove it.",
                "key_numbers": [], "caveats": []}
    if "planning stage" in system:
        return {"intent": "x", "ambiguous": False, "clarifying_question": None,
                "steps": ["answer"]}
    if "SQL authoring stage" in system or "repair stage" in system:
        return {"sql": "SELECT COUNT(*) AS n FROM customers", "rationale": "x",
                "expected_shape": ""}
    if "verification stage" in system:
        return {"verdict": "pass", "reason": "x", "repair_hint": None}
    if "final stage" in system:
        return {"answer": "1400 customers.", "key_numbers": [], "caveats": []}
    if "strict" in system and "checker" in system:
        return {"verdict": "no", "quote": "", "reason": "scripted"}
    raise AssertionError(system[:80])


def judged_responder(system: str, user: str):
    """A answers sc03 naming the driver; the judge says yes to fact 1 only."""
    if "final answer" in system:
        return {"answer": "Online has more orders: 3,458 versus 2,366 for Direct.",
                "key_numbers": [], "caveats": []}
    if "strict fact checker" in system:
        if "Required fact:\nOnline has far more completed orders" in user:
            return {"verdict": "yes", "reason": "stated",
                    "quote": "Online has more orders: 3,458 versus 2,366 for Direct."}
        return {"verdict": "yes", "quote": "order value is the same", "reason": "x"}
    if "strict checker" in system:
        return {"verdict": "no", "quote": "", "reason": "none"}
    return responder(system, user)


@pytest.fixture
def cfg(tmp_path, db_path):
    return replace(RunConfig.load(), cache_dir=tmp_path / "cache")


def test_offline_run_writes_results_traces_and_resumes(cfg, tmp_path):
    made = []

    def factory(settings):
        made.append(settings)
        return FakeClient(responder=responder)

    runner = Runner(cfg, live_factory=factory, log=lambda *_: None)
    payload = runner.run("showcase", ["baseline", "v1"], tmp_path / "run")
    assert payload["complete"] and payload["today"] == "2026-10-04"
    assert payload["model"] == "gemini-3.5-flash-lite"
    items = payload["items"]
    assert len(items) == 10 and all(i["state"] == "evaluated" for i in items)
    assert made[0].today.isoformat() == "2026-10-04"
    first = items[0]
    trace = json.loads((tmp_path / "run" / "traces" / "baseline" / "sc01.json")
                       .read_text("utf-8"))
    assert trace["sql_attempts"] and trace["llm_calls"] and "latency_s" in trace
    assert first["llm_calls"] == 2 and first["live_calls"] == 2
    why = next(i for i in items if i["id"] == "sc03" and i["arch"] == "baseline")
    assert why["primary"] is False  # driver not named -> no judge call
    assert why["judge"] is None

    # Second run: nothing to do, no live client built.
    made.clear()
    again = Runner(cfg, live_factory=factory, log=lambda *_: None)
    assert again.run("showcase", ["baseline", "v1"], tmp_path / "run")["complete"]
    assert made == []


def test_daily_limit_stops_cleanly(cfg, tmp_path):
    class Exhausted(FakeClient):
        def complete(self, **kwargs):
            raise QuotaExhaustedError("GenerateRequestsPerDayPerProjectPerModel")

    runner = Runner(cfg, live_factory=lambda s: Exhausted(), log=lambda *_: None)
    payload = runner.run("showcase", ["baseline"], tmp_path / "run")
    assert not payload["complete"]
    assert "DailyLimitReached" in payload["stopped_reason"]
    assert {i["state"] for i in payload["items"]} == {"not_evaluated"}


class Overloaded(FakeClient):
    def complete(self, **kwargs):
        raise LLMError("Gemini call failed: 503 UNAVAILABLE high demand")


def test_persistent_provider_error_is_not_evaluated_and_resumed(cfg, tmp_path):
    """Pre-run amendment 3."""
    slept: list[float] = []
    run_dir = tmp_path / "run"
    day1 = Runner(cfg, live_factory=lambda s: Overloaded(), log=lambda *_: None,
                  sleep=slept.append, clock=lambda: "2026-10-06")
    payload = day1.run("showcase", ["baseline"], run_dir)
    assert not payload["complete"]  # waiting for a resume, not a result
    assert slept == [10.0, 30.0, 60.0] * 5  # three retries per question
    for item in payload["items"]:
        assert item["state"] == "not_evaluated" and item["primary"] is None
        assert item["provider_errors"][0]["date"] == "2026-10-06"
        assert item["provider_errors"][0]["stage"] == "step 1: SQL"
    assert payload["summary"]["baseline"]["not_evaluated"] == 5
    assert len(payload["provider_resumes"]) == 5

    # Day 2: the provider is back; every question resumes and is listed.
    day2 = Runner(cfg, live_factory=lambda s: FakeClient(responder=responder),
                  log=lambda *_: None, clock=lambda: "2026-10-07")
    again = day2.run("showcase", ["baseline"], run_dir)
    assert again["complete"]
    for item in again["items"]:
        assert item["state"] == "evaluated" and item["resumed"] is True
        assert [e["date"] for e in item["provider_errors"]] == ["2026-10-06"]
    assert {r["state"] for r in again["provider_resumes"]} == {"evaluated"}


def test_provider_error_on_resumes_on_two_days_is_a_fail(cfg, tmp_path):
    run_dir = tmp_path / "run"

    def attempt(day):
        return Runner(cfg, live_factory=lambda s: Overloaded(), log=lambda *_: None,
                      sleep=lambda s: None, clock=lambda: day).run(
            "showcase", ["baseline"], run_dir)

    attempt("2026-10-06")                        # original run
    attempt("2026-10-07")                        # resume, day 1
    same_day = attempt("2026-10-07")             # resume, same day again
    assert {i["state"] for i in same_day["items"]} == {"not_evaluated"}
    final = attempt("2026-10-08")                # resume, a second day
    for item in final["items"]:
        assert item["state"] == "evaluated" and item["primary"] is False
        assert item["status"] == "ERROR"
        assert item["error"]["type"] == "ProviderUnavailable"
        assert len(item["provider_errors"]) == 4
    assert final["complete"]


def test_model_and_code_errors_still_count_as_fails(cfg, tmp_path):
    class BadRequest(FakeClient):
        def complete(self, **kwargs):
            raise LLMError("Gemini call failed: 400 INVALID_ARGUMENT bad schema")

    payload = Runner(cfg, live_factory=lambda s: BadRequest(), log=lambda *_: None,
                     sleep=lambda s: None).run("showcase", ["baseline"],
                                               tmp_path / "run")
    assert payload["complete"]
    assert all(i["status"] == "ERROR" and i["primary"] is False
               for i in payload["items"])


@pytest.mark.parametrize(("error", "expected"), [
    (LLMError("Gemini call failed: 503 UNAVAILABLE. high demand"), True),
    (LLMError("Gemini call failed: 500 INTERNAL. error"), True),
    (LLMError("Gemini call failed: request timed out"), True),
    (LLMError("Gemini call failed: 400 INVALID_ARGUMENT"), False),
    (LLMError("model returned LIMIT 500 rows"), False),
])
def test_provider_error_classification(error, expected):
    assert is_provider_error(error) is expected


def test_provider_error_classification_reads_the_cause():
    class ServerError(Exception):
        code = 502

    for cause in (ServerError("bad gateway?"), TimeoutError("read")):
        try:
            try:
                raise cause
            except Exception as inner:
                raise LLMError("call failed") from inner
        except LLMError as exc:
            assert is_provider_error(exc)


def test_exception_inside_an_architecture_is_an_error_with_its_stage(
    cfg, tmp_path, monkeypatch
):
    import agentcrew.archs.router as router_module

    def broken(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(router_module, "write_answer", broken)
    runner = Runner(cfg, live_factory=lambda s: FakeClient(responder=router_responder),
                    log=lambda *_: None)
    payload = runner.run("showcase", ["router"], tmp_path / "run")
    item = payload["items"][0]
    assert item["status"] == "ERROR" and item["error"]["type"] == "KeyError"
    assert item["error"]["stage"] == "final: answer"
    assert item["error"]["where"].startswith("agentcrew/archs/router.py:")
    trace = json.loads((tmp_path / "run" / item["trace"]).read_text("utf-8"))
    assert trace["status"] == "ERROR" and trace["error_detail"] == item["error"]


def test_a_provider_error_names_the_call_it_happened_in(cfg, tmp_path):
    def failing_decision(system, user):
        if "You check whether one query result" in system:
            raise LLMError("500 internal")
        return router_responder(system, user)

    runner = Runner(cfg, live_factory=lambda s: FakeClient(responder=failing_decision),
                    log=lambda *_: None, sleep=lambda s: None)
    item = runner.run("showcase", ["router"], tmp_path / "run")["items"][0]
    assert (item["status"], item["error"]["stage"]) == ("ERROR", "follow-up decision")


def test_per_fact_judge_and_rejudge_from_stored_answers(cfg, tmp_path):
    made = []

    def factory(settings):
        made.append(1)
        return FakeClient(responder=judged_responder)

    run_dir = tmp_path / "run"
    payload = Runner(cfg, live_factory=factory, log=lambda *_: None).run(
        "showcase", ["baseline"], run_dir)
    sc03 = next(i for i in payload["items"] if i["id"] == "sc03")
    judge = sc03["judge"]
    # Fact 1 quoted verbatim -> yes; fact 2's "yes" quote is not in the
    # answer -> counts as no; so the answer fails.
    assert [f["counts_as_yes"] for f in judge["facts"]] == [True, False]
    assert judge["facts"][1]["verdict"] == "yes"
    assert judge["facts"][1]["quote_verified"] is False
    assert judge["verdict"] == "fail" and sc03["primary"] is False
    assert sc03["judge_calls"] == 3  # 2 facts + 1 disqualifying check

    # Re-judge: answers come from the traces; judge calls hit the cache.
    redo = Runner(cfg, live_factory=None, log=lambda *_: None).rejudge(
        "showcase", run_dir)
    again = next(i for i in redo["items"] if i["id"] == "sc03")
    assert again["judge"] == judge and "rejudged_utc" in redo


def test_planned_calls():
    rubric = {"required_facts": ["a", "b"], "disqualifying": ["c"]}
    questions = [{"type": "why", "judge_rubric": rubric}, {"type": "easy"}]
    plan = planned_calls(questions, ["baseline", "v1"])
    assert plan["judge_max"] == 3 * 2
    assert plan["typical"] == 2 * 2 + 5 * 2 + 6
    assert plan["max"] == 3 * 2 + 20 * 2 + 6


def test_escalation_effects():
    from evalrun.runner import escalation_effects

    def item(qid, arch, primary, path):
        return {"id": qid, "arch": arch, "state": "evaluated", "primary": primary,
                "path": path}

    items = [
        item("q1", "baseline", False, "direct"), item("q1", "router", True, "escalated"),
        item("q2", "baseline", True, "direct"), item("q2", "router", False, "escalated"),
        item("q3", "baseline", False, "direct"), item("q3", "router", False, "escalated"),
        item("q4", "baseline", True, "direct"), item("q4", "router", True, "direct"),
    ]
    effects = escalation_effects(items)["router"]
    assert (effects["helped"], effects["harmed"], effects["no change"]) == (1, 1, 1)
    assert [q["id"] for q in effects["questions"]] == ["q1", "q2", "q3"]


def test_missed_escalations():
    from evalrun.runner import missed_escalations

    def item(qid, arch, primary, path):
        return {"id": qid, "arch": arch, "state": "evaluated", "primary": primary,
                "path": path}

    items = [
        item("q1", "baseline", False, "direct"), item("q1", "router", False, "direct"),
        item("q1", "full", True, "escalated"),
        item("q2", "baseline", True, "direct"), item("q2", "full", True, "direct"),
        item("q3", "baseline", False, "direct"),
    ]
    missed = missed_escalations(items)
    assert missed["baseline_failed"] == 2
    assert missed["router"] == {"escalated": 0, "not_escalated": 1, "error": 0,
                                "not_run": 1}
    assert missed["full"] == {"escalated": 1, "not_escalated": 0, "error": 0,
                              "not_run": 1}
    assert missed["questions"] == [
        {"id": "q1", "router": "direct (fail)", "full": "escalated (pass)"},
        {"id": "q3", "router": "not run", "full": "not run"},
    ]


def test_results_carry_missed_escalations_and_live_latency(cfg, tmp_path):
    runner = Runner(cfg, live_factory=lambda s: FakeClient(responder=responder),
                    log=lambda *_: None)
    payload = runner.run("showcase", ["baseline"], tmp_path / "run")
    assert payload["missed_escalations"]["baseline_failed"] >= 1
    replay = Runner(cfg, live_factory=None, log=lambda *_: None).run(
        "showcase", ["baseline"], tmp_path / "replay")
    item = replay["items"][0]
    assert item["live_calls"] == 0
    assert item["latency_live_s"] >= item["latency_s"]
    assert "avg_latency_live_s" in replay["summary"]["baseline"]


def test_report_has_every_section(cfg, tmp_path):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "report_v2", root / "scripts" / "report_v2.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    run_dir = tmp_path / "run"
    payload = Runner(cfg, live_factory=lambda s: FakeClient(responder=responder),
                     log=lambda *_: None).run("showcase", ["baseline"], run_dir)
    text = report.render(payload, run_dir, cfg.cache_dir)
    for heading in ("## Summary", "## Per question", "## Escalation against A",
                    "## Missed escalations (questions A failed)"):
        assert heading in text
    assert "| sc03 | why | A baseline | FAIL |" in text



def router_responder(system: str, user: str):
    """C: a clean step 0, then a follow-up decision that needs one."""
    if "You check whether one query result" in system:
        return {"needs_followup": True, "reason": "more"}
    if "continuing an analysis" in system:
        return {"done": True, "sql": "", "reason": ""}
    return responder(system, user)


def test_missed_escalations_report_errors_not_direct():
    from evalrun.runner import missed_escalations

    items = [
        {"id": "q1", "arch": "baseline", "state": "evaluated", "primary": False,
         "path": "direct"},
        {"id": "q1", "arch": "router", "state": "evaluated", "primary": False,
         "path": "escalated"},
        {"id": "q1", "arch": "full", "state": "evaluated", "primary": False,
         "path": "error", "status": "ERROR",
         "error": {"type": "LLMError", "stage": "step 2: decide_next_action"}},
    ]
    missed = missed_escalations(items)
    assert missed["full"]["error"] == 1 and missed["full"]["not_escalated"] == 0
    assert missed["questions"][0]["full"] == \
        "ERROR (LLMError at step 2: decide_next_action)"


class TestForcedEscalation:
    """A development-only switch: refused on holdout sets and for A, B, C."""

    @pytest.mark.parametrize("set_name", ["holdout_agent", "holdout_easy"])
    def test_refused_on_holdout_sets(self, set_name):
        from evalrun.runner import check_force_escalation

        with pytest.raises(ValueError, match="holdout"):
            check_force_escalation(set_name, ["full"])

    def test_d_only(self):
        from evalrun.runner import check_force_escalation

        check_force_escalation("dev2", ["full"])
        with pytest.raises(ValueError, match="D only"):
            check_force_escalation("dev2", ["baseline", "full"])

    def test_runner_refuses_before_any_call(self, cfg, tmp_path):
        made = []
        runner = Runner(cfg, live_factory=lambda s: made.append(1),
                        log=lambda *_: None, force_escalation=True)
        with pytest.raises(ValueError, match="holdout"):
            runner.run("holdout_easy", ["full"], tmp_path / "run")
        assert made == [] and not (tmp_path / "run").exists()

    def test_forced_and_normal_results_never_mix(self, cfg, tmp_path):
        normal = Runner(cfg, live_factory=lambda s: FakeClient(responder=responder),
                        log=lambda *_: None)
        normal.run("showcase", ["baseline"], tmp_path / "run")
        forced = Runner(cfg, live_factory=lambda s: FakeClient(responder=responder),
                        log=lambda *_: None, force_escalation=True)
        with pytest.raises(ValueError, match="mixes"):
            forced.run("showcase", ["full"], tmp_path / "run")



def test_paired_comparison_against_a():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "report_v2", root / "scripts" / "report_v2.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)

    def item(qid, arch, primary, state="evaluated"):
        return {"id": qid, "arch": arch, "state": state, "primary": primary}

    items = [item("q1", "baseline", True), item("q1", "full", True),
             item("q2", "baseline", False), item("q2", "full", True),
             item("q3", "baseline", True), item("q3", "full", False),
             item("q4", "baseline", False), item("q4", "full", True),
             item("q5", "baseline", True), item("q5", "full", None, "not_evaluated")]
    p = report.paired_against_a(items, "full")
    assert (p["n"], p["both"], p["only_a"], p["only_b"]) == (4, 1, 1, 2)
    assert p["mcnemar_p"] == 1.0 and p["not_worse_than_a"] is True
