"""The password in AGENTCREW_DATABASE_URL never appears anywhere.

"Anywhere" is checked surface by surface: a printed Settings object, the
errors the engine and the source raise (message, cause chain and formatted
traceback), the data source's key and label (they reach the browser as
widget keys and captions), everything the app renders, the structured run
log on stdout, and the detail lines on stderr. The driver is made to echo
the URL it was given, which is the worst case a real driver could produce.
Offline: the "server" is a closed local port or a patched engine factory.
"""

from __future__ import annotations

import io
import json
import logging
import traceback
from typing import Any

import pytest
from ui_harness import OWN_KEY, FakeAgent, ask, everything_rendered, start_app

from agentcrew.config import Settings
from agentcrew.datasets import source as source_module
from agentcrew.datasets.source import postgres_source, postgres_state
from agentcrew.db.engine import (
    DatabaseUnavailableError,
    UnsupportedDatabaseError,
    postgres_read_only_engine,
    scrub_url,
)

PASSWORD = "S3cr3t-Pa55w0rd-do-not-leak"
#: Port 1 on localhost: nothing listens, so the connection is refused at once.
URL = f"postgresql://agent_ro:{PASSWORD}@127.0.0.1:1/company"
#: A password with URL-special characters, as the parser and as typed.
RAW = "p@ss/w:rd#1"
ENCODED_URL = "postgresql://u:p%40ss%2Fw%3Ard%231@127.0.0.1:1/db"
#: The same password pasted without encoding: the parser cuts it at the "@".
UNENCODED_URL = f"postgresql://u:{RAW}@127.0.0.1:1/db"


class EchoingDriverError(RuntimeError):
    """A driver that puts the whole URL in its error text."""


@pytest.fixture
def echoing_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(url: str, **_: Any) -> Any:
        raise EchoingDriverError(f"could not connect using {url} (password "
                                 f"{PASSWORD!r} rejected)")

    monkeypatch.setattr(source_module, "postgres_read_only_engine", factory)


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        if record.exc_info:
            self.lines.append("".join(traceback.format_exception(*record.exc_info)))


@pytest.fixture
def all_logs() -> Any:
    """Every record of every logger, including the two the app configures
    with propagate=False (agentcrew.app on stderr, agentcrew.run on stdout)."""
    handler = Capture()
    names = ("", "agentcrew.app", "agentcrew.run", "agentcrew.app.langfuse",
             "agentcrew.app.llm", "sqlalchemy", "streamlit")
    loggers = [logging.getLogger(n) for n in names]
    for logger in loggers:
        logger.addHandler(handler)
    yield handler.lines
    for logger in loggers:
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class TestSettings:
    def test_printing_settings_hides_the_url(self) -> None:
        settings = Settings(database_url=URL)
        for shown in (repr(settings), str(settings), repr(settings.database_url),
                      json.dumps(settings.model_dump(mode="json"), default=str)):
            assert PASSWORD not in shown
        assert settings.database_url_value() == URL

    def test_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCREW_DATABASE_URL", URL)
        settings = Settings()
        assert settings.database_url_value() == URL and PASSWORD not in repr(settings)

    def test_unset_means_none(self) -> None:
        assert Settings().database_url_value() is None


# ---------------------------------------------------------------------------
# scrub_url
# ---------------------------------------------------------------------------
class TestScrub:
    def test_url_and_password_are_removed(self) -> None:
        text = f"failed for {URL}; tried password {PASSWORD} twice"
        cleaned = scrub_url(text, URL)
        assert PASSWORD not in cleaned and "agent_ro:" not in cleaned
        assert cleaned.count("***") == 2

    def test_encoded_and_decoded_spellings(self) -> None:
        text = f"auth failed: {RAW} / p%40ss%2Fw%3Ard%231"
        cleaned = scrub_url(text, ENCODED_URL)
        assert RAW not in cleaned and "p%40ss" not in cleaned

    def test_password_with_an_unencoded_at_sign(self) -> None:
        # The parser reads the password as "p" and the host as "ss/w...";
        # the whole typed password must still go.
        cleaned = scrub_url(f"could not resolve host for {UNENCODED_URL}; "
                            f"password was {RAW}", UNENCODED_URL)
        assert RAW not in cleaned and "ss/w:rd#1" not in cleaned

    def test_no_url_leaves_text_alone(self) -> None:
        assert scrub_url("plain text", None) == "plain text"
        assert scrub_url("", URL) == ""


# ---------------------------------------------------------------------------
# Engine and source errors
# ---------------------------------------------------------------------------
def everything_about(exc: BaseException) -> str:
    """The message, the repr and the full formatted traceback with causes."""
    return "\n".join([str(exc), repr(exc),
                      "".join(traceback.format_exception(exc))])


