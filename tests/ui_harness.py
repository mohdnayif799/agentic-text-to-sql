"""Helpers for driving app.py through Streamlit's AppTest.

Shared by test_demo_mode.py and test_chat_ui.py. The matching fixtures
(`fake_agent`, `demo_env`) live in conftest.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

from agentcrew.graph.build import RunOutcome

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")

SERVER_KEY = "AQ.server-side-demo-key-that-must-never-render-0123456789"
OWN_KEY = "AQ.a-visitors-own-key-0123456789"


def start_app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=60).run()


def ask(at: AppTest, question: str) -> AppTest:
    """Submit a question the way a visitor does."""
    at.chat_input[0].set_value(question)
    return at.run()


def all_nodes(node: Any) -> list[Any]:
    found = [node]
    for child in getattr(node, "children", {}).values():
        found.extend(all_nodes(child))
    return found


def everything_rendered(at: AppTest) -> str:
    """Every element's protobuf plus every widget's current value, as text.

    The protobuf is what Streamlit actually sends to the browser, so this is
    the widest net available: labels, defaults, placeholders, help text,
    markdown bodies and error messages all end up in it.
    """
    parts = []
    for node in all_nodes(at._tree):
        parts.append(str(getattr(node, "proto", "")))
        try:
            parts.append(repr(node.value))
        except (AttributeError, KeyError):
            # Non-widget elements (charts, tables) have no widget state; AppTest
            # raises KeyError looking it up. Their proto above covers them.
            pass
    return "\n".join(parts)


def sidebar_captions(at: AppTest) -> list[str]:
    return [c.value for c in at.sidebar.caption]


def stub_outcome(question: str) -> RunOutcome:
    return RunOutcome(
        question=question,
        status="done",
        answer="Stub answer.",
        key_numbers=[],
        caveats=[],
        steps=[],
        step_results=[],
        selected_tables=[],
        schema_debug={},
        stop_reason="none",
        trace={
            "counters": {"sql_executions": 0, "repairs": 0, "llm_calls": 1},
            "elapsed_seconds": 0.1,
            "spans": [
                {"name": "plan", "kind": "node", "duration_ms": 1.0, "ok": True,
                 "data": {}}
            ],
        },
        trace_path=None,
        state={},  # type: ignore[arg-type]
    )


class FakeAgent:
    """Stands in for the agent so no database, model or network is needed.

    Records the Settings and question of every run, which is how the tests
    see which key a run actually used.
    """

    def __init__(self) -> None:
        self.settings_seen: list[Any] = []
        self.questions: list[str] = []
        self.error: Exception | None = None
        self.outcome: RunOutcome | None = None

    def build_deps(self, settings: Any, **_: Any) -> object:
        return object()

    def run_question(self, question: str, *, settings: Any, deps: Any,
                     **_: Any) -> RunOutcome:
        self.settings_seen.append(settings)
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.outcome or stub_outcome(question)
