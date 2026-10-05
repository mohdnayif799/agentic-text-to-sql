"""agentcrew/archs/app_runner.py: how the app runs D (and A, C). Offline:
a scripted model and the real read-only database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs.app_runner import build_runtime, run_arch_question
from agentcrew.llm import FakeClient, LLMError

COUNT = "SELECT COUNT(*) AS n FROM customers"


def scripted(system: str, user: str):
    if "SQL analyst" in system:
        return {"sql": COUNT, "rationale": "x", "expected_shape": ""}
    if "You check whether one query result" in system:
        return {"needs_followup": True, "reason": "check"}
    if "You plan the next step" in system:
        return {"action": "answer"}
    if "final answer" in system:
        return {"answer": "1,400 customers.", "key_numbers": [], "caveats": []}
    raise AssertionError(system[:80])


def test_runs_d_counts_calls_and_reports_stages(settings) -> None:
    pinned = settings.model_copy(update={"arch": "full", "today": date(2026, 10, 4)})
    runtime = build_runtime(pinned, client_factory=lambda s: FakeClient(
        responder=scripted))
    stages: list[str] = []
    run = run_arch_question("How many customers do we have?", settings=pinned,
                            runtime=runtime, on_stage=stages.append)
    assert run.outcome.arch == "full" and run.outcome.path == "escalated"
    assert run.outcome.selected.rows == [[1400]]
    assert run.llm_calls == 4  # step 0, decision, next step, answer
    assert run.tools[0] == ["run_sql"]
    assert stages[0] == "step 1: SQL (A's call)" and stages[-1] == "final: answer"
    assert run.seconds >= 0


def test_runtime_reuses_the_profiled_context(settings) -> None:
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    first = build_runtime(pinned, client_factory=lambda s: FakeClient())
    second = build_runtime(pinned, client_factory=lambda s: FakeClient())
    assert first.builder is second.builder and first.tools is second.tools


def test_the_count_survives_a_failing_run(settings) -> None:
    def broken(system: str, user: str):
        if "SQL analyst" in system:
            return {"sql": COUNT, "rationale": "x", "expected_shape": ""}
        raise LLMError("provider down")

    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    runtime = build_runtime(pinned, client_factory=lambda s: FakeClient(
        responder=broken))
    client = runtime.new_client()
    with pytest.raises(LLMError):
        run_arch_question("q", settings=pinned, runtime=runtime, client=client)
    assert client.calls == 2  # step 0, then the failing decision call


def test_d_is_the_default_architecture() -> None:
    from agentcrew.config import Settings

    assert Settings().arch == "full"
