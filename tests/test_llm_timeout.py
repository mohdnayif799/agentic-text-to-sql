"""A slow model: the 30-second request timeout and per-call logging.

On 2026-10-26 a direct question on the live demo took 89.8 s with no error:
three Gemini calls that each answered slowly. Two changes follow from it:

* GeminiClient gives up on a request after GEMINI_TIMEOUT_SECONDS and raises
  LLMTimeoutError. The app shows a short message, never crashes, and does not
  count the run against the 5 demo runs.
* CountingClient (the client every app run goes through) logs each call with
  its role, duration and token counts, so the next slow run shows which call
  was slow.

No network: GeminiClient's HTTP call is replaced, and the app runs on the
FakeAgent from ui_harness.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from ui_harness import OWN_KEY, FakeAgent, ask, sidebar_captions, start_app

from agentcrew.archs.app_runner import CountingClient
from agentcrew.llm import (
    GEMINI_TIMEOUT_SECONDS,
    GeminiClient,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMTimeoutError,
    Usage,
)
from evalrun.backoff import is_provider_error

SLOW_MESSAGE = "The model is responding slowly right now. Please try again in a minute."


def gemini(monkeypatch: pytest.MonkeyPatch, raise_: BaseException) -> tuple[
        GeminiClient, list[dict[str, Any]]]:
    """A GeminiClient whose HTTP call raises `raise_`; returns its attempts."""
    client = GeminiClient("AQ.test-key-not-real", "gemini-3.5-flash-lite")
    attempts: list[dict[str, Any]] = []

    def call(**kwargs: Any) -> None:
        attempts.append(kwargs)
        raise raise_

    monkeypatch.setattr(client._client.models, "generate_content", call)
    return client, attempts


# ---------------------------------------------------------------------------
# GeminiClient
# ---------------------------------------------------------------------------
class TestGeminiTimeout:
    def test_the_client_is_built_with_a_30_second_timeout(self) -> None:
        client = GeminiClient("AQ.test-key-not-real", "gemini-3.5-flash-lite")
        assert GEMINI_TIMEOUT_SECONDS == 30
        # google-genai's HttpOptions.timeout is in milliseconds.
        assert client._client._api_client._http_options.timeout == 30_000

    def test_a_timed_out_request_raises_llm_timeout_error_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx.ReadTimeout is what google-genai 2.20 raises when its timeout
        fires (checked against a local server that never answers)."""
        client, attempts = gemini(monkeypatch, httpx.ReadTimeout("timed out"))
        with pytest.raises(LLMTimeoutError) as raised:
            client.complete(system="s", user="u")
        assert len(attempts) == 1  # no retry: the visitor is told to try again
        assert isinstance(raised.value, LLMError)
        assert isinstance(raised.value.__cause__, httpx.ReadTimeout)

    def test_a_timeout_deeper_in_the_cause_chain_is_still_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        try:
            raise TimeoutError("read timed out")
        except TimeoutError as inner:
            wrapped = RuntimeError("transport failed")
            wrapped.__cause__ = inner
        client, _ = gemini(monkeypatch, wrapped)
        with pytest.raises(LLMTimeoutError):
            client.complete(system="s", user="u")

    def test_other_failures_are_not_timeouts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = gemini(monkeypatch, ValueError("bad request"))
        with pytest.raises(LLMError) as raised:
            client.complete(system="s", user="u")
        assert not isinstance(raised.value, LLMTimeoutError)

    def test_the_evaluation_still_retries_a_timeout_as_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """evalrun/backoff.py retries provider errors; a timeout must stay one."""
        client, _ = gemini(monkeypatch, httpx.ReadTimeout("timed out"))
        with pytest.raises(LLMTimeoutError) as raised:
            client.complete(system="s", user="u")
        assert is_provider_error(raised.value)


# ---------------------------------------------------------------------------
# CountingClient: one log line per call
# ---------------------------------------------------------------------------
class Inner(LLMClient):
    """A provider stand-in: answers, or raises `error`."""

    name = "inner"

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    def complete(self, *, system: str, user: str, max_tokens: int = 2048,
                 temperature: float = 0.0) -> LLMResponse:
        if self.error is not None:
            raise self.error
        return LLMResponse(text="SELECT 1",
                           usage=Usage(input_tokens=1200, output_tokens=34, calls=1))


@pytest.fixture
def llm_log() -> Any:
    """Lines logged on agentcrew.app.llm at INFO. In the app its parent,
    agentcrew.app, sets INFO and the stderr handler (app.py)."""
    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("agentcrew.app.llm")
    handler, level = Keep(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(level)


class TestCallLog:
    SYSTEM = "You write one SQLite query.\nRules follow."
    QUESTION = "a visitor's private question"

    def test_each_call_is_logged_with_role_duration_and_tokens(
        self, llm_log: list[str]
    ) -> None:
        client = CountingClient(Inner())
        client.complete(system=self.SYSTEM, user=self.QUESTION)
        client.complete(system=self.SYSTEM, user=self.QUESTION)
        assert client.calls == 2
        assert len(llm_log) == 2
        line = llm_log[1]
        assert line.startswith("llm_call n=2 ")
        assert 'role="You write one SQLite query."' in line
        assert "status=ok" in line
        assert "input_tokens=1200 output_tokens=34" in line
        assert float(line.split("seconds=")[1].split()[0]) >= 0
        assert self.QUESTION not in line  # never the question text

    def test_a_timed_out_call_is_logged_and_re_raised(
        self, llm_log: list[str]
    ) -> None:
        client = CountingClient(Inner(LLMTimeoutError("Gemini call timed out")))
        with pytest.raises(LLMTimeoutError):
            client.complete(system=self.SYSTEM, user=self.QUESTION)
        assert client.calls == 1
        (line,) = llm_log
        assert "status=LLMTimeoutError" in line
        assert "input_tokens" not in line


# ---------------------------------------------------------------------------
# The app: a short message, no crash, no demo run used
# ---------------------------------------------------------------------------
class TestSlowModelInTheApp:
    def test_demo_run_that_times_out_shows_the_message_and_is_not_counted(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMTimeoutError("Gemini call timed out after 30 s")
        at = ask(start_app(), "What was total completed revenue in 2024?")
        assert not at.exception
        assert [w.value for w in at.warning] == [SLOW_MESSAGE]
        assert not at.error
        assert "Demo runs left: 5 of 5" in sidebar_captions(at)

    def test_own_key_run_that_times_out_shows_the_same_message(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMTimeoutError("Gemini call timed out after 30 s")
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        at = ask(at, "q")
        assert not at.exception
        assert [w.value for w in at.warning] == [SLOW_MESSAGE]
        assert not at.error

    def test_the_run_still_counts_once_the_model_answers(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMTimeoutError("slow")
        at = ask(start_app(), "q")
        fake_agent.error = None
        at = ask(at, "q again")
        assert "Demo runs left: 4 of 5" in sidebar_captions(at)
