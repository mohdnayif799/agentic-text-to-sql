"""Chat layout: bubbles, example chips, sidebar structure and theme.

Rendered through Streamlit's AppTest with the agent replaced by FakeAgent,
so these run offline in about a second each. The reply content itself (answer,
key figures, SQL, table, chart) must survive the layout change unchanged,
which `test_full_reply_is_still_rendered` pins down.
"""

from __future__ import annotations

import importlib.util
import json
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
    all_nodes,
    ask,
    everything_rendered,
    sidebar_captions,
    start_app,
    stub_arch_run,
    stub_outcome,
)

from agentcrew.schemas import ExecutionResult, StepResult
from evalrun.sets import load_questions

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_COUNT = 6
DELETE_EXAMPLE = "Delete all customers in the LATAM region."

# Where each example chip comes from, in chip order: its question set and id,
# the final run where D answered it correctly, and the path and a tool D took
# there. eval/runs/ is local (gitignored); results.json files under
# eval/results/ are committed.
EVALUATED = (
    ("holdout_easy", "he10", "eval/results/phase5/phase5-holdout-easy",
     "direct", "run_sql"),
    ("holdout_easy", "he02", "eval/results/phase5/phase5-holdout-easy",
     "direct", "run_sql"),
    ("dev2", "dv01", "eval/runs/phase4d-dev2", "escalated", "search_values"),
    ("holdout_agent", "ha09", "eval/results/phase5/phase5-holdout-agent",
     "escalated", "compute:decompose_change"),
    ("holdout_agent", "ha12", "eval/results/phase5/phase5-holdout-agent",
     "escalated", "compute:linear_trend_forecast"),
)

# Emoji and pictograph blocks, plus the variation selector that turns a plain
# character into an emoji.
EMOJI = re.compile("[☀-➿⬀-⯿\U0001f000-\U0001faff️]")


def example_buttons(at: AppTest) -> list:
    return [b for b in at.button if (b.key or "").startswith("example_")]


