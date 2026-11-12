"""Langfuse run tracing for the v2 architectures (agentcrew/tracer.py,
LangfuseRunSink / RunTrace) and its wiring in archs/app_runner.py.

Langfuse is an optional extra. Pinned here: it is on only when its keys are
set; it records the same facts as the run log and nothing more unless
trace_content is on; a broken client is logged once and never reaches the
run; nothing on the run path waits on the network (no flush).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import pytest

from agentcrew.archs import app_runner
from agentcrew.archs.app_runner import AppRun, CountingClient, Runtime
from agentcrew.archs.outcome import ArchOutcome
from agentcrew.caps import DailyCap, DailyCapExceeded
from agentcrew.config import Settings
from agentcrew.llm import FakeClient, LLMError, Usage
from agentcrew.schemas import ExecutionResult
from agentcrew.tracer import (
    LangfuseRunSink,
    build_run_sink,
    build_tracer,
    langfuse_wanted,
)

SYSTEM = "You write one SQLite query.\nRules follow."
QUESTION = "a visitor's private question about Idirs Rosis"
ROW_VALUE = "APAC-secret-row-value"


# ---------------------------------------------------------------------------
# A fake Langfuse client that records every call
# ---------------------------------------------------------------------------
class FakeObs:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], **kwargs: Any) -> None:
        self.log = log
        self.kwargs = kwargs
        self.children: list[FakeObs] = []
        self.ended = False
        log.append(("start", kwargs))

    def start_observation(self, **kwargs: Any) -> FakeObs:
        child = FakeObs(self.log, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs: Any) -> FakeObs:
        self.log.append(("update", kwargs))
        return self

    def end(self, **_: Any) -> FakeObs:
        self.ended = True
        self.log.append(("end", {}))
        return self


class FakeLangfuse:
    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.roots: list[FakeObs] = []
        self.flushes = 0

    def start_observation(self, **kwargs: Any) -> FakeObs:
        root = FakeObs(self.log, **kwargs)
        self.roots.append(root)
        return root

    def flush(self) -> None:
        self.flushes += 1

    def auth_check(self) -> bool:
        return True

    def dump(self) -> str:
        return json.dumps(self.log, default=str)


class Broken:
    def start_observation(self, **_: Any) -> None:
        raise ConnectionError("langfuse is down")


@pytest.fixture
def warnings() -> list[str]:
    kept: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            kept.append(record.getMessage())

    logger = logging.getLogger("agentcrew.app.langfuse")
    handler, level = Keep(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield kept
    logger.removeHandler(handler)
    logger.setLevel(level)


def outcome(**overrides: Any) -> ArchOutcome:
    base = ArchOutcome(
        arch="full", question=QUESTION, status="done", answer="an answer",
        selected=ExecutionResult(ok=True, sql="SELECT region FROM t",
                                 columns=["region"], rows=[[ROW_VALUE]],
                                 row_count=1),
        path="escalated",
        steps=({"step": 1, "tools": ["run_sql"], "sql": "SELECT region FROM t"},
               {"step": 2, "tools": ["compute:pct_change"]}),
    )
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# When tracing is on
# ---------------------------------------------------------------------------
class TestWanted:
    def test_keys_alone_switch_it_on(self) -> None:
        assert not langfuse_wanted(Settings())
        assert langfuse_wanted(Settings(langfuse_public_key="pk",
                                        langfuse_secret_key="sk"))

    def test_explicit_false_switches_it_off_with_keys(self) -> None:
        assert not langfuse_wanted(Settings(
            langfuse_enabled=False, langfuse_public_key="pk",
            langfuse_secret_key="sk"))

    def test_one_key_is_not_enough(self) -> None:
        assert not langfuse_wanted(Settings(langfuse_public_key="pk"))
        assert not langfuse_wanted(Settings(langfuse_enabled=True,
                                            langfuse_public_key="pk"))

    def test_build_returns_none_when_off(self, warnings: list[str]) -> None:
        assert build_run_sink(Settings(), client_factory=lambda s: FakeLangfuse(),
                              check_auth=False) is None
        assert warnings == []

    def test_build_warns_when_enabled_without_keys(self, warnings: list[str]) -> None:
        assert build_run_sink(Settings(langfuse_enabled=True),
                              client_factory=lambda s: FakeLangfuse(),
                              check_auth=False) is None
        assert any("missing" in w for w in warnings)

    def test_build_makes_a_sink_with_keys(self, warnings: list[str]) -> None:
        settings = Settings(langfuse_public_key="pk", langfuse_secret_key="sk",
                            trace_content=True)
        sink = build_run_sink(settings, client_factory=lambda s: FakeLangfuse(),
                              check_auth=False)
        assert isinstance(sink, LangfuseRunSink) and sink.trace_content is True
        assert any("enabled" in w and "trace_content=on" in w for w in warnings)

    def test_a_client_that_cannot_start_means_off_with_a_warning(
        self, warnings: list[str]
    ) -> None:
        def boom(settings: Any) -> Any:
            raise ImportError("No module named langfuse")

        settings = Settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        assert build_run_sink(settings, client_factory=boom) is None
        assert any("tracing is off" in w for w in warnings)

    def test_v1_tracer_follows_the_same_rule(self) -> None:
        assert build_tracer(Settings())._langfuse is None
        assert build_tracer(Settings(langfuse_enabled=True))._langfuse is None


    def test_langfuse_is_not_imported_unless_wanted(self) -> None:
        """The default path must never load the package: it is an extra, and
        its import cost belongs only to deployments that set the keys. Checked
        in a fresh interpreter, because this process may have loaded it."""
        import subprocess
        import sys

        from conftest import clean_subprocess_env

        code = (
            "import sys\n"
            "import agentcrew.tracer, agentcrew.archs.app_runner\n"
            "from agentcrew.config import Settings\n"
            "from agentcrew.archs.app_runner import run_sink\n"
            "assert run_sink(Settings()) is None\n"
            "print([m for m in sys.modules if m.startswith('langfuse')])\n"
        )
        env = clean_subprocess_env()
        env = {k: v for k, v in env.items() if not k.startswith("AGENTCREW_")}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, env=env, check=True)
        assert done.stdout.strip() == "[]", done.stdout


# ---------------------------------------------------------------------------
# What a trace records
# ---------------------------------------------------------------------------
class TestRecorded:
    def _run(self, *, trace_content: bool, fail: bool = False) -> FakeLangfuse:
        fake = FakeLangfuse()
        sink = LangfuseRunSink(fake, trace_content=trace_content)
        trace = sink.start(arch="full", provider="gemini", model="m",
                           question=QUESTION)
        inner = FakeClient(['{"sql": "SELECT 1"}', '{"sql": "SELECT 2"}'])
        client = CountingClient(inner, trace=trace)
        client.complete(system=SYSTEM, user=QUESTION)
        if fail:
            client.inner = _Raising()
            with pytest.raises(LLMError):
                client.complete(system=SYSTEM, user=QUESTION)
            trace.finish(status="error", path="-", tools=[], llm_calls=2,
                         seconds=0.5, error="LLMError")
        else:
            trace.finish(status="done", path="escalated",
                         tools=[["run_sql"], ["compute:pct_change"]], llm_calls=1,
                         seconds=1.5, final_sql="SELECT region FROM t")
        return fake

    def test_run_and_calls_carry_the_log_facts(self) -> None:
        fake = self._run(trace_content=False)
        (root,) = fake.roots
        assert root.kwargs["name"] == "agentcrew.run"
        assert root.kwargs["metadata"] == {"arch": "full", "provider": "gemini",
                                           "model": "m", "trace_content": False}
        (call,) = root.children
        assert call.kwargs["as_type"] == "generation"
        assert call.kwargs["name"] == "llm:You write one SQLite query."
        updates = [kw for kind, kw in fake.log if kind == "update"]
        assert updates[0]["usage_details"]["input"] > 0
        assert updates[0]["level"] == "DEFAULT"
        assert updates[-1]["output"] == {"status": "done", "path": "escalated",
                                         "tools": "run_sql>compute:pct_change"}
        assert updates[-1]["metadata"]["llm_calls"] == 1
        assert root.ended and call.ended

    def test_without_trace_content_nothing_private_is_recorded(self) -> None:
        dump = self._run(trace_content=False).dump()
        assert QUESTION not in dump
        assert "Rules follow" not in dump  # the prompt body
        assert "SELECT" not in dump  # neither the completion nor the final SQL
        assert ROW_VALUE not in dump

    def test_with_trace_content_only_question_and_final_sql_are_added(self) -> None:
        fake = self._run(trace_content=True)
        dump = fake.dump()
        assert fake.roots[0].kwargs["input"] == {"question": QUESTION}
        updates = [kw for kind, kw in fake.log if kind == "update"]
        assert updates[-1]["output"]["final_sql"] == "SELECT region FROM t"
        assert "Rules follow" not in dump and ROW_VALUE not in dump
        assert "SELECT 1" not in dump  # a model reply is still not recorded

    def test_a_failed_call_and_run_are_marked_error_by_type_only(self) -> None:
        fake = self._run(trace_content=False, fail=True)
        updates = [kw for kind, kw in fake.log if kind == "update"]
        assert updates[1]["level"] == "ERROR"
        assert updates[1]["status_message"] == "LLMError"
        assert updates[-1]["status_message"] == "LLMError"
        assert "provider exploded" not in fake.dump()

    def test_nothing_on_the_run_path_flushes(self) -> None:
        assert self._run(trace_content=False).flushes == 0


class _Raising:
    name = "raising"
    _model = "m"

    def complete(self, **_: Any) -> Any:
        raise LLMError("provider exploded with request data")


# ---------------------------------------------------------------------------
# Failures never reach the run
# ---------------------------------------------------------------------------
class TestIsolation:
    def test_broken_client_logs_once_and_the_run_goes_on(
        self, warnings: list[str]
    ) -> None:
        sink = LangfuseRunSink(Broken())
        assert sink.start(arch="full", provider="p", model="m", question="q") is None
        assert sink.start(arch="full", provider="p", model="m", question="q") is None
        assert len([w for w in warnings if "skipped" in w]) == 1

    def test_broken_observation_does_not_break_a_call(
        self, warnings: list[str]
    ) -> None:
        class Root:
            def start_observation(self, **_: Any) -> Any:
                raise RuntimeError("otel broke")

            def update(self, **_: Any) -> Any:
                raise RuntimeError("otel broke")

        sink = LangfuseRunSink(FakeLangfuse())
        from agentcrew.tracer import RunTrace

        trace = RunTrace(sink, Root())
        client = CountingClient(FakeClient(['{"sql": "SELECT 1"}']), trace=trace)
        assert client.complete(system=SYSTEM, user="q").text == '{"sql": "SELECT 1"}'
        trace.finish(status="done", path="direct", tools=[], llm_calls=1, seconds=0)
        assert len([w for w in warnings if "skipped" in w]) == 1


# ---------------------------------------------------------------------------
# Wiring in app_runner
# ---------------------------------------------------------------------------
class TestAppRunnerWiring:
    @pytest.fixture
    def runtime(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Runtime, FakeLangfuse]:
        fake = FakeLangfuse()
        settings = Settings(provider="fake", arch="full")
        runtime = Runtime(settings, tools=None, builder=None,  # type: ignore[arg-type]
                          client_factory=lambda s: FakeClient(),
                          sink=LangfuseRunSink(fake))
        return runtime, fake

    def test_counts_tokens_and_closes_the_trace(
        self, runtime: tuple[Runtime, FakeLangfuse], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt, fake = runtime

        def fake_arch(arch, question, *, llm, **_):
            llm.complete(system=SYSTEM, user=question)
            llm.complete(system=SYSTEM, user=question)
            return outcome()

        monkeypatch.setattr(app_runner, "run_architecture", fake_arch)
        client = rt.new_client(trace=rt.start_trace(QUESTION))
        client.inner = FakeClient(["a" * 40, "b" * 40])  # FakeClient: 4 chars/token
        run = app_runner.run_arch_question(QUESTION, settings=rt.settings,
                                           runtime=rt, client=client)
        assert isinstance(run, AppRun) and run.llm_calls == 2
        assert run.input_tokens > 0 and run.output_tokens > 0
        updates = [kw for kind, kw in fake.log if kind == "update"]
        assert updates[-1]["output"]["path"] == "escalated"
        assert updates[-1]["output"]["tools"] == "run_sql>compute:pct_change"
        assert fake.roots[0].ended

    def test_a_raising_run_still_closes_the_trace(
        self, runtime: tuple[Runtime, FakeLangfuse], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt, fake = runtime

        def fake_arch(*_, **__):
            raise LLMError("boom")

        monkeypatch.setattr(app_runner, "run_architecture", fake_arch)
        client = rt.new_client(trace=rt.start_trace(QUESTION))
        with pytest.raises(LLMError):
            app_runner.run_arch_question(QUESTION, settings=rt.settings,
                                         runtime=rt, client=client)
        updates = [kw for kind, kw in fake.log if kind == "update"]
        assert updates[-1]["status_message"] == "LLMError"
        assert fake.roots[0].ended

    def test_no_sink_means_no_trace(self) -> None:
        rt = Runtime(Settings(provider="fake"), tools=None, builder=None,  # type: ignore[arg-type]
                     client_factory=lambda s: FakeClient())
        assert rt.start_trace("q") is None
        assert rt.new_client().trace is None

    def test_sink_is_built_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[Any] = []

        def factory(settings: Any) -> Any:
            calls.append(settings)
            return None

        monkeypatch.setattr(app_runner, "build_run_sink", factory)
        app_runner.reset_run_sink()
        app_runner.run_sink(Settings())
        app_runner.run_sink(Settings())
        assert len(calls) == 1
        app_runner.reset_run_sink()


# ---------------------------------------------------------------------------
# The daily cap on the counting client
# ---------------------------------------------------------------------------
class TestCallBudget:
    def test_a_call_past_the_cap_never_reaches_the_provider(self) -> None:
        inner = FakeClient(["a", "b", "c"])
        cap = DailyCap(max_runs=0, max_llm_calls=2)
        client = CountingClient(inner, budget=cap)
        client.complete(system=SYSTEM, user="q")
        client.complete(system=SYSTEM, user="q")
        with pytest.raises(DailyCapExceeded):
            client.complete(system=SYSTEM, user="q")
        assert client.calls == 2 and len(inner.prompts) == 2
        assert cap.snapshot().llm_calls == 2

    def test_usage_adds_up_over_calls(self) -> None:
        client = CountingClient(FakeClient(["aaaa", "bbbbbbbb"]))
        client.complete(system=SYSTEM, user="q")
        client.complete(system=SYSTEM, user="q")
        assert isinstance(client.usage, Usage)
        assert client.usage.calls == 2
        assert client.usage.output_tokens == len("aaaa") // 4 + len("bbbbbbbb") // 4
