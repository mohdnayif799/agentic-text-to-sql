"""The idle guard: an idle browser tab must not keep the server alive.

Streamlit holds one websocket per open tab and its client reconnects on its
own, so Cloud Run's request timeout never frees the instance while a tab is
open. The guard has two halves:

* in the browser, a script that leaves the app for a static page after
  `idle_timeout_seconds` without input, which closes the connection; AppTest
  has no browser, so these tests check the script is rendered with the
  configured timeout and that its target page exists and ships in the image;
* on the server, a last-activity stamp per session: a request that arrives
  after the timeout finds the conversation cleared and a notice shown, and
  the agent is not called for stale state.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from ui_harness import FakeAgent, ask, everything_rendered, start_app

ROOT = Path(__file__).resolve().parents[1]


def _load_app():
    spec = importlib.util.spec_from_file_location("idle_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idle_app"] = module
    spec.loader.exec_module(module)
    return module


app = _load_app()


class TestServerSide:
    def test_fresh_session_is_not_expired(self) -> None:
        assert not app.session_expired({}, now=1000.0, timeout=600)

    def test_expires_after_the_timeout(self) -> None:
        state = {app.LAST_ACTIVITY: 1000.0}
        assert not app.session_expired(state, now=1599.0, timeout=600)
        assert app.session_expired(state, now=1601.0, timeout=600)

    def test_touch_records_now(self) -> None:
        state: dict = {}
        app.touch(state, now=42.0)
        assert state[app.LAST_ACTIVITY] == 42.0

    def test_idle_timeout_comes_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert app.idle_timeout() == 600
        monkeypatch.setenv("AGENTCREW_IDLE_TIMEOUT_SECONDS", "120")
        assert app.idle_timeout() == 120

    def test_idle_timeout_survives_bad_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_IDLE_TIMEOUT_SECONDS", "soon")
        assert app.idle_timeout() == 600


class TestInTheApp:
    def test_stale_session_is_cleared_and_told(self, fake_agent: FakeAgent) -> None:
        at = start_app()
        at.text_input(key="api_key_anthropic").set_value("sk-ant-own").run()
        at = ask(at, "How many customers?")
        assert fake_agent.questions == ["How many customers?"]
        assert len(at.session_state["turns"]) == 1

        at.session_state[app.LAST_ACTIVITY] = time.monotonic() - 601
        at.run()
        assert at.session_state["turns"] == []
        assert any(app.IDLE_MESSAGE in w.value for w in at.warning)
        assert fake_agent.questions == ["How many customers?"], "no run for stale state"
        # The session is usable again without a reload.
        assert time.monotonic() - at.session_state[app.LAST_ACTIVITY] < 60

    def test_active_session_keeps_its_turns(self, fake_agent: FakeAgent) -> None:
        at = start_app()
        at.text_input(key="api_key_anthropic").set_value("sk-ant-own").run()
        at = ask(at, "q")
        at.run()
        assert len(at.session_state["turns"]) == 1
        assert not any(app.IDLE_MESSAGE in w.value for w in at.warning)

    def test_browser_guard_is_rendered_with_the_timeout(self) -> None:
        at = start_app()
        page = everything_rendered(at)
        assert app.IDLE_PAGE in page
        assert "600 * 1000" in page

    def test_browser_guard_follows_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_IDLE_TIMEOUT_SECONDS", "120")
        page = everything_rendered(start_app())
        assert "120 * 1000" in page


class TestIdlePage:
    def test_static_page_exists_and_links_home(self) -> None:
        html = (ROOT / "static" / "idle.html").read_text("utf-8")
        assert 'href="/"' in html
        assert "<script" not in html, "the page must not talk to the server"

    def test_guard_targets_the_served_path(self) -> None:
        # Streamlit serves static/ at /app/static/ (server.enableStaticServing).
        assert app.IDLE_PAGE == "/app/static/idle.html"
        assert (ROOT / "static" / "idle.html").exists()

    def test_image_ships_the_static_folder(self) -> None:
        assert "COPY static/ static/" in (ROOT / "Dockerfile").read_text()