def chip_boxes(at: AppTest) -> list[list[str]]:
    """Each example chip's box, top to bottom: the chip's label, then the
    text of everything under it in the same box."""
    boxes = []
    for node in all_nodes(at._tree):
        kids = list(getattr(node, "children", {}).values())
        if kids and (getattr(kids[0], "key", None) or "").startswith("example_"):
            boxes.append([kids[0].label, *(k.value for k in kids[1:])])
    return boxes


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
        self, demo_env: None, fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original agent's reply (AGENTCREW_ARCH=v1)."""
        monkeypatch.setenv("AGENTCREW_ARCH", "v1")
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

    def test_each_chip_has_its_caption_under_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        examples = load_app(monkeypatch).EXAMPLES
        assert chip_boxes(start_app()) == [list(pair) for pair in examples]
        assert [shows for _, shows in examples] == [
            "answered directly",
            "answered directly",
            "escalates: fuzzy value search",
            "escalates: metric analysis",
            "escalates: trend forecast",
            "refused: read-only database",
        ]

    def test_examples_are_evaluated_questions_word_for_word(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every chip but the delete example is the exact text of a question
        in a frozen evaluation set; the delete example stays last."""
        questions = [q for q, _ in load_app(monkeypatch).EXAMPLES]
        expected = []
        for set_name, qid, *_ in EVALUATED:
            texts = {q["id"]: q["question"] for q in load_questions(set_name)}
            expected.append(texts[qid])
        assert questions == [*expected, DELETE_EXAMPLE]

    @pytest.mark.parametrize(("set_name", "qid", "run", "path", "tool"), EVALUATED)
    def test_d_answered_each_example_correctly_on_its_path(
        self, set_name: str, qid: str, run: str, path: str, tool: str
    ) -> None:
        results = ROOT / run / "results.json"
        if not results.exists():
            pytest.skip(f"{run} is a local run (eval/runs/ is gitignored)")
        payload = json.loads(results.read_text(encoding="utf-8"))
        assert payload["set"] == set_name and payload["complete"]
        (item,) = [i for i in payload["items"]
                   if i["id"] == qid and i["arch"] == "full"]
        assert item["end_to_end"] is True
        assert item["path"] == path
        assert tool in [t for step in item["tools"] for t in step]


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



# ---------------------------------------------------------------------------
# Architecture D (the default): path, tools per step, progress, log line
# ---------------------------------------------------------------------------
ESCALATED_STEPS = (
    {"step": 1, "tools": ["run_sql", "repair"], "sql": "SELECT 1", "reason": ""},
    {"step": 2, "tools": ["compute:decompose_change"],
     "sql": "-- compute: decompose_change", "reason": "split the metric"},
    {"step": "final", "tools": ["stack"], "plan": {"stacked_steps": [2]}},
)


def load_app(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("chat_ui_app_d", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "chat_ui_app_d", app)
    spec.loader.exec_module(app)  # main() only runs under __main__
    return app


class TestArchitectureD:
    def test_d_is_the_default_architecture(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        ask(start_app(), "q")
        assert fake_agent.settings_seen[-1].arch == "full"
        assert fake_agent.settings_seen[-1].max_steps == 3  # demo limits hold

    def test_direct_reply_says_so(self, demo_env: None, fake_agent: FakeAgent) -> None:
        at = ask(start_app(), "q")
        assert [s.value for s in at.success] == ["Stub answer."]
        assert any("Answered directly" in m.value for m in at.markdown)
        assert [m.label for m in at.metric] == [
            "Path", "Steps", "Repairs", "LLM calls", "Time"]
        assert at.metric[0].value == "Direct"
        assert [e.label for e in at.main.expander] == ["Step 1: run_sql"]

    def test_escalated_reply_shows_the_path_and_each_steps_tools(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.arch_run = stub_arch_run(
            "q", path="escalated", steps=ESCALATED_STEPS, answer="Orders drove it.")
        at = ask(start_app(), "q")
        assert at.metric[0].value == "Escalated"
        assert any(m.value.startswith("**Escalated:**") for m in at.markdown)
        assert [e.label for e in at.main.expander] == [
            "Step 1: run_sql + repair", "Step 2: compute:decompose_change"]
        assert len(at.dataframe) == 1 and len(at.get("vega_lite_chart")) == 1
        assert any(c.value == "Final table: the metric analyses below, stacked."
                   for c in at.main.caption)
        assert not EMOJI.search(everything_rendered(at))

    def test_progress_is_shown_only_once_a_question_escalates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = load_app(monkeypatch)
        shown: list[tuple[str, float]] = []
        report = app.stage_reporter(lambda text, f: shown.append((text, f)), 3)
        for stage in ("step 1: SQL (A's call)", "step 1: query", "follow-up decision"):
            report(stage)
        assert shown == []  # the direct path shows no progress bar
        for stage in ("step 1: verification", "step 2: metric plan",
                      "step 2: compute:decompose_change", "step 3: decide_next_action",
                      "final: answer"):
            report(stage)
        texts = [t for t, _ in shown]
        assert texts == [
            "Escalated: checking the first result",
            "Escalated: step 2 of 3, planning the metric analysis",
            "Escalated: step 2 of 3, decompose change",
            "Escalated: step 3 of 3, choosing the next step",
            "Writing the answer",
        ]
        fractions = [f for _, f in shown]
        assert fractions == sorted(fractions) and fractions[-1] == 1.0

    def test_progress_callback_reaches_the_run(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        fake_agent.stages = ["step 1: SQL (A's call)", "step 2: decide_next_action"]
        at = ask(start_app(), "q")
        assert [s.value for s in at.success] == ["Stub answer."]
        assert not at.get("progress")  # cleared once the answer is shown

    def test_log_line_records_path_tools_and_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging

        app = load_app(monkeypatch)
        lines: list[str] = []

        class Keep(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append(record.getMessage())

        handler = Keep()
        app.log.addHandler(handler)
        try:
            app._log_run({"provider": "gemini", "model": "m"}, "done", 0.0, 5,
                         arch="full", path="escalated",
                         tools=[["run_sql", "repair"], ["compute:decompose_change"]])
        finally:
            app.log.removeHandler(handler)
        (line,) = lines
        assert "arch=full" in line and "path=escalated" in line
        assert "tools=run_sql+repair>compute:decompose_change" in line
        assert "llm_calls=5" in line
