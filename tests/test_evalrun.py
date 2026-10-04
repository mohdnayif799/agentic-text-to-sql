"""Eval runner: response cache, backoff, redaction and a full offline run."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentcrew.llm import FakeClient, LLMError, QuotaExhaustedError
from evalrun.backoff import DailyLimitReached, call_with_backoff, is_daily_limit
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


def test_provider_error_is_a_failed_run_not_a_stop(cfg, tmp_path):
    class Overloaded(FakeClient):
        def complete(self, **kwargs):
            raise LLMError("503 overloaded")

    runner = Runner(cfg, live_factory=lambda s: Overloaded(), log=lambda *_: None)
    payload = runner.run("showcase", ["baseline"], tmp_path / "run")
    assert payload["complete"]
    assert all(i["primary"] is False for i in payload["items"])


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