class TestErrors:
    @pytest.mark.parametrize("bad", [
        f"mysql://u:{PASSWORD}@h/db",
        f"not-a-url {PASSWORD}",
        f"postgresql://u:{PASSWORD}@h:notaport/db",
    ])
    def test_refused_urls_do_not_quote_themselves(self, bad: str) -> None:
        with pytest.raises(UnsupportedDatabaseError) as caught:
            postgres_read_only_engine(bad)
        assert PASSWORD not in everything_about(caught.value)

    def test_unreachable_server(self) -> None:
        with pytest.raises(DatabaseUnavailableError) as caught:
            postgres_state(Settings(database_url=URL))
        shown = everything_about(caught.value)
        assert PASSWORD not in shown and "AGENTCREW_DATABASE_URL" in shown

    def test_a_driver_that_echoes_the_url(self, echoing_engine: None) -> None:
        with pytest.raises(DatabaseUnavailableError) as caught:
            postgres_state(Settings(database_url=URL))
        exc = caught.value
        assert exc.__cause__ is None and exc.__suppress_context__
        shown = everything_about(exc)
        assert PASSWORD not in shown and "***" in shown
        assert "EchoingDriverError" in shown  # the type survives, for debugging

    def test_key_and_engine_repr_are_safe(self) -> None:
        engine = postgres_read_only_engine(URL)
        try:
            for shown in (repr(engine), str(engine.url), repr(engine.url),
                          source_module._url_key(URL)):
                assert PASSWORD not in shown
        finally:
            engine.dispose()

    def test_key_does_not_depend_on_the_password(self) -> None:
        other = URL.replace(PASSWORD, "another-password")
        assert source_module._url_key(URL) == source_module._url_key(other)


class FakeEngine:
    """Enough of an engine for postgres_source to build its label and key."""

    def __init__(self, url: str) -> None:
        from sqlalchemy.engine import make_url

        self.url = make_url(url)

    def dispose(self) -> None:
        pass


class TestSourceObject:
    def test_label_and_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentcrew.db.introspect import Catalog

        monkeypatch.setattr(source_module, "postgres_read_only_engine",
                            lambda url, **_: FakeEngine(url))
        monkeypatch.setattr(source_module, "build_catalog", lambda e: Catalog())
        monkeypatch.setattr(source_module, "infer_joins", lambda e, c: [])
        source = postgres_source(Settings(database_url=URL))
        assert source.label == "company on 127.0.0.1"
        assert PASSWORD not in source.key + source.label + repr(source)


# ---------------------------------------------------------------------------
# The app: browser, stdout run log, stderr detail lines
# ---------------------------------------------------------------------------
class TestApp:
    def _check(self, at: Any, logs: list[str], capsys: pytest.CaptureFixture) -> None:
        out = capsys.readouterr()
        assert not at.exception
        for surface in (everything_rendered(at), repr(at.session_state.filtered_state),
                        "\n".join(logs), out.out, out.err):
            assert PASSWORD not in surface

    def test_unreachable_database(
        self, monkeypatch: pytest.MonkeyPatch, fake_agent: FakeAgent,
        all_logs: list[str], capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DATABASE_URL", URL)
        at = start_app()
        at.text_input(key="api_key_anthropic").set_value("sk-ant-own-key-123").run()
        at = ask(at, "How many orders?")
        self._check(at, all_logs, capsys)
        assert any("could not be loaded" in e.value for e in at.sidebar.error)
        assert any("data panel failed: DatabaseUnavailableError" in line
                   for line in all_logs), "the failure is logged, scrubbed"

    def test_driver_echoing_the_url(
        self, monkeypatch: pytest.MonkeyPatch, echoing_engine: None,
        fake_agent: FakeAgent, all_logs: list[str], capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("AGENTCREW_DATABASE_URL", URL)
        at = ask(start_app(), "How many orders?")
        self._check(at, all_logs, capsys)
        assert any("***" in line for line in all_logs)

    def test_a_run_error_that_quotes_the_url(
        self, monkeypatch: pytest.MonkeyPatch, demo_env: None,
        fake_agent: FakeAgent, all_logs: list[str], capsys: pytest.CaptureFixture,
    ) -> None:
        """Whatever a run raises, the own-key error text shown in the browser
        and both log lines go through redact()."""
        monkeypatch.setenv("AGENTCREW_DATABASE_URL", URL)
        monkeypatch.setattr(source_module, "postgres_read_only_engine",
                            lambda url, **_: FakeEngine(url))
        from agentcrew.db.introspect import Catalog

        monkeypatch.setattr(source_module, "build_catalog", lambda e: Catalog())
        monkeypatch.setattr(source_module, "infer_joins", lambda e, c: [])
        fake_agent.error = RuntimeError(f"server closed the connection to {URL}")
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        at = ask(at, "How many orders?")
        self._check(at, all_logs, capsys)
        assert [e.value for e in at.error] == [
            "Run failed: RuntimeError: server closed the connection to ***"]
        run_lines = [json.loads(line) for line in all_logs
                     if line.startswith('{"severity"')]
        assert run_lines and run_lines[-1]["error"] == "RuntimeError"

    def test_run_log_with_trace_content_still_has_no_url(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import importlib.util
        import sys
        from pathlib import Path

        monkeypatch.setenv("AGENTCREW_DATABASE_URL", URL)
        monkeypatch.setenv("AGENTCREW_TRACE_CONTENT", "true")
        spec = importlib.util.spec_from_file_location(
            "secret_app", Path(__file__).resolve().parents[1] / "app.py")
        app = importlib.util.module_from_spec(spec)
        sys.modules["secret_app"] = app
        spec.loader.exec_module(app)
        stderr = io.StringIO()
        handler = logging.StreamHandler(stderr)
        app.log.addHandler(handler)
        try:
            line = app._log_run({"provider": "gemini", "model": "m"}, "error", 0.0, 1,
                                arch="full", error=RuntimeError(f"bad {URL}"),
                                question="q", final_sql="SELECT 1")
        finally:
            app.log.removeHandler(handler)
        assert PASSWORD not in line and PASSWORD not in stderr.getvalue()
        assert "***" in stderr.getvalue()
