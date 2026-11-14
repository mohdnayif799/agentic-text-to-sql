"""The daily cap in the app: the ceiling behind the per-session allowance.

Rendered through AppTest with the agent replaced by FakeAgent. Each
start_app() is a new browser session with a fresh per-session allowance; the
daily cap is process-wide, so it has to hold across sessions. conftest
forgets it before every test.
"""

from __future__ import annotations

import pytest
from ui_harness import OWN_KEY, FakeAgent, ask, start_app

from agentcrew import caps
from agentcrew.llm import LLMError

DAILY_MESSAGE = (
    "The demo has reached its daily limit. Try again tomorrow, or add your own "
    "key under 'Use your own API key'."
)


def warnings(at) -> list[str]:
    return [w.value for w in at.warning]


class TestDailyRunCap:
    def test_holds_across_browser_sessions(
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "2")
        first = start_app()
        first = ask(first, "question 1")
        first = ask(first, "question 2")
        assert len(fake_agent.settings_seen) == 2

        second = start_app()  # a reload: the per-session allowance is fresh
        second = ask(second, "question 3")
        assert len(fake_agent.settings_seen) == 2, "the cap must stop the run"
        assert DAILY_MESSAGE in warnings(second)

    def test_a_failed_run_is_not_counted(
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "1")
        fake_agent.error = LLMError("Gemini call failed: 503 overloaded")
        ask(start_app(), "q")
        fake_agent.error = None
        at = ask(start_app(), "q")
        assert len(fake_agent.settings_seen) == 2
        assert DAILY_MESSAGE not in warnings(at)

    def test_a_refused_write_request_is_not_counted(
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "1")
        ask(start_app(), "Delete all customers in the LATAM region.")
        at = ask(start_app(), "q")
        assert len(fake_agent.settings_seen) == 1
        assert DAILY_MESSAGE not in warnings(at)

    def test_own_key_is_never_capped(
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "1")
        ask(start_app(), "q")  # uses up the day on the server key
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        at = ask(at, "q")
        assert len(fake_agent.settings_seen) == 2
        assert fake_agent.settings_seen[-1].gemini_api_key == OWN_KEY
        assert DAILY_MESSAGE not in warnings(at)

    def test_cap_is_read_from_the_environment_once(
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "1")
        ask(start_app(), "q")
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "100")  # ignored: already built
        at = ask(start_app(), "q")
        assert DAILY_MESSAGE in warnings(at)
        assert caps.daily_cap(None).snapshot().max_runs == 1

    def test_demo_counter_still_shows_the_session_allowance(
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The daily total is not shown: it is an operator's number."""
        monkeypatch.setenv("AGENTCREW_DAILY_RUN_CAP", "1")
        at = ask(start_app(), "q")
        from ui_harness import everything_rendered, sidebar_captions

        assert "Demo runs left: 4 of 5" in sidebar_captions(at)
        assert "daily" not in everything_rendered(at).lower()
