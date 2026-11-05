"""Harness for the secondary model experiment. Offline, no model calls."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from agentcrew.llm import FakeClient, LLMError, QuotaExhaustedError
from evalrun.backoff import call_with_backoff
from evalrun.runner import RunConfig, Runner

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call(client) -> None:
    client.complete(system="s", user="u", max_tokens=8, temperature=0.0)


class TestJudgeStaysFixed:
    def _runner(self, tmp_path, judge_model):
        seen: list[str] = []

        def factory(settings):
            seen.append(settings.model)
            return FakeClient(responder=lambda system, user: "ok")

        cfg = replace(RunConfig.load(), model="model-under-test",
                      judge_model=judge_model, cache_dir=tmp_path)
        return Runner(cfg, live_factory=factory, sleep=lambda _: None), seen

    def test_the_judge_uses_its_own_model_and_cache_folder(self, tmp_path):
        runner, seen = self._runner(tmp_path, "fixed-judge")
        _call(runner._judge_client("full", "q1"))
        _call(runner._client("full", "q1"))
        assert seen == ["fixed-judge", "model-under-test"]
        assert list((tmp_path / "fixed-judge" / "judge-full" / "q1").glob("*.json"))
        assert list((tmp_path / "model-under-test" / "full" / "q1").glob("*.json"))

    def test_without_a_judge_model_nothing_changes(self, tmp_path):
        runner, seen = self._runner(tmp_path, None)
        _call(runner._judge_client("full", "q1"))
        assert seen == ["model-under-test"]
        assert list((tmp_path / "model-under-test" / "judge-full" / "q1").glob("*.json"))

    def test_the_primary_configuration_has_no_judge_model(self):
        assert RunConfig.load().judge_model is None


class TestRetriesAreCounted:
    def test_a_503_that_a_retry_hid_is_still_reported(self):
        calls, noted = [], []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise LLMError("503 UNAVAILABLE high demand")
            return "fine"

        value, _ = call_with_backoff(flaky, sleep=lambda _: None,
                                     on_retry=lambda k, m: noted.append(k))
        assert value == "fine"
        assert noted == ["provider"]

    def test_a_rate_limit_is_a_different_kind(self):
        calls, noted = [], []

        def limited():
            calls.append(1)
            if len(calls) == 1:
                raise QuotaExhaustedError("429 per minute, retry in 1s")
            return "fine"

        call_with_backoff(limited, sleep=lambda _: None,
                          on_retry=lambda k, m: noted.append(k))
        assert noted == ["rate_limit"]

    def test_the_runner_keeps_each_retry_with_its_question(self, tmp_path):
        attempts = []

        class Flaky(FakeClient):
            def complete(self, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise LLMError("503 UNAVAILABLE")
                return super().complete(**kwargs)

        cfg = replace(RunConfig.load(), cache_dir=tmp_path)
        runner = Runner(cfg, sleep=lambda _: None,
                        live_factory=lambda s: Flaky(responder=lambda a, b: "ok"))
        _call(runner._client("full", "q7"))
        assert [(r["id"], r["arch"], r["kind"]) for r in runner.retries] == [
            ("q7", "full", "provider")]


class TestRunConfigChoice:
    def test_a_model_override_needs_its_own_cache(self):
        with pytest.raises(ValueError, match="cache-dir"):
            _script("run_eval_v2").run_config(None, "some-model")

    def test_the_judge_keeps_the_configured_model(self):
        base = RunConfig.load()
        cfg = _script("run_eval_v2").run_config("eval/runs/x-cache", "some-model")
        assert cfg.model == "some-model"
        assert cfg.judge_model == base.model

    def test_no_override_leaves_the_configuration_alone(self):
        base = RunConfig.load()
        cfg = _script("run_eval_v2").run_config(None)
        assert cfg == base
