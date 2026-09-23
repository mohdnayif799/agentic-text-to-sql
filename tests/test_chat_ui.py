"""Chat layout: bubbles, example chips, sidebar structure and theme.

Rendered through Streamlit's AppTest with the agent replaced by FakeAgent,
so these run offline in about a second each. The reply content itself (answer,
key figures, SQL, table, chart) must survive the layout change unchanged,
which `test_full_reply_is_still_rendered` pins down.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from ui_harness import (
    FakeAgent,
    ask,
    everything_rendered,
    sidebar_captions,
    start_app,
    stub_outcome,
)

from agentcrew.schemas import ExecutionResult, StepResult

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_COUNT = 6

# Emoji and pictograph blocks, plus the variation selector that turns a plain
# character into an emoji.
EMOJI = re.compile("[☀-➿⬀-⯿\U0001f000-\U0001faff️]")


def example_buttons(at: AppTest) -> list:
    return [b for b in at.button if (b.key or "").startswith("example_")]


def user_bubbles(at: AppTest) -> list[str]:
    return [m.value for m in at.markdown if 'class="user-msg"' in m.value]


def outcome_with_one_step() -> object:
    result = ExecutionResult(
        ok=True,
        sql="SELECT region, n FROM t",
        columns=["region", "n"],
        rows=[["APAC", 44], ["EMEA", 11]],
        row_count=2,
    )
    step = StepResult(
        goal="Count lost customers per region",
        sql="SELECT region, n FROM t",
        result=result,
        attempts_used=1,
        verified=True,
    )
    return replace(
        stub_outcome("q"),
        answer="APAC lost the most customers.",
        key_numbers=["APAC: 44 lost customers"],
        caveats=["Churn is inferred from churn_date."],
        step_results=[step],
        selected_tables=["customers", "regions"],
    )


# ---------------------------------------------------------------------------
# Conversation layout
# ---------------------------------------------------------------------------
class TestConversation:
    def test_question_is_a_right_aligned_bubble(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = ask(start_app(), "Which region lost the most customers?")
        assert len(user_bubbles(at)) == 1
        assert 'class="user-msg-row"' in user_bubbles(at)[0]
        assert not at.chat_message, "replies must not use st.chat_message (avatar)"

    def test_bubble_shows_the_question_exactly_as_typed(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = ask(start_app(), "<b>bold?</b> & __init__.py")
        assert "&lt;b&gt;bold?&lt;/b&gt; &amp; __init__.py" in user_bubbles(at)[0]

    def test_turns_accumulate_in_order(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = ask(ask(start_app(), "first"), "second")
        bubbles = user_bubbles(at)
        assert len(bubbles) == 2
        assert "first" in bubbles[0] and "second" in bubbles[1]

    def test_full_reply_is_still_rendered(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.outcome = outcome_with_one_step()
        at = ask(start_app(), "q")
        assert [s.value for s in at.success] == ["APAC lost the most customers."]
        assert any("APAC: 44 lost customers" in m.value for m in at.markdown)
        assert [w.value for w in at.warning] == ["Churn is inferred from churn_date."]
        assert [c.value for c in at.code] == ["SELECT region, n FROM t"]
        assert len(at.dataframe) == 1
        assert len(at.get("vega_lite_chart")) == 1
        assert [m.label for m in at.metric] == [
            "Steps", "SQL runs", "Repairs", "LLM calls", "Time"
        ]

    def test_caption_says_questions_are_independent(self) -> None:
        at = start_app()
        assert any("answered independently" in c.value for c in at.main.caption)


# ---------------------------------------------------------------------------
# Example chips
# ---------------------------------------------------------------------------
class TestExampleChips:
    def test_shown_while_the_conversation_is_empty(self) -> None:
        assert len(example_buttons(start_app())) == EXAMPLE_COUNT

    def test_one_click_runs_the_example(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        label = example_buttons(at)[0].label
        at.button(key="example_0").click().run()
        assert fake_agent.questions == [label]
        assert label in user_bubbles(at)[0]

    def test_hidden_once_the_conversation_has_started(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = ask(start_app(), "q")
        assert example_buttons(at) == []


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
class TestSidebar:
    def test_expanders_are_collapsed_and_hold_the_right_controls(self) -> None:
        at = start_app()
        own_key, advanced = at.sidebar.expander
        assert (own_key.label, advanced.label) == (
            "Use your own API key", "Advanced settings"
        )
        assert not own_key.proto.expanded and not advanced.proto.expanded
        assert [s.key for s in own_key.selectbox] == ["provider"]
        assert len(own_key.text_input) == 1
        assert [s.label for s in advanced.slider] == [
            "Max attempts per step", "Max analysis steps"
        ]
        assert any(s.label == "Model" for s in advanced.selectbox)

    def test_new_chat_clears_the_conversation_but_not_the_run_count(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = ask(start_app(), "q")
        assert "Demo runs left: 4 of 5" in sidebar_captions(at)

        at.button(key="new_chat").click().run()
        assert user_bubbles(at) == []
        assert len(example_buttons(at)) == EXAMPLE_COUNT
        assert "Demo runs left: 4 of 5" in sidebar_captions(at)


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
class TestStyling:
    def test_no_emoji_or_icons_anywhere(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.outcome = outcome_with_one_step()
        at = ask(start_app(), "q")
        rendered = everything_rendered(at)
        assert not EMOJI.search(rendered)
        assert ":material/" not in rendered
        assert "page_icon" not in (ROOT / "app.py").read_text(encoding="utf-8")

    def test_theme_file_matches_the_brief(self) -> None:
        config = tomllib.loads(
            (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        )
        assert config["theme"] == {
            "base": "light",
            "primaryColor": "#2563EB",
            "backgroundColor": "#FFFFFF",
            "secondaryBackgroundColor": "#EFF6FF",
            "textColor": "#1E293B",
        }
        assert config["client"] == {
            "toolbarMode": "minimal", "showErrorDetails": "none"
        }

    def test_charts_use_the_theme_blue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = importlib.util.spec_from_file_location("chat_ui_app", ROOT / "app.py")
        app = importlib.util.module_from_spec(spec)
        # dataclasses look their module up in sys.modules while executing.
        monkeypatch.setitem(sys.modules, "chat_ui_app", app)
        spec.loader.exec_module(app)  # main() only runs under __main__

        config = tomllib.loads(
            (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        )
        df = pd.DataFrame([["APAC", 44], ["EMEA", 11]], columns=["region", "n"])
        chart = app.build_chart(df)
        assert chart is not None
        assert chart.to_dict()["mark"]["color"] == config["theme"]["primaryColor"]
