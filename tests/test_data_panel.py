"""The 'Your data' panel (agentcrew/data_panel.py) rendered in the real app
through AppTest. The agent is FakeAgent; the description call is a scripted
FakeClient, so no model or network is used."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from ui_harness import OWN_KEY, FakeAgent, ask, everything_rendered, start_app

from agentcrew import caps, data_panel
from agentcrew.archs import answer as answer_module
from agentcrew.context import values
from agentcrew.datasets import describe
from agentcrew.datasets.joins import Join
from agentcrew.datasets.store import dataset_store
from agentcrew.llm import FakeClient

CSV = b"student_id,dept,score\n1,CS,80\n2,EE,70\n3,CS,90\n"
DESCRIPTIONS = {"tables": [{"name": "marks", "description": "Exam marks",
                            "columns": [{"name": "score",
                                         "description": "Mark out of 100"}]}]}


@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "uploads"
    monkeypatch.setenv("AGENTCREW_UPLOAD_DIR", str(root))
    return root


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The description call: a scripted client; records each system prompt."""
    systems: list[str] = []

    def factory(settings: Any) -> FakeClient:
        def reply(system: str, user: str) -> dict:
            systems.append(system)
            return DESCRIPTIONS
        return FakeClient(responder=reply)

    monkeypatch.setattr("agentcrew.llm.build_client", factory)
    return systems


def choose_upload(at):
    at.radio(key=data_panel.DATA_CHOICE).set_value(data_panel.UPLOAD).run()
    return at


def upload(at, name: str = "marks.csv", data: bytes = CSV):
    at.sidebar.file_uploader[0].set_value((name, data, "text/csv")).run()
    return at


def store_of(at) -> Any:
    from agentcrew.config import Settings

    return dataset_store(Settings())


