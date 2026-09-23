"""Demo mode: the shared server key and the per-session run allowance.

The public demo runs on a key configured on the server. Three properties
matter, and each is easy to break without noticing:

* The server key must never reach the browser. Streamlit sends every widget's
  value to the client, so a key used as a widget default is readable by any
  visitor - password fields even have a show/hide toggle. These tests render
  the real app with Streamlit's AppTest and search every element for the key.
* Runs on the server key are capped per browser session, with fixed budgets.
  Only a run that completed uses up the allowance: a quota refusal, a missing
  key or any exception does not. A visitor's own key is never capped.
* A quota refusal (HTTP 429) stops the agent at once. It is not a SQL
  failure, so the repair loop must never see it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from conftest import ScriptedAnalyst
from google.genai import errors as genai_errors
from ui_harness import (
    OWN_KEY,
    SERVER_KEY,
    FakeAgent,
    ask,
    everything_rendered,
    sidebar_captions,
    start_app,
    stub_outcome,
)

from agentcrew.graph.build import run_question
from agentcrew.llm import PROVIDERS, GeminiClient, LLMError, QuotaExhaustedError

LIMIT_MESSAGE = (
    "You've used the 5 free demo runs. Add your own Gemini key (free at "
    "aistudio.google.com/apikey) under 'Use your own API key' to keep going."
)
QUOTA_MESSAGE = (
    "The shared demo quota is used up for now. Try again later or add your own key."
)
UNAVAILABLE_MESSAGE = (
    "The demo is temporarily unavailable. Try again later, or add your own key "
    "under 'Use your own API key'."
)
FIXED_CAPTION = "Fixed in demo mode. Add your own key to change."


# ---------------------------------------------------------------------------
# The server key never reaches the browser
# ---------------------------------------------------------------------------
class TestServerKeyNeverRendered:
    def test_key_box_starts_empty(self, demo_env: None) -> None:
        at = start_app()
        assert not at.exception
        assert at.text_input(key="api_key_gemini").value == ""

    def test_server_key_is_in_no_widget_or_element(self, demo_env: None) -> None:
        at = start_app()
        assert SERVER_KEY not in everything_rendered(at)
        assert SERVER_KEY not in repr(at.session_state.filtered_state)

    def test_server_key_stays_hidden_on_every_provider(self, demo_env: None) -> None:
        at = start_app()
        for provider in ("anthropic", "openai", "gemini"):
            at.selectbox(key="provider").set_value(provider).run()
            assert SERVER_KEY not in everything_rendered(at)

    def test_error_text_containing_the_key_is_never_shown(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMError(f"400 API key not valid: {SERVER_KEY}")
        at = ask(start_app(), "Which region lost the most customers?")
        assert SERVER_KEY not in everything_rendered(at)
        assert SERVER_KEY not in repr(at.session_state.filtered_state)
        assert [e.value for e in at.error] == [UNAVAILABLE_MESSAGE]

    def test_server_key_is_used_server_side(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        ask(start_app(), "q")
        assert fake_agent.settings_seen[0].gemini_api_key == SERVER_KEY


# ---------------------------------------------------------------------------
# Initial provider and model come from configuration
# ---------------------------------------------------------------------------
class TestInitialSelection:
    def test_provider_and_model_come_from_settings(self, demo_env: None) -> None:
        at = start_app()
        assert at.selectbox(key="provider").value == "gemini"
        assert at.selectbox(key="model_gemini").value == "gemini-3.5-flash-lite"

    def test_defaults_without_configuration(self) -> None:
        at = start_app()
        assert at.selectbox(key="provider").value == "anthropic"
        assert (
            at.selectbox(key="model_anthropic").value
            == PROVIDERS["anthropic"].models[0]
        )

    def test_configured_model_outside_the_list_is_still_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_PROVIDER", "gemini")
        monkeypatch.setenv("AGENTCREW_MODEL", "gemini-9-experimental")
        at = start_app()
        assert at.selectbox(key="model_gemini").value == "gemini-9-experimental"

    def test_test_only_provider_falls_back_to_a_selectable_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_PROVIDER", "fake")
        at = start_app()
        assert at.selectbox(key="provider").value == "anthropic"


# ---------------------------------------------------------------------------
# Per-session allowance on the server key
# ---------------------------------------------------------------------------
class TestDemoRunAllowance:
    def test_counter_shown_while_server_key_in_use(self, demo_env: None) -> None:
        at = start_app()
        assert "Demo runs left: 5 of 5" in sidebar_captions(at)

    def test_sixth_run_is_refused_with_the_limit_message(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        for n in range(1, 6):
            at = ask(at, f"question {n}")
            assert f"Demo runs left: {5 - n} of 5" in sidebar_captions(at)

        at = ask(at, "question 6")
        assert len(fake_agent.settings_seen) == 5, "the 6th run must not reach the agent"
        assert LIMIT_MESSAGE in [w.value for w in at.warning]

    def test_quota_refusal_is_not_counted(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = QuotaExhaustedError(
            "Gemini quota/rate limit: 429 RESOURCE_EXHAUSTED"
        )
        at = ask(start_app(), "q")
        assert QUOTA_MESSAGE in [w.value for w in at.warning]
        assert "Demo runs left: 5 of 5" in sidebar_captions(at)

    def test_a_run_that_raises_is_not_counted(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMError("Gemini call failed: 503 overloaded")
        at = ask(start_app(), "q")
        assert "Demo runs left: 5 of 5" in sidebar_captions(at)

    def test_a_completed_run_counts_even_without_an_answer(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        # The agent ran to the end and gave up: that spent the model calls.
        fake_agent.outcome = replace(
            stub_outcome("q"), status="failed", answer=None, stop_reason="budget"
        )
        at = ask(start_app(), "q")
        assert "Demo runs left: 4 of 5" in sidebar_captions(at)

    def test_server_key_failure_shows_only_the_generic_message(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMError("Gemini call failed: 503 overloaded")
        at = ask(start_app(), "q")
        assert [e.value for e in at.error] == [UNAVAILABLE_MESSAGE]
        assert "503 overloaded" not in everything_rendered(at)

    def test_own_key_failure_keeps_the_redacted_error(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.error = LLMError(f"Gemini call failed: 400 bad key {OWN_KEY}")
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        at = ask(at, "q")
        # Exact match proves the key was replaced. (The key itself is still in
        # the visitor's own password box, where they typed it.)
        assert [e.value for e in at.error] == [
            "Run failed: LLMError: Gemini call failed: 400 bad key ***"
        ]

    def test_missing_server_key_is_not_counted(
        self, monkeypatch: pytest.MonkeyPatch, fake_agent: FakeAgent
    ) -> None:
        monkeypatch.setenv("AGENTCREW_PROVIDER", "gemini")
        at = ask(start_app(), "q")
        assert fake_agent.settings_seen == []
        assert any("Use your own API key" in w.value for w in at.warning)
        assert not any(c.startswith("Demo runs left") for c in sidebar_captions(at))

    def test_own_key_is_never_capped(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        assert not any(c.startswith("Demo runs left") for c in sidebar_captions(at))
        for n in range(7):
            at = ask(at, f"question {n}")
        assert len(fake_agent.settings_seen) == 7
        assert all(s.gemini_api_key == OWN_KEY for s in fake_agent.settings_seen)
        assert LIMIT_MESSAGE not in [w.value for w in at.warning]


# ---------------------------------------------------------------------------
# Budgets are fixed while the server key is in use
# ---------------------------------------------------------------------------
class TestDemoBudgets:
    def test_sliders_are_disabled_at_the_fixed_values(self, demo_env: None) -> None:
        at = start_app()
        assert [(s.value, s.proto.disabled) for s in at.sidebar.slider] == [
            (3, True), (3, True)
        ]
        assert FIXED_CAPTION in sidebar_captions(at)

    def test_demo_run_ignores_slider_values_sent_by_the_browser(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        # Widget values come from the client, so a tampered browser could send
        # 5/5 for a disabled slider. The server must still use 3/3.
        at = start_app()
        for slider in at.sidebar.slider:
            slider.set_value(5)
        at = ask(at, "q")
        seen = fake_agent.settings_seen[0]
        assert (seen.max_attempts_per_step, seen.max_steps) == (3, 3)

    def test_own_key_unlocks_the_sliders(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        assert not any(s.proto.disabled for s in at.sidebar.slider)
        assert FIXED_CAPTION not in sidebar_captions(at)

        for slider in at.sidebar.slider:
            slider.set_value(5)
        at = ask(at, "q")
        seen = fake_agent.settings_seen[0]
        assert (seen.max_attempts_per_step, seen.max_steps) == (5, 5)


# ---------------------------------------------------------------------------
# A quota refusal stops the agent; it is never "repaired"
# ---------------------------------------------------------------------------
class QuotaAtStage(ScriptedAnalyst):
    """A scripted analyst whose provider refuses with 429 at one stage."""

    def __init__(self, stage: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stage = stage

    def __call__(self, system: str, user: str) -> dict[str, Any] | str:
        response = super().__call__(system, user)
        if self.calls[-1] == self.stage:
            raise QuotaExhaustedError("Gemini quota/rate limit: 429 RESOURCE_EXHAUSTED")
        return response


class TestQuotaStopsTheAgent:
    def test_quota_on_the_first_call_stops_immediately(self, make_deps, settings) -> None:
        analyst = QuotaAtStage("plan")
        with pytest.raises(QuotaExhaustedError):
            run_question("q", settings=settings, deps=make_deps(analyst))
        assert analyst.calls == ["plan"]

    def test_quota_after_a_sql_failure_is_not_repaired_again(
        self, make_deps, settings
    ) -> None:
        # The first query fails (missing table), so the graph asks for a
        # repair - and the provider refuses that call. The run must end there,
        # not treat the refusal as another failed attempt to repair.
        analyst = QuotaAtStage(
            "repair", default_sql="SELECT nope FROM table_that_does_not_exist"
        )
        with pytest.raises(QuotaExhaustedError):
            run_question("q", settings=settings, deps=make_deps(analyst))
        assert analyst.calls == ["plan", "author", "repair"]

    def test_gemini_429_raises_quota_error_after_one_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = GeminiClient("AQ.test-key-not-real", "gemini-3.5-flash-lite")
        attempts: list[dict[str, Any]] = []

        def refuse(**kwargs: Any) -> None:
            attempts.append(kwargs)
            raise genai_errors.ClientError(
                429,
                {"error": {"code": 429, "message": "Resource has been exhausted.",
                           "status": "RESOURCE_EXHAUSTED"}},
            )

        monkeypatch.setattr(client._client.models, "generate_content", refuse)
        with pytest.raises(QuotaExhaustedError):
            client.complete(system="s", user="u")
        assert len(attempts) == 1
