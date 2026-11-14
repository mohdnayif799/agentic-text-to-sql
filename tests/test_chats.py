"""Several chats per session (agentcrew/chats.py).

The first half tests the chat list on a plain dict. The second drives the
real app through AppTest (FakeAgent instead of the agent, a scripted client
for description drafts), with real uploads, the real dataset store and the
real SQL editor path: histories, per-chat data sources, deleting a chat, the
10-chat limit, and that "New chat" never resets a session limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from ui_harness import (
    FakeAgent,
    FakeRuntime,
    ask,
    chat_turns,
    everything_rendered,
    main_notes,
    open_chat,
    start_app,
    stub_arch_run,
)

from agentcrew import chats, data_panel
from agentcrew.db.engine import read_only_engine
from agentcrew.llm import FakeClient
from agentcrew.schemas import ExecutionResult
from agentcrew.service import DEMO_LIMIT_MESSAGE, DEMO_RUN_LIMIT
from agentcrew.tools import ToolContext

CSV = b"student_id,dept,score\n1,CS,80\n2,EE,70\n3,CS,90\n"
UPLOAD_SQL = "SELECT dept, COUNT(*) AS n FROM marks GROUP BY dept ORDER BY dept"
NORTHSTAR_SQL = "SELECT segment FROM customers GROUP BY segment ORDER BY segment"


@dataclass(frozen=True)
class Q:
    """Stands in for a Turn: chats.title reads only the question."""

    question: str


# ---------------------------------------------------------------------------
# The chat list on a plain dict
# ---------------------------------------------------------------------------
class TestChatList:
    def test_a_session_starts_with_one_empty_chat(self) -> None:
        state: dict = {}
        chat = chats.current(state)
        assert chat["id"] == "c1" and chat[chats.TURNS] == []
        assert chats.title(chat) == chats.UNTITLED
        assert [c["id"] for c in chats.all_chats(state)] == ["c1"]

    def test_new_chat_keeps_the_old_one(self) -> None:
        state: dict = {}
        chats.current(state)[chats.TURNS] = [Q("first")]
        assert chats.start_new(state) == []
        assert [c["id"] for c in chats.all_chats(state)] == ["c1", "c2"]
        assert chats.current(state)["id"] == "c2"
        assert chats.current(state)[chats.TURNS] == []
        assert chats.switch(state, "c1")
        assert chats.current(state)[chats.TURNS] == [Q("first")]

    def test_new_chat_on_an_empty_chat_replaces_it(self) -> None:
        state: dict = {}
        chats.current(state)
        dropped = chats.start_new(state)
        assert [c["id"] for c in dropped] == ["c1"]
        assert [c["id"] for c in chats.all_chats(state)] == ["c2"]
        assert chats.eviction_notice(dropped) is None, "nothing worth a notice"

    def test_the_eleventh_chat_removes_the_oldest_with_a_notice(self) -> None:
        state: dict = {}
        for n in range(chats.MAX_CHATS):
            chats.current(state)[chats.TURNS] = [Q(f"question {n}")]
            if n == 0:
                chats.current(state)[chats.DATASET_ID] = "d0"
            dropped = chats.start_new(state)
            assert dropped == [] or n == chats.MAX_CHATS - 1
        assert len(chats.all_chats(state)) == chats.MAX_CHATS
        assert [c["id"] for c in dropped] == ["c1"]
        notice = chats.eviction_notice(dropped)
        assert notice == ('Only 10 chats are kept, so the oldest one ("question 0") '
                          "was removed, with its uploaded data.")
        assert chats.all_chats(state)[0]["id"] == "c2"

    def test_switch_to_an_unknown_chat_is_refused(self) -> None:
        state: dict = {}
        chats.current(state)
        assert not chats.switch(state, "c99")
        assert chats.current(state)["id"] == "c1"

    def test_delete_the_open_chat_shows_the_newest_left(self) -> None:
        state: dict = {}
        for name in ("a", "b", "c"):
            chats.current(state)[chats.TURNS] = [Q(name)]
            chats.start_new(state)
        chats.switch(state, "c2")
        gone = chats.delete(state, "c2")
        assert gone is not None and gone[chats.TURNS] == [Q("b")]
        assert [c["id"] for c in chats.all_chats(state)] == ["c1", "c3", "c4"]
        assert chats.current(state)["id"] == "c4"
        assert chats.delete(state, "c2") is None

    def test_deleting_the_last_chat_leaves_a_new_one(self) -> None:
        state: dict = {}
        chats.current(state)[chats.TURNS] = [Q("only")]
        chats.delete(state, "c1")
        assert [c["id"] for c in chats.all_chats(state)] == ["c2"]
        assert chats.current(state)[chats.TURNS] == []

    def test_reset_drops_every_chat(self) -> None:
        state: dict = {}
        chats.current(state)[chats.TURNS] = [Q("a")]
        chats.start_new(state)
        dropped = chats.reset(state)
        assert [c["id"] for c in dropped] == ["c1", "c2"]
        assert [c["id"] for c in chats.all_chats(state)] == ["c3"]

    def test_title_is_the_first_question_cut_short(self) -> None:
        chat = {chats.TURNS: [Q("  How   many\ncustomers? "), Q("later")]}
        assert chats.title(chat) == "How many customers?"
        long = {chats.TURNS: [Q("x" * 60)]}
        assert chats.title(long) == "x" * chats.TITLE_CHARS + "..."

    def test_a_question_being_asked_titles_an_untitled_chat(self) -> None:
        assert chats.title({chats.TURNS: []}, "Revenue?") == "Revenue?"
        assert chats.title({chats.TURNS: [Q("first")]}, "second") == "first"
        assert chats.title({chats.TURNS: []}, None) == chats.UNTITLED


class TestChatState:
    """The view data_panel works on: chat keys in the chat, the rest (every
    limit) in the session."""

    def test_keys_go_to_the_right_place(self) -> None:
        state: dict = {}
        view = chats.view(state)
        view[data_panel.DATASET_ID] = "d1"
        view[data_panel.UPLOADS_USED] = 2
        view[data_panel.OWNER] = "o"
        assert chats.current(state)[chats.DATASET_ID] == "d1"
        assert state[data_panel.UPLOADS_USED] == 2 and state[data_panel.OWNER] == "o"
        assert data_panel.DATASET_ID not in state

    def test_a_new_chat_sees_the_session_counts_but_not_the_data(self) -> None:
        state: dict = {"demo_runs_used": 4, "sql_runs_used": 30}
        view = chats.view(state)
        view[data_panel.DATASET_ID] = "d1"
        view[data_panel.UPLOADS_USED] = 3
        chats.current(state)[chats.TURNS] = [Q("q")]
        chats.start_new(state)
        assert view.get(data_panel.DATASET_ID) is None
        assert view[data_panel.UPLOADS_USED] == 3
        assert view["demo_runs_used"] == 4 and view["sql_runs_used"] == 30

    def test_every_per_chat_key_is_a_data_panel_key(self) -> None:
        assert {data_panel.DATASET_ID, data_panel.DATA_CHOICE,
                data_panel.UPLOAD_SEEN} <= chats.CHAT_KEYS
        assert data_panel.UPLOADS_USED not in chats.CHAT_KEYS
        assert data_panel.OWNER not in chats.CHAT_KEYS
        for limit_key in ("demo_runs_used", "sql_runs_used"):
            assert limit_key not in chats.CHAT_KEYS


# ---------------------------------------------------------------------------
# In the app
# ---------------------------------------------------------------------------
@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "uploads"
    monkeypatch.setenv("AGENTCREW_UPLOAD_DIR", str(root))
    return root


@pytest.fixture
def agent(fake_agent: FakeAgent, demo_env: None, uploads: Path, db_path: Path,
          monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    """FakeAgent whose runtime carries the real ToolContext of the source it
    is built for, so the SQL editor runs on NorthStar or on the upload; and
    a scripted client for the description draft."""
    northstar = ToolContext.create(read_only_engine(db_path), max_rows=25)

    def build_runtime(settings: Any, *_: Any, source: Any = None,
                      **__: Any) -> FakeRuntime:
        fake_agent.sources_seen.append(source)
        return FakeRuntime(northstar if source is None
                           else source.tool_context(settings))

    monkeypatch.setattr("agentcrew.archs.app_runner.build_runtime", build_runtime)
    monkeypatch.setattr("agentcrew.llm.build_client",
                        lambda s: FakeClient(responder=lambda a, b: {"tables": []}))
    return fake_agent


def answer_with(agent: FakeAgent, sql: str) -> None:
    agent.arch_run = stub_arch_run("q", result=ExecutionResult(
        ok=True, sql=sql, columns=["x"], rows=[["stub"]], row_count=1))


def radio(at: Any) -> Any:
    return at.radio(key=f"{data_panel.DATA_CHOICE}_{open_chat(at)['id']}")


def upload_in_chat(at: Any, name: str = "marks.csv", data: bytes = CSV) -> Any:
    radio(at).set_value(data_panel.UPLOAD).run()
    at.sidebar.file_uploader[0].set_value((name, data, "text/csv")).run()
    return at


def new_chat(at: Any) -> Any:
    return at.button(key="new_chat").click().run()


def open_(at: Any, chat_id: str) -> Any:
    return at.button(key=f"chat_{chat_id}").click().run()


def bubbles(at: Any) -> list[str]:
    return [m.value for m in at.main.markdown if 'class="user-msg"' in m.value]


def run_editor(at: Any, sql: str, slot: int = 0) -> Any:
    chat_id = open_chat(at)["id"]
    at.text_area(key=f"sql_text_{chat_id}_{slot}").set_value(sql)
    at.button(key=f"sql_run_{chat_id}_{slot}").click()
    return at.run()


def store_of() -> Any:
    from agentcrew.config import Settings

    return data_panel.store(Settings())


class TestChatsInTheApp:
    def test_chats_keep_their_histories(self, agent: FakeAgent) -> None:
        at = ask(start_app(), "How many customers?")
        at = ask(new_chat(at), "Revenue in 2024?")
        assert [b.label for b in at.sidebar.button
                if b.key.startswith("chat_")] == ["Revenue in 2024?",
                                                   "How many customers?"]
        assert len(bubbles(at)) == 1 and "Revenue in 2024?" in bubbles(at)[0]

        at = open_(at, "c1")
        assert [t.question for t in chat_turns(at)] == ["How many customers?"]
        assert len(bubbles(at)) == 1 and "How many customers?" in bubbles(at)[0]
        assert any(m.value == "Stub answer." for m in at.markdown), "answer kept"
        assert at.dataframe, "the answer's table is kept"
        at = open_(at, "c2")
        assert [t.question for t in chat_turns(at)] == ["Revenue in 2024?"]
        assert not at.exception

    def test_chat_titles_are_shown_as_typed(self, agent: FakeAgent) -> None:
        at = ask(start_app(), "Sum of *net_amount* in [2024]")
        (button,) = [b for b in at.sidebar.button if b.key == "chat_c1"]
        assert button.label == r"Sum of \*net\_amount\* in \[2024\]"

    def test_a_new_chat_starts_on_the_default_source(self, agent: FakeAgent) -> None:
        at = upload_in_chat(start_app())
        assert open_chat(at)[data_panel.DATASET_ID]
        at = new_chat(at)
        assert open_chat(at)[data_panel.DATASET_ID] is None
        assert radio(at).value == data_panel.NORTHSTAR
        at = ask(at, "q")
        assert agent.sources_seen[-1] is None, "NorthStar, not chat 1's upload"
        assert "Answering from" not in everything_rendered(at)

    def test_switching_back_restores_the_chats_dataset(self, agent: FakeAgent) -> None:
        at = upload_in_chat(start_app())
        dataset_id = open_chat(at)[data_panel.DATASET_ID]
        answer_with(agent, UPLOAD_SQL)
        at = ask(at, "Students per department?")
        assert agent.sources_seen[-1].label == "marks.csv"

        answer_with(agent, NORTHSTAR_SQL)
        at = ask(new_chat(at), "Customer segments?")
        assert agent.sources_seen[-1] is None

        at = open_(at, "c1")
        assert radio(at).value == data_panel.UPLOAD
        assert open_chat(at)[data_panel.DATASET_ID] == dataset_id
        assert "Answering from marks.csv" in everything_rendered(at)
        answer_with(agent, UPLOAD_SQL)
        at = ask(at, "And the average score?")
        assert agent.sources_seen[-1].label == "marks.csv"

    def test_sql_editors_run_on_their_own_chats_source(self, agent: FakeAgent) -> None:
        at = upload_in_chat(start_app())
        answer_with(agent, UPLOAD_SQL)
        at = run_editor(ask(at, "Students per department?"), UPLOAD_SQL)
        assert not at.error, [e.value for e in at.error]
        assert at.main.dataframe[-1].value.values.tolist() == [["CS", 2], ["EE", 1]]

        answer_with(agent, NORTHSTAR_SQL)
        at = run_editor(ask(new_chat(at), "Customer segments?"), NORTHSTAR_SQL)
        assert not at.error, [e.value for e in at.error]
        assert at.main.dataframe[-1].value.shape[0] == 3

        at = open_(at, "c1")
        assert at.text_area(key="sql_text_c1_0").value == UPLOAD_SQL
        assert at.main.dataframe[-1].value.values.tolist() == [["CS", 2], ["EE", 1]], (
            "the last run's result is still under the answer")
        at = run_editor(at, "SELECT COUNT(*) AS n FROM marks")
        assert at.main.dataframe[-1].value.values.tolist() == [[3]]
        assert at.session_state["sql_runs_used"] == 3

    def test_an_editor_whose_dataset_is_gone_is_read_only(self,
                                                          agent: FakeAgent) -> None:
        at = upload_in_chat(start_app())
        answer_with(agent, UPLOAD_SQL)
        at = ask(at, "Students per department?")
        at = at.button(key="remove_data_c1").click().run().run()  # past st.rerun
        import app as app_module

        assert app_module.SQL_OTHER_SOURCE in main_notes(at)
        assert not [t for t in at.text_area if t.key == "sql_text_c1_0"]

    def test_deleting_a_chat_deletes_its_dataset(self, agent: FakeAgent,
                                                 uploads: Path) -> None:
        at = upload_in_chat(start_app())
        kept = new_chat(at)
        kept = upload_in_chat(kept, name="other.csv", data=b"a,b\n1,2\n")
        first, second = (c[data_panel.DATASET_ID]
                         for c in kept.session_state[chats.CHATS])
        at = open_(kept, "c1").button(key="delete_chat").click().run()
        assert not (uploads / first).exists(), "the deleted chat's data is gone"
        assert (uploads / second).exists(), "the other chat's data is not"
        assert [c["id"] for c in at.session_state[chats.CHATS]] == ["c2"]
        assert open_chat(at)[data_panel.DATASET_ID] == second

    def test_the_eleventh_chat_removes_the_oldest(self, agent: FakeAgent,
                                                  uploads: Path,
                                                  monkeypatch: pytest.MonkeyPatch
                                                  ) -> None:
        at = start_app()
        at.text_input(key="api_key_gemini").set_value("AQ.own-key-0123456789").run()
        at = upload_in_chat(at)
        first = open_chat(at)[data_panel.DATASET_ID]
        for n in range(chats.MAX_CHATS):
            at = ask(at, f"question {n}") if n else ask(at, "the first chat")
            at = new_chat(at)
        assert len(at.session_state[chats.CHATS]) == chats.MAX_CHATS
        assert "c1" not in [c["id"] for c in at.session_state[chats.CHATS]]
        assert any('("the first chat") was removed, with its uploaded data'
                   in i.value for i in at.info)
        assert not (uploads / first).exists()

    def test_a_full_store_refuses_and_keeps_other_chats_data(
        self, agent: FakeAgent, uploads: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        at = upload_in_chat(start_app())
        first = open_chat(at)[data_panel.DATASET_ID]
        store_of().max_bytes = store_of().total_bytes()  # full from now on
        at = upload_in_chat(new_chat(at), name="other.csv", data=b"a,b\n1,2\n")
        (error,) = [e.value for e in at.error]
        assert error.startswith("The demo is holding as much uploaded data")
        assert data_panel.STORE_FULL_OTHER_CHATS.strip() in error
        assert (uploads / first).exists(), "nothing was evicted"
        assert open_chat(at)[data_panel.DATASET_ID] is None


class TestNewChatCannotResetLimits:
    """Every limit is the session's: a new chat starts with the counts the
    session already used."""

    def test_demo_runs(self, agent: FakeAgent) -> None:
        at = start_app()
        for n in range(DEMO_RUN_LIMIT):
            at = new_chat(ask(at, f"q{n}"))
        assert len(agent.questions) == DEMO_RUN_LIMIT
        at = ask(at, "one more")
        assert len(agent.questions) == DEMO_RUN_LIMIT, "no sixth run"
        assert any(DEMO_LIMIT_MESSAGE in w.value for w in at.warning)

    def test_sql_runs(self, agent: FakeAgent,
                      monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCREW_SESSION_SQL_RUN_CAP", "1")
        answer_with(agent, NORTHSTAR_SQL)
        at = run_editor(ask(start_app(), "q"), "SELECT 1")
        at = run_editor(ask(new_chat(at), "q"), "SELECT 2")
        assert any("this session's SQL runs" in w.value for w in at.warning)
        assert at.session_state["sql_runs_used"] == 1

    def test_uploads(self, agent: FakeAgent) -> None:
        at = start_app()
        for n in range(data_panel.UPLOADS_PER_SESSION):
            at = upload_in_chat(at, name=f"f{n}.csv", data=f"a\n{n}\n".encode())
            at = new_chat(ask(at, "q"))
        at = upload_in_chat(at, name="extra.csv", data=b"a\n9\n")
        assert data_panel.UPLOAD_LIMIT_MESSAGE in [e.value for e in at.error]
        assert open_chat(at)[data_panel.DATASET_ID] is None
