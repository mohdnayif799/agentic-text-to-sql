"""Progress messages while an upload converts.

A 10 MB workbook takes about 16 seconds (docs/DATA_SOURCES.md), so the
converters report each step and the panel shows the latest one. Checked at
both ends: the messages ingest emits, and the status box the app renders.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook
from ui_harness import FakeAgent, start_app

from agentcrew import data_panel
from agentcrew.config import Settings
from agentcrew.datasets import convert
from agentcrew.datasets.errors import UploadError
from agentcrew.datasets.ingest import ingest
from agentcrew.datasets.store import DatasetStore
from agentcrew.llm import FakeClient


def workbook(rows: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Marks"
    ws.append(["student_id", "score"])
    for i in range(rows):
        ws.append([i, i % 100])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def run_ingest(tmp_path: Path, files: list[tuple[str, bytes]]) -> list[str]:
    said: list[str] = []
    store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60, sweep=False)
    ingest(store, owner="o", files=files, settings=Settings(upload_dir=tmp_path),
           progress=said.append)
    return said


class TestMessages:
    def test_excel_reports_each_chunk(self, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(convert, "CHUNK_ROWS", 100)
        said = run_ingest(tmp_path, [("marks.xlsx", workbook(250))])
        assert said[0].startswith("Opening marks.xlsx (")
        assert "Converting sheet 'Marks'" in said
        assert "Converting sheet 'Marks': 100 rows so far" in said
        assert "Converting sheet 'Marks': 200 rows so far" in said
        assert said[-1] == "Reading the tables and looking for joins"

    def test_csv_names_each_file_and_its_rows(self, tmp_path: Path) -> None:
        said = run_ingest(tmp_path, [("a.csv", b"x\n1\n2\n"), ("b.csv", b"y\n3\n")])
        assert [m for m in said if m.startswith("Converting")] == [
            "Converting a.csv: 2 rows", "Converting b.csv: 1 rows"]
        assert said[0].startswith("Reading a.csv (")

    def test_no_callback_is_fine(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        assert ingest(store, owner="o", filename="a.csv", data=b"x\n1\n",
                      settings=Settings(upload_dir=tmp_path)).tables == ("a",)

    def test_messages_stop_at_the_failure(self, tmp_path: Path) -> None:
        said: list[str] = []
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        with pytest.raises(UploadError):
            ingest(store, owner="o", filename="bad.db", data=b"nope" * 10,
                   settings=Settings(upload_dir=tmp_path), progress=said.append)
        assert said == ["Checking bad.db"]


class TestInTheApp:
    @pytest.fixture
    def model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agentcrew.llm.build_client",
                            lambda s: FakeClient(responder=lambda a, b: {"tables": []}))

    def _upload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                name: str, data: bytes) -> Any:
        monkeypatch.setenv("AGENTCREW_UPLOAD_DIR", str(tmp_path / "uploads"))
        at = start_app()
        at.radio(key=data_panel.DATA_CHOICE).set_value(data_panel.UPLOAD).run()
        at.sidebar.file_uploader[0].set_value((name, data, "application/x")).run()
        return at

    def test_status_box_ends_ready(self, tmp_path: Path, demo_env: None,
                                   fake_agent: FakeAgent, model: None,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        real = data_panel.handle_upload

        def spy(*args: Any, progress: Any = None, **kwargs: Any) -> Any:
            def both(message: str) -> None:
                seen.append(message)
                progress(message)
            return real(*args, progress=both, **kwargs)

        monkeypatch.setattr(data_panel, "handle_upload", spy)
        at = self._upload(tmp_path, monkeypatch, "marks.xlsx", workbook(20))
        assert not at.exception
        (status,) = at.sidebar.status
        assert status.label == data_panel.PROGRESS_DONE
        assert status.state == "complete"
        assert seen[0].startswith("Opening marks.xlsx")
        assert seen[-1] == "Drafting descriptions of the tables and columns"

    def test_status_box_says_refused(self, tmp_path: Path, demo_env: None,
                                     fake_agent: FakeAgent, model: None,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        at = self._upload(tmp_path, monkeypatch, "bad.db", b"not a database")
        (status,) = at.sidebar.status
        assert status.label == data_panel.PROGRESS_FAILED and status.state == "error"
