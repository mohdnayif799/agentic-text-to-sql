"""The structured run log (agentcrew/runlog.py): one JSON line per run.

It is the production trace of the router path, so three things are pinned:
the line is one JSON object Cloud Logging can parse, it never carries the
question, the SQL, rows or an error's text unless trace_content allows the
first two, and the app's _log_run builds it for every outcome.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
from pathlib import Path

import pytest

from agentcrew import runlog
from agentcrew.runlog import RunRecord

ROOT = Path(__file__).resolve().parents[1]


def record(**overrides) -> RunRecord:
    base = dict(surface="app", provider="gemini", model="m", arch="full",
                status="done", path="escalated",
                tools=(("run_sql", "repair"), ("compute:decompose_change",)),
                llm_calls=5, input_tokens=1200, output_tokens=300,
                thoughts_tokens=40, seconds=7.25)
    base.update(overrides)
    return RunRecord(**base)


class TestLine:
    def test_is_one_json_object_with_the_fields(self) -> None:
        entry = json.loads(runlog.emit(record(), logging.getLogger("t.runlog")))
        assert entry["event"] == "run" and entry["severity"] == "INFO"
        assert entry["surface"] == "app" and entry["key"] == "server"
        assert (entry["provider"], entry["model"], entry["arch"]) == ("gemini", "m",
                                                                     "full")
        assert (entry["status"], entry["path"]) == ("done", "escalated")
        assert entry["tools"] == "run_sql+repair>compute:decompose_change"
        assert (entry["llm_calls"], entry["input_tokens"], entry["output_tokens"],
                entry["thoughts_tokens"]) == (5, 1200, 300, 40)
        assert entry["seconds"] == 7.25 and entry["error"] is None
        assert entry["time"].endswith("+00:00")

    def test_message_summarises_for_the_log_list_view(self) -> None:
        entry = runlog.to_dict(record())
        assert entry["message"] == (
            "run status=done path=escalated "
            "tools=run_sql+repair>compute:decompose_change llm_calls=5 seconds=7.2"
        )

    def test_error_is_a_type_name_and_raises_the_severity(self) -> None:
        line = runlog.emit(record(status="error", error="LLMError"),
                           logging.getLogger("t.runlog"))
        entry = json.loads(line)
        assert entry["severity"] == "WARNING" and entry["error"] == "LLMError"
        assert entry["message"].endswith("error=LLMError")

    def test_no_tools_renders_as_a_dash(self) -> None:
        assert runlog.to_dict(record(tools=()))["tools"] == "-"

    def test_content_fields_are_absent_unless_given(self) -> None:
        entry = runlog.to_dict(record())
        assert "question" not in entry and "final_sql" not in entry
        entry = runlog.to_dict(record(question="q?", final_sql="SELECT 1"))
        assert entry["question"] == "q?" and entry["final_sql"] == "SELECT 1"

    def test_non_ascii_survives(self) -> None:
        line = runlog.emit(record(question="kitne customers hain?"),
                           logging.getLogger("t.runlog"))
        assert "kitne customers hain?" in line

    def test_emit_logs_at_info_or_warning(self) -> None:
        kept: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                kept.append(r)

        logger = logging.getLogger("t.runlog.levels")
        logger.addHandler(Keep())
        logger.setLevel(logging.INFO)
        runlog.emit(record(), logger)
        runlog.emit(record(error="X"), logger)
        assert [r.levelno for r in kept] == [logging.INFO, logging.WARNING]


class TestConfigure:
    def test_writes_bare_json_to_the_given_stream_once(self) -> None:
        logger = runlog.log
        saved = (list(logger.handlers), logger.level, logger.propagate)
        logger.handlers.clear()
        try:
            out = io.StringIO()
            runlog.configure(out)
            runlog.configure(io.StringIO())  # a second call adds nothing
            assert len(logger.handlers) == 1 and logger.propagate is False
            runlog.emit(record())
            lines = out.getvalue().splitlines()
            assert len(lines) == 1
            assert json.loads(lines[0])["event"] == "run"  # no prefix
        finally:
            logger.handlers[:] = saved[0]
            logger.setLevel(saved[1])
            logger.propagate = saved[2]

    def test_default_stream_is_stdout(self) -> None:
        logger = runlog.log
        saved = list(logger.handlers)
        logger.handlers.clear()
        try:
            runlog.configure()
            assert logger.handlers[0].stream is sys.stdout
        finally:
            logger.handlers[:] = saved


# ---------------------------------------------------------------------------
# The app's _log_run builds the record
# ---------------------------------------------------------------------------
def _load_app():
    spec = importlib.util.spec_from_file_location("runlog_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["runlog_app"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app():
    return _load_app()


class Usage:
    input_tokens = 11
    output_tokens = 7
    thoughts_tokens = 3


class TestAppLogRun:
    def test_records_tokens_key_and_error_type(self, app) -> None:
        line = app._log_run(
            {"provider": "gemini", "model": "m"}, "error", 0.0, 2, arch="full",
            error=RuntimeError("secret-text-here"), secret="", usage=Usage(),
            own_key=True, question="private question", final_sql="SELECT 1",
        )
        entry = json.loads(line)
        assert entry["key"] == "own" and entry["error"] == "RuntimeError"
        assert (entry["input_tokens"], entry["output_tokens"],
                entry["thoughts_tokens"]) == (11, 7, 3)
        # Content stays out unless AGENTCREW_TRACE_CONTENT is on, and an
        # error's text never appears.
        assert "private question" not in line and "SELECT 1" not in line
        assert "secret-text-here" not in line

    def test_trace_content_adds_question_and_final_sql(
        self, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCREW_TRACE_CONTENT", "true")
        line = app._log_run(
            {"provider": "gemini", "model": "m"}, "done", 0.0, 3, arch="full",
            path="direct", question="private question", final_sql="SELECT 1",
        )
        entry = json.loads(line)
        assert entry["question"] == "private question"
        assert entry["final_sql"] == "SELECT 1"

    def test_trace_content_is_off_by_default_and_in_the_image(self, app) -> None:
        assert app.trace_content_enabled() is False
        dockerfile = (ROOT / "Dockerfile").read_text()
        assert "TRACE_CONTENT" not in dockerfile