class TestPanel:
    def test_offers_northstar_and_upload(self, uploads: Path) -> None:
        at = start_app()
        assert at.radio(key=data_panel.DATA_CHOICE).options == [
            data_panel.NORTHSTAR, data_panel.UPLOAD]
        assert at.radio(key=data_panel.DATA_CHOICE).value == data_panel.NORTHSTAR

    def test_upload_choice_shows_the_privacy_note(self, uploads: Path) -> None:
        at = choose_upload(start_app())
        page = everything_rendered(at)
        assert "What the model sees" in page
        assert "not for sensitive or personal data" in page
        assert "15 minutes" in page

    def test_uploads_can_be_switched_off(self, uploads: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCREW_UPLOADS_ENABLED", "false")
        at = start_app()
        assert [e.label for e in at.sidebar.expander] == [
            "Use your own API key", "Advanced settings"]


class TestUpload:
    def test_upload_describe_and_ask(self, uploads: Path, demo_env: None,
                                     fake_agent: FakeAgent, model: list[str]) -> None:
        at = upload(choose_upload(start_app()))
        assert not at.exception
        assert any("Using marks.csv" in s.value for s in at.success)
        assert model and model[0].startswith("You document a database")
        (dataset,) = store_of(at).owned_by(at.session_state[data_panel.OWNER])
        assert dataset.notes.tables == {"marks": "Exam marks"}
        assert dataset.tables == ("marks",)

        at = ask(at, "Average score by department?")
        assert fake_agent.sources_seen[-1].kind == "upload"
        assert fake_agent.sources_seen[-1].label == "marks.csv"
        page = everything_rendered(at)
        assert "Answering from marks.csv" in page
        assert "How many customers signed up in 2025?" not in page  # no chips

    def test_several_csv_files_become_one_dataset_with_joins(
        self, uploads: Path, demo_env: None, fake_agent: FakeAgent,
        model: list[str],
    ) -> None:
        at = choose_upload(start_app())
        at.sidebar.file_uploader[0].set_value([
            ("students.csv", b"student_id,name\n1,A\n2,B\n3,C\n4,D\n", "text/csv"),
            ("marks.csv", CSV, "text/csv"),
        ]).run()
        (dataset,) = store_of(at).owned_by(at.session_state[data_panel.OWNER])
        assert dataset.tables == ("marks", "students")
        assert dataset.label == "students.csv and 1 more"
        assert [j.sql() for j in dataset.joins] == [
            "marks.student_id = students.student_id"]

    def test_excel_cannot_be_mixed_with_csv(
        self, uploads: Path, demo_env: None, fake_agent: FakeAgent,
        model: list[str],
    ) -> None:
        at = choose_upload(start_app())
        at.sidebar.file_uploader[0].set_value([
            ("a.csv", b"x\n1\n", "text/csv"), ("b.xlsx", b"PK", "application/x")]).run()
        assert any("only when all are CSV" in e.value for e in at.error)

    def test_the_description_call_charges_the_daily_cap(
        self, uploads: Path, demo_env: None, fake_agent: FakeAgent,
        model: list[str],
    ) -> None:
        upload(choose_upload(start_app()))
        snap = caps.daily_cap(None).snapshot()
        assert snap.uploads == 1 and snap.llm_calls == len(model) == 1

    def test_without_any_key_descriptions_stay_empty(
        self, uploads: Path, fake_agent: FakeAgent, model: list[str]
    ) -> None:
        at = upload(choose_upload(start_app()))
        assert model == []
        assert any("No API key" in w.value for w in at.warning)
        assert any("Using marks.csv" in s.value for s in at.success)

    def test_own_key_is_used_for_descriptions(
        self, uploads: Path, demo_env: None, fake_agent: FakeAgent,
        model: list[str],
    ) -> None:
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        at = upload(choose_upload(at))
        assert len(model) == 1
        assert caps.daily_cap(None).snapshot().llm_calls == 0  # own key: not counted

    def test_a_refused_file_stores_nothing(self, uploads: Path, demo_env: None,
                                           fake_agent: FakeAgent,
                                           model: list[str]) -> None:
        at = upload(choose_upload(start_app()), name="evil.db",
                    data=b"not a database")
        assert [e.value for e in at.error] == ["The file is not a SQLite database."]
        assert store_of(at).owned_by(at.session_state[data_panel.OWNER]) == []
        assert list(uploads.iterdir()) == []

    def test_replacing_a_file_deletes_the_old_one(
        self, uploads: Path, demo_env: None, fake_agent: FakeAgent, model: list[str]
    ) -> None:
        at = upload(choose_upload(start_app()))
        first = at.session_state[data_panel.DATASET_ID]
        at = upload(at, name="other.csv", data=b"a,b\n1,2\n")
        second = at.session_state[data_panel.DATASET_ID]
        assert first != second and not (uploads / first).exists()
        assert len(store_of(at).owned_by(at.session_state[data_panel.OWNER])) == 1

    def test_remove_button(self, uploads: Path, demo_env: None,
                           fake_agent: FakeAgent, model: list[str]) -> None:
        at = upload(choose_upload(start_app()))
        dataset_id = at.session_state[data_panel.DATASET_ID]
        at.button(key="remove_data").click().run()
        assert at.session_state[data_panel.DATASET_ID] is None
        assert not (uploads / dataset_id).exists()

    def test_idle_session_deletes_its_data(self, uploads: Path, demo_env: None,
                                           fake_agent: FakeAgent,
                                           model: list[str]) -> None:
        import time

        import app as app_module

        at = upload(choose_upload(start_app()))
        dataset_id = at.session_state[data_panel.DATASET_ID]
        at.session_state[app_module.LAST_ACTIVITY] = time.monotonic() - 601
        at.run()
        assert not (uploads / dataset_id).exists()

    def test_sessions_cannot_see_each_other(self, uploads: Path, demo_env: None,
                                            fake_agent: FakeAgent,
                                            model: list[str]) -> None:
        first = upload(choose_upload(start_app()))
        dataset_id = first.session_state[data_panel.DATASET_ID]
        other = choose_upload(start_app())
        other.session_state[data_panel.DATASET_ID] = dataset_id  # a guessed id
        other = ask(other, "q")
        assert fake_agent.sources_seen[-1] is None, "falls back, never borrows"

    def test_per_session_upload_limit(self, uploads: Path, demo_env: None,
                                      fake_agent: FakeAgent, model: list[str]) -> None:
        at = choose_upload(start_app())
        for n in range(data_panel.UPLOADS_PER_SESSION):
            at = upload(at, name=f"f{n}.csv", data=f"a\n{n}\n".encode())
        at = upload(at, name="extra.csv", data=b"a\n9\n")
        assert data_panel.UPLOAD_LIMIT_MESSAGE in [e.value for e in at.error]

    def test_daily_upload_cap(self, uploads: Path, demo_env: None,
                              fake_agent: FakeAgent, model: list[str],
                              monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCREW_DAILY_UPLOAD_CAP", "1")
        upload(choose_upload(start_app()))
        at = upload(choose_upload(start_app()), name="b.csv", data=b"a\n1\n")
        assert data_panel.DAILY_UPLOAD_MESSAGE in [e.value for e in at.error]


class TestEditedJoins:
    @pytest.fixture
    def catalog(self, tmp_path: Path):
        import sqlite3

        from agentcrew.db.engine import read_only_engine
        from agentcrew.db.introspect import build_catalog

        path = tmp_path / "c.db"
        with sqlite3.connect(path) as conn:
            pd.DataFrame({"id": [1], "student_id": [1]}).to_sql("marks", conn,
                                                                index=False)
            pd.DataFrame({"id": [1], "student_id": [1]}).to_sql("students", conn,
                                                                index=False)
        engine = read_only_engine(path)
        yield build_catalog(engine)
        engine.dispose()

    def test_keep_add_refuse(self, catalog) -> None:
        inferred = Join("marks", "student_id", "students", "student_id",
                        reason="same key column")
        frame = pd.DataFrame([
            {"keep": True, "from_table": "marks", "from_column": "student_id",
             "to_table": "students", "to_column": "student_id"},
            {"keep": True, "from_table": "marks", "from_column": "id",
             "to_table": "students", "to_column": "id"},
            {"keep": True, "from_table": "marks", "from_column": "",
             "to_table": "students", "to_column": "id"},
            {"keep": False, "from_table": "students", "from_column": "student_id",
             "to_table": "marks", "to_column": "student_id"},
        ])
        joins, problems = data_panel.edited_joins(frame, catalog, (inferred,))
        assert joins == (inferred,), "the kept inferred join keeps its reason"
        assert any("generic column 'id'" in p for p in problems)
        assert any("incomplete" in p for p in problems)


class TestPrivacyNoteMatchesTheCode:
    """Every number in the note is one the code enforces."""

    def test_numbers(self) -> None:
        note = data_panel.PRIVACY_NOTE
        assert f"fewer than {values.MAX_DISTINCT} distinct" in note
        assert f"query results (up to {answer_module.ANSWER_ROWS})" in note
        assert f"up to {describe.SAMPLE_ROWS} rows per table" in note
        assert f"cut to {describe.SAMPLE_CELL} characters" in note
        assert "up to 100)" in note  # explore.lookup: get_distinct_values LIMIT 100
        assert re.search(r"up to 3 example values", note)
