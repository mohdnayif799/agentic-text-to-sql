"""The REST API (agentcrew/api), end to end and offline.

Everything below the HTTP layer is real: the gate, the routes, the service,
the guard, the NorthStar database, the upload converter and the dataset
store. Only the model is scripted, and the clock the rate limiter and the
store read, so "a minute later" and "31 minutes later" need no sleeping.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from agentcrew import caps, runlog, service
from agentcrew.api.app import create_app
from agentcrew.api.auth import MIN_KEY_CHARS, KeyRing
from agentcrew.api.ratelimit import RateLimiter
from agentcrew.archs import app_runner
from agentcrew.datasets import store as store_module
from agentcrew.datasets.source import reset_sources
from agentcrew.datasets.store import DatasetStore
from agentcrew.llm import FakeClient, LLMTimeoutError, QuotaExhaustedError

KEY_A = "test-key-a-0123456789abcdef0123456789"
KEY_B = "test-key-b-0123456789abcdef0123456789"
SERVER_MODEL_KEY = "AQ.server-side-model-key-never-shown-0123456789"
COUNT = "SELECT COUNT(*) AS n FROM customers"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Model:
    """A scripted model: one direct answer per question, and a description
    draft for an upload. Counts its calls."""

    def __init__(self) -> None:
        self.sql = COUNT
        self.calls = 0
        self.error: Exception | None = None

    def __call__(self, system: str, user: str) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if "SQL analyst" in system:
            return {"sql": self.sql, "rationale": "x", "expected_shape": ""}
        if "You check whether one query result" in system:
            return {"needs_followup": False, "reason": "enough"}
        if "final answer" in system:
            return {"answer": "Here is the answer.", "key_numbers": [],
                    "caveats": ["A caveat."]}
        if "You document a database" in system:
            return {"tables": [
                {"name": "students", "description": "People enrolled.",
                 "row": "One row per student.",
                 "columns": [{"name": "name", "description": "The student's name."}]},
                {"name": "marks", "description": "Scores.",
                 "row": "One row per student per course.", "columns": []},
            ]}
        raise AssertionError(system[:80])


class Api:
    def __init__(self, client: TestClient, model: Model, clock: Clock,
                 store: DatasetStore, lines: list[dict[str, Any]],
                 texts: list[str]) -> None:
        self.client, self.model, self.clock, self.store = client, model, clock, store
        self.lines, self.texts = lines, texts

    def get(self, path: str, key: str | None = KEY_A, **kw: Any) -> Any:
        return self.client.get(f"/v1{path}", headers=_auth(key), **kw)

    def post(self, path: str, body: Any = None, key: str | None = KEY_A,
             **kw: Any) -> Any:
        return self.client.post(f"/v1{path}", json=body, headers=_auth(key), **kw)

    def delete(self, path: str, key: str | None = KEY_A) -> Any:
        return self.client.delete(f"/v1{path}", headers=_auth(key))

    def upload(self, filename: str, data: bytes, key: str | None = KEY_A) -> Any:
        return self.client.post("/v1/datasets", params={"filename": filename},
                                content=data, headers=_auth(key))

    def runs(self) -> list[dict[str, Any]]:
        return [line for line in self.lines if line.get("event") == "run"]

    def requests(self) -> list[dict[str, Any]]:
        return [line for line in self.lines if line.get("event") == "request"]


def _auth(key: str | None) -> dict[str, str]:
    return {} if key is None else {"X-API-Key": key}


def error(response: Any) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert "Traceback" not in response.text
    return body["error"]


class _Collect(logging.Handler):
    def __init__(self, lines: list[dict[str, Any]], texts: list[str]) -> None:
        super().__init__()
        self.lines, self.texts = lines, texts

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        self.texts.append(text)
        if text.startswith("{"):
            self.lines.append(json.loads(text))


@pytest.fixture
def make_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_path: Path,
             demo_env: None) -> Iterator[Callable[..., Api]]:
    """Builds the API as a deployment would have it: keys, the server's model
    key and provider from the environment. Keyword arguments are further
    AGENTCREW_* variables."""
    model, clock = Model(), Clock()
    lines: list[dict[str, Any]] = []
    texts: list[str] = []
    handler = _Collect(lines, texts)
    loggers = [logging.getLogger(runlog.LOGGER_NAME), logging.getLogger("agentcrew.app")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        # configure() leaves a logger that already has a handler alone, so
        # the level a deployment gets from it is set here.
        logger.setLevel(logging.INFO)
    real_build = app_runner.build_runtime

    def build_runtime(settings: Any, *_: Any, source: Any = None, **__: Any) -> Any:
        return real_build(settings, lambda s: FakeClient(responder=model),
                          source=source)

    monkeypatch.setattr(app_runner, "build_runtime", build_runtime)
    monkeypatch.setattr("agentcrew.llm.build_client",
                        lambda settings: FakeClient(responder=model))
    monkeypatch.setenv("AGENTCREW_GEMINI_API_KEY", SERVER_MODEL_KEY)
    monkeypatch.setenv("AGENTCREW_API_KEYS", f"{KEY_A}, {KEY_B}")
    monkeypatch.setenv("AGENTCREW_UPLOAD_DIR", str(tmp_path / "uploads"))
    stores: list[DatasetStore] = []
    recached = False

    def make(per_minute: int = 1000, **env: Any) -> Api:
        nonlocal recached
        for name, value in env.items():
            monkeypatch.setenv(f"AGENTCREW_{name.upper()}", str(value))
        if "max_result_rows" in env:
            # The tools are cached per database for the process (a deployment
            # never changes its row cap while running): rebuild them for this
            # test's cap, and again afterwards.
            app_runner._CONTEXT.clear()
            recached = True
        store = DatasetStore(tmp_path / "uploads", max_bytes=150_000_000,
                             ttl_seconds=1800, clock=clock, sweep=False)
        store_module._store = store
        stores.append(store)
        app = create_app(limiter=RateLimiter(per_minute, clock=clock))
        return Api(TestClient(app), model, clock, store, lines, texts)

    yield make
    for store in stores:
        for owner in {d.owner for d in list(store._datasets.values())}:
            store.remove_owner(owner)
    reset_sources()
    if recached:
        app_runner._CONTEXT.clear()
    for logger, level in zip(loggers, levels, strict=True):
        logger.removeHandler(handler)
        logger.setLevel(level)


@pytest.fixture
def api(make_api: Callable[..., Api]) -> Api:
    return make_api()


def xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


COLLEGE = xlsx({
    "students": [["student_id", "name", "dept"],
                 [1, "Asha", "CS"], [2, "Ravi", "CS"], [3, "Meena", "EE"]],
    "marks": [["mark_id", "student_id", "course", "score"],
              [1, 1, "DB", 80], [2, 1, "OS", 90], [3, 2, "DB", 70], [4, 3, "OS", 60]],
})
PEOPLE_CSV = ("id,name,score\n" + "\n".join(
    f"{i},name {i},{i * 10}" for i in range(1, 11)) + "\n").encode()


# ---------------------------------------------------------------------------
# Health and authentication
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health_needs_no_key_and_says_only_ok(self, api: Api) -> None:
        response = api.client.get("/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_is_not_logged(self, api: Api) -> None:
        api.client.get("/v1/health")
        assert api.requests() == []


class TestAuth:
    @pytest.mark.parametrize("path", ["/ask", "/sql", "/datasets"])
    def test_missing_key_is_401(self, api: Api, path: str) -> None:
        response = api.post(path, {"question": "q", "sql": COUNT}, key=None)
        assert response.status_code == 401
        assert error(response)["code"] == "unauthorized"

    @pytest.mark.parametrize("key", [
        "wrong-key-0123456789abcdef0123456789", KEY_A[:-1], KEY_A + "x", "",
        KEY_A.upper(), "x" * 5000,
    ])
    def test_wrong_key_is_401(self, api: Api, key: str) -> None:
        response = api.post("/sql", {"sql": COUNT}, key=key)
        assert response.status_code == 401
        assert error(response)["code"] == "unauthorized"

    def test_dataset_routes_need_a_key(self, api: Api) -> None:
        dataset = "0" * 32
        assert api.get(f"/datasets/{dataset}/schema", key=None).status_code == 401
        assert api.delete(f"/datasets/{dataset}", key=None).status_code == 401

    def test_an_unknown_v1_path_without_a_key_is_401_not_404(self, api: Api) -> None:
        assert api.get("/nothing-here", key=None).status_code == 401

    def test_a_key_in_the_query_string_is_not_accepted(self, api: Api) -> None:
        response = api.client.post(f"/v1/sql?api_key={KEY_A}&X-API-Key={KEY_A}",
                                   json={"sql": COUNT})
        assert response.status_code == 401

    def test_two_key_headers_are_refused(self, api: Api) -> None:
        response = api.client.post(
            "/v1/sql", json={"sql": COUNT},
            headers=[("X-API-Key", KEY_A), ("X-API-Key", KEY_B)])
        assert response.status_code == 401

    def test_an_unauthenticated_body_is_never_read(self, api: Api) -> None:
        """401 before 413: the size of the body is not even looked at."""
        response = api.client.post("/v1/sql", content=b"x" * 500_000)
        assert response.status_code == 401

    def test_both_configured_keys_work(self, api: Api) -> None:
        for key in (KEY_A, KEY_B):
            assert api.post("/sql", {"sql": COUNT}, key=key).status_code == 200

    def test_no_keys_configured_refuses_everything(
            self, make_api: Callable[..., Api]) -> None:
        api = make_api(api_keys="")
        assert api.post("/sql", {"sql": COUNT}).status_code == 401
        assert api.client.get("/v1/health").status_code == 200
        assert any("no usable key" in text for text in api.texts)

    def test_short_keys_are_ignored(self, make_api: Callable[..., Api]) -> None:
        api = make_api(api_keys=f"short,{KEY_A}")
        assert api.post("/sql", {"sql": COUNT}, key="short").status_code == 401
        assert api.post("/sql", {"sql": COUNT}).status_code == 200


class TestKeyRing:
    def test_identify(self) -> None:
        ring = KeyRing(f"{KEY_A}\n{KEY_B} , {KEY_A}")
        assert len(ring) == 2 and ring.ignored == 0
        a, b = ring.identify(KEY_A), ring.identify(KEY_B)
        assert a is not None and b is not None and a != b
        assert len(a.id) == 12 and a.owner.startswith("api:") and a.id in a.owner
        assert ring.identify("nope" * 10) is None and ring.identify(None) is None

    def test_it_keeps_digests_not_keys(self) -> None:
        ring = KeyRing(KEY_A)
        client = ring.identify(KEY_A)
        assert KEY_A not in repr(vars(ring)) and KEY_A not in repr(client)

    def test_short_keys_are_counted_and_dropped(self) -> None:
        ring = KeyRing(f"{'x' * (MIN_KEY_CHARS - 1)},{KEY_A}")
        assert len(ring) == 1 and ring.ignored == 1

    def test_unset(self) -> None:
        assert len(KeyRing(None)) == 0 and KeyRing("").identify("") is None


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
class TestRateLimit:
    def test_429_with_retry_after(self, make_api: Callable[..., Api]) -> None:
        api = make_api(per_minute=3)
        for _ in range(3):
            assert api.post("/sql", {"sql": COUNT}).status_code == 200
        response = api.post("/sql", {"sql": COUNT})
        assert response.status_code == 429
        assert error(response)["code"] == "rate_limited"
        assert response.headers["retry-after"] == "60"

    def test_it_is_per_key(self, make_api: Callable[..., Api]) -> None:
        api = make_api(per_minute=1)
        assert api.post("/sql", {"sql": COUNT}).status_code == 200
        assert api.post("/sql", {"sql": COUNT}).status_code == 429
        assert api.post("/sql", {"sql": COUNT}, key=KEY_B).status_code == 200

    def test_the_window_slides(self, make_api: Callable[..., Api]) -> None:
        api = make_api(per_minute=2)
        api.post("/sql", {"sql": COUNT})
        api.clock.now += 30
        api.post("/sql", {"sql": COUNT})
        assert api.post("/sql", {"sql": COUNT}).headers["retry-after"] == "30"
        api.clock.now += 31
        assert api.post("/sql", {"sql": COUNT}).status_code == 200

    def test_a_limited_request_runs_nothing(self, make_api: Callable[..., Api]) -> None:
        api = make_api(per_minute=1)
        api.post("/sql", {"sql": COUNT})
        before = len(api.runs())
        assert api.post("/ask", {"question": "How many customers?"}).status_code == 429
        assert len(api.runs()) == before and api.model.calls == 0

    def test_wrong_keys_do_not_use_up_a_keys_limit(
            self, make_api: Callable[..., Api]) -> None:
        api = make_api(per_minute=1)
        for _ in range(5):
            api.post("/sql", {"sql": COUNT}, key="wrong" * 8)
        assert api.post("/sql", {"sql": COUNT}).status_code == 200

    def test_limiter_unit(self) -> None:
        clock = Clock()
        limiter = RateLimiter(2, clock=clock)
        assert [limiter.check("a") for _ in range(3)] == [0, 0, 60]
        assert limiter.check("b") == 0
        clock.now += 60
        assert limiter.check("a") == 0
        assert RateLimiter(0).check("a") == 0


# ---------------------------------------------------------------------------
# Body limit
# ---------------------------------------------------------------------------
class TestBodyLimit:
    def test_declared_length_over_the_limit_is_413(self, api: Api) -> None:
        response = api.post("/sql", {"sql": "SELECT 1 -- " + "x" * 70_000})
        assert response.status_code == 413
        assert error(response)["code"] == "body_too_large"

    def test_a_body_with_no_declared_length_is_cut_while_read(self, api: Api) -> None:
        def chunks() -> Iterator[bytes]:
            yield b'{"sql": "SELECT 1 -- '
            for _ in range(20):
                yield b"x" * 8_000
            yield b'"}'

        response = api.client.post(
            "/v1/sql", content=chunks(),
            headers={**_auth(KEY_A), "Content-Type": "application/json"})
        assert response.status_code == 413
        assert error(response)["code"] == "body_too_large"
        assert api.runs() == [], "nothing ran"

    def test_an_upload_over_its_limit_is_413(self, make_api: Callable[..., Api]) -> None:
        api = make_api(max_upload_mb=0.01, max_excel_upload_mb=0.01)
        response = api.upload("big.csv", b"a,b\n" + b"1,2\n" * 5_000)
        assert response.status_code == 413
        assert api.store.owned_by(KeyRing(KEY_A).identify(KEY_A).owner) == []

    def test_an_upload_may_be_larger_than_a_json_body(self, api: Api) -> None:
        data = b"a,b\n" + b"1,2\n" * 30_000  # 120 KB, over the 64 KB JSON limit
        assert api.upload("wide.csv", data).status_code == 201

    def test_the_json_limit_is_configurable(self, make_api: Callable[..., Api]) -> None:
        api = make_api(api_max_json_kb=1)
        assert api.post("/sql", {"sql": "SELECT 1 -- " + "x" * 2_000}).status_code == 413
        assert api.post("/sql", {"sql": COUNT}).status_code == 200


# ---------------------------------------------------------------------------
# The error envelope
# ---------------------------------------------------------------------------
class TestEnvelope:
    def test_unknown_path(self, api: Api) -> None:
        response = api.get("/nothing-here")
        assert response.status_code == 404
        assert error(response)["code"] == "not_found"

    def test_wrong_method(self, api: Api) -> None:
        response = api.get("/ask")
        assert response.status_code == 405
        assert error(response)["code"] == "method_not_allowed"

    @pytest.mark.parametrize("body", [
        {}, {"question": ""}, {"question": "x" * 2001}, {"question": "q", "extra": 1},
        {"question": "q", "dataset_id": "../../etc/passwd"}, {"question": 5},
    ])
    def test_invalid_request(self, api: Api, body: dict[str, Any]) -> None:
        response = api.post("/ask", body)
        assert response.status_code == 422
        assert error(response)["code"] == "invalid_request"
        assert api.model.calls == 0

    def test_validation_errors_do_not_echo_the_input(self, api: Api) -> None:
        response = api.post("/ask", {"question": "q", "dataset_id": "SECRET-VALUE"})
        assert "SECRET-VALUE" not in response.text

    def test_not_json(self, api: Api) -> None:
        response = api.client.post("/v1/ask", content=b"not json",
                                   headers={**_auth(KEY_A),
                                            "Content-Type": "application/json"})
        assert response.status_code == 422
        assert error(response)["code"] == "invalid_request"

    def test_an_unexpected_exception_is_the_500_envelope(
            self, api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("internal detail with SECRET in it")

        monkeypatch.setattr(service, "answer", boom)
        response = api.post("/ask", {"question": "How many customers?"})
        assert response.status_code == 500
        assert error(response)["code"] == "internal_error"
        assert "SECRET" not in response.text and "RuntimeError" not in response.text
        assert not any("SECRET" in text for text in api.texts)

    def test_every_response_carries_a_request_id(self, api: Api) -> None:
        ok = api.post("/sql", {"sql": COUNT})
        bad = api.post("/sql", {"sql": COUNT}, key=None)
        assert ok.headers["x-request-id"] != bad.headers["x-request-id"]
        assert error(bad)["request_id"] == bad.headers["x-request-id"]
        assert ok.headers["cache-control"] == "no-store"

    def test_no_cors_headers(self, api: Api) -> None:
        origin = {"Origin": "https://evil.example",
                  "Access-Control-Request-Method": "POST"}
        preflight = api.client.options("/v1/ask", headers={**_auth(KEY_A), **origin})
        normal = api.client.post("/v1/sql", json={"sql": COUNT},
                                 headers={**_auth(KEY_A), **origin})
        for response in (preflight, normal):
            assert not any(name.lower().startswith("access-control")
                           for name in response.headers)

    def test_docs_are_off_by_default(self, api: Api) -> None:
        for path in ("/docs", "/openapi.json"):
            assert api.client.get(path).status_code == 401, "no key: nothing shown"
            assert api.client.get(path, headers=_auth(KEY_A)).status_code == 404

    def test_docs_can_be_switched_on(self, make_api: Callable[..., Api]) -> None:
        api = make_api(api_docs="true")
        assert api.client.get("/openapi.json").status_code == 200
        assert api.client.get("/docs").status_code == 200
        assert api.post("/sql", {"sql": COUNT}, key=None).status_code == 401


# ---------------------------------------------------------------------------
# POST /ask
# ---------------------------------------------------------------------------
class TestAsk:
    def test_northstar_question(self, api: Api) -> None:
        response = api.post("/ask", {"question": "How many customers do we have?"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "done" and body["answer"] == "Here is the answer."
        assert body["sql"].upper().startswith("SELECT COUNT(*)")
        assert body["columns"] == ["n"] and body["rows"] == [[1400]]
        assert body["row_count"] == 1 and body["rows_capped"] is False
        assert body["path"] == "direct" and body["llm_calls"] == 3
        assert body["caveats"] == ["A caveat."] and body["dataset_id"] is None

    def test_rows_are_capped_and_json_safe(self) -> None:
        """The table of a response: cut at the row cap (the guard injects it
        as LIMIT, so reaching it is the sign), and every value JSON."""
        import datetime as dt
        import decimal

        from agentcrew.api.schemas import table
        from agentcrew.schemas import ExecutionResult

        rows = [[1, decimal.Decimal("2.5"), dt.date(2026, 1, 2), bytes([0, 1]), None]] * 5
        result = ExecutionResult(ok=True, sql="x", columns=list("abcde"), rows=rows,
                                 row_count=5)
        capped = table(result, max_rows=5)
        assert capped["rows_capped"] is True and capped["row_count"] == 5
        assert capped["rows"][0] == [1, 2.5, "2026-01-02", "<2 bytes>", None]
        assert table(result, max_rows=500)["rows_capped"] is False
        assert table(None, 500) == {}
        json.dumps(capped)

    def test_it_runs_on_the_server_key_with_the_demo_budgets(self, api: Api) -> None:
        api.post("/ask", {"question": "How many customers do we have?"})
        (line,) = api.runs()
        assert line["surface"] == "api" and line["key"] == "server"
        assert line["status"] == "done" and line["llm_calls"] == 3
        assert line["client"] == KeyRing(KEY_A).identify(KEY_A).id

    def test_a_write_request_is_refused_without_a_model_call(self, api: Api) -> None:
        response = api.post("/ask", {"question": "Delete all customers in LATAM."})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "refused" and body["sql"] is None and not body["rows"]
        assert api.model.calls == 0
        assert api.runs()[-1]["status"] == "refused"

    def test_a_write_from_the_model_is_refused_by_the_guard(self, api: Api) -> None:
        api.model.sql = "DELETE FROM customers"
        body = api.post("/ask", {"question": "How many customers?"}).json()
        assert body["status"] in ("refused", "failed") and not body["rows"]
        count = api.post("/sql", {"sql": COUNT}).json()
        assert count["rows"] == [[1400]]

    @pytest.mark.parametrize(("raised", "status", "code"), [
        (QuotaExhaustedError("quota: SECRET-DETAIL"), 503, "model_quota"),
        (LLMTimeoutError("slow: SECRET-DETAIL"), 504, "model_timeout"),
        (RuntimeError("boom: SECRET-DETAIL"), 500, "agent_error"),
    ])
    def test_model_failures(self, api: Api, raised: Exception, status: int,
                            code: str) -> None:
        api.model.error = raised
        response = api.post("/ask", {"question": "How many customers?"})
        assert response.status_code == status
        assert error(response)["code"] == code
        assert "SECRET-DETAIL" not in response.text
        assert caps._cap is not None and caps._cap.snapshot().runs == 0

    def test_no_model_key_is_503(self, make_api: Callable[..., Api],
                                 monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTCREW_GEMINI_API_KEY")
        api = make_api()
        response = api.post("/ask", {"question": "How many customers?"})
        assert response.status_code == 503
        assert error(response)["code"] == "not_configured"

    def test_unknown_dataset_is_404(self, api: Api) -> None:
        response = api.post("/ask", {"question": "q", "dataset_id": "0" * 32})
        assert response.status_code == 404
        assert error(response)["code"] == "dataset_not_found"


# ---------------------------------------------------------------------------
# POST /sql
# ---------------------------------------------------------------------------
class TestSql:
    def test_select(self, api: Api) -> None:
        response = api.post("/sql", {"sql": "SELECT segment, COUNT(*) AS n FROM "
                                            "customers GROUP BY segment ORDER BY 1"})
        assert response.status_code == 200
        body = response.json()
        assert body["columns"] == ["segment", "n"] and body["row_count"] == 3
        assert body["sql"].upper().endswith("LIMIT 500")
        assert api.model.calls == 0

    @pytest.mark.parametrize("statement", [
        "DELETE FROM customers", "UPDATE customers SET segment = 'x'",
        "DROP TABLE customers", "INSERT INTO regions (region_id) VALUES (99)",
        "SELECT 1; DELETE FROM customers", "ATTACH DATABASE 'x.db' AS x",
        "PRAGMA writable_schema = ON",
    ])
    def test_write_sql_is_refused(self, api: Api, statement: str) -> None:
        response = api.post("/sql", {"sql": statement})
        assert response.status_code == 403
        found = error(response)
        assert found["code"] == "sql_refused" and found["message"].startswith("Refused:")
        assert api.post("/sql", {"sql": COUNT}).json()["rows"] == [[1400]]

    def test_a_bad_query_is_one_plain_sentence(self, api: Api) -> None:
        response = api.post("/sql", {"sql": "SELECT nope FROM customers"})
        assert response.status_code == 400
        found = error(response)
        assert found["code"] == "sql_error" and "nope" in found["message"]
        assert "\n" not in found["message"] and "sqlite3" not in found["message"]

    def test_row_cap(self, make_api: Callable[..., Api]) -> None:
        api = make_api(max_result_rows=7)
        body = api.post("/sql", {"sql": "SELECT * FROM orders LIMIT 100000"}).json()
        assert body["row_count"] == 7 and body["rows_capped"] is True

    def test_it_goes_through_run_sql_and_logs_as_api(self, api: Api) -> None:
        api.post("/sql", {"sql": COUNT})
        api.post("/sql", {"sql": "DROP TABLE customers"})
        done, refused = api.runs()
        assert done["surface"] == refused["surface"] == "api"
        assert done["rows"] == 1 and refused["error"] == "blocked"
        assert done["client"] == KeyRing(KEY_A).identify(KEY_A).id
        assert "final_sql" not in done, "SQL text only with trace content on"
        assert caps._cap is not None and caps._cap.snapshot().sql_runs == 2
        assert caps._cap.snapshot().runs == 0, "the agent's daily runs are untouched"

    def test_unknown_dataset_is_404(self, api: Api) -> None:
        response = api.post("/sql", {"sql": COUNT, "dataset_id": "f" * 32})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
class TestDatasets:
    def test_upload_then_schema_then_ask_then_sql(self, api: Api) -> None:
        created = api.upload("college.xlsx", COLLEGE)
        assert created.status_code == 201
        made = created.json()
        dataset = made["dataset_id"]
        assert made["kind"] == "excel" and made["tables"] == ["marks", "students"]
        assert made["descriptions"] == "drafted"
        assert made["expires_after_seconds"] == 1800

        schema = api.get(f"/datasets/{dataset}/schema")
        assert schema.status_code == 200
        tables = {t["name"]: t for t in schema.json()["tables"]}
        assert tables["students"]["description"] == "People enrolled."
        assert tables["students"]["row_meaning"] == "One row per student."
        assert tables["marks"]["row_meaning"] == "One row per student per course."
        assert tables["students"]["row_count"] == 3
        columns = {c["name"]: c for c in tables["students"]["columns"]}
        assert set(columns) == {"student_id", "name", "dept"}
        assert columns["name"]["description"] == "The student's name."
        assert columns["student_id"]["type"]
        joins = schema.json()["joins"]
        assert {(j["from_table"], j["from_column"], j["to_table"], j["to_column"])
                for j in joins} == {("marks", "student_id", "students", "student_id")}

        api.model.sql = ("SELECT s.name, AVG(m.score) AS average FROM students s "
                         "JOIN marks m ON m.student_id = s.student_id "
                         "GROUP BY s.name ORDER BY s.name")
        asked = api.post("/ask", {"question": "What is each student's average?",
                                  "dataset_id": dataset})
        assert asked.status_code == 200
        body = asked.json()
        assert body["status"] == "done" and body["dataset_id"] == dataset
        assert body["rows"] == [["Asha", 85.0], ["Meena", 60.0], ["Ravi", 70.0]]

        ran = api.post("/sql", {"sql": "SELECT COUNT(*) AS n FROM marks",
                                "dataset_id": dataset})
        assert ran.status_code == 200 and ran.json()["rows"] == [[4]]
        refused = api.post("/sql", {"sql": "DELETE FROM marks", "dataset_id": dataset})
        assert refused.status_code == 403

    def test_the_upload_charges_the_upload_and_call_caps(self, api: Api) -> None:
        api.upload("college.xlsx", COLLEGE)
        snapshot = caps._cap.snapshot()
        assert snapshot.uploads == 1 and snapshot.llm_calls == 1
        describe = [line for line in api.runs() if line["arch"] == "describe"]
        assert len(describe) == 1 and describe[0]["surface"] == "api"

    def test_another_keys_dataset_is_404_everywhere(self, api: Api) -> None:
        dataset = api.upload("people.csv", PEOPLE_CSV).json()["dataset_id"]
        attempts = [
            api.get(f"/datasets/{dataset}/schema", key=KEY_B),
            api.delete(f"/datasets/{dataset}", key=KEY_B),
            api.post("/ask", {"question": "q", "dataset_id": dataset}, key=KEY_B),
            api.post("/sql", {"sql": "SELECT * FROM people", "dataset_id": dataset},
                     key=KEY_B),
        ]
        for response in attempts:
            assert response.status_code == 404
            assert error(response)["code"] == "dataset_not_found"
        assert api.get(f"/datasets/{dataset}/schema").status_code == 200, (
            "and the owner still has it")

    def test_a_dataset_expires_30_minutes_after_its_last_use(self, api: Api) -> None:
        dataset = api.upload("people.csv", PEOPLE_CSV).json()["dataset_id"]
        api.clock.now += 29 * 60
        assert api.get(f"/datasets/{dataset}/schema").status_code == 200  # a use
        api.clock.now += 29 * 60
        assert api.post("/sql", {"sql": "SELECT COUNT(*) FROM people",
                                 "dataset_id": dataset}).status_code == 200
        api.clock.now += 31 * 60
        for response in (
            api.get(f"/datasets/{dataset}/schema"),
            api.post("/ask", {"question": "q", "dataset_id": dataset}),
            api.post("/sql", {"sql": "SELECT 1", "dataset_id": dataset}),
        ):
            assert response.status_code == 410
            found = error(response)
            assert found["code"] == "dataset_expired" and "30 minutes" in found["message"]
        assert api.get(f"/datasets/{dataset}/schema", key=KEY_B).status_code == 404
        assert not (api.store.root / dataset).exists(), "its file is gone"

    def test_two_datasets_per_key(self, api: Api) -> None:
        first = api.upload("a.csv", PEOPLE_CSV).json()["dataset_id"]
        assert api.upload("b.csv", PEOPLE_CSV).status_code == 201
        third = api.upload("c.csv", PEOPLE_CSV)
        assert third.status_code == 409
        assert error(third)["code"] == "dataset_limit"
        assert api.upload("c.csv", PEOPLE_CSV, key=KEY_B).status_code == 201
        assert api.delete(f"/datasets/{first}").status_code == 204
        assert api.upload("c.csv", PEOPLE_CSV).status_code == 201

    def _slow_ingest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every conversion take long enough that simultaneous uploads
        are all inside it together."""
        import time

        from agentcrew.api import routes

        real = routes.ingest

        def slow(*args: Any, **kwargs: Any) -> Any:
            time.sleep(0.3)
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "ingest", slow)

    def _together(self, api: Api, keys: list[str]) -> list[int]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(len(keys)) as pool:
            return sorted(r.status_code for r in pool.map(
                lambda key: api.upload("a.csv", PEOPLE_CSV, key=key), keys))

    def test_simultaneous_uploads_cannot_pass_the_per_key_limit(
            self, make_api: Callable[..., Api],
            monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(api_max_concurrent_per_key=10)  # so all six get in
        self._slow_ingest(monkeypatch)
        assert self._together(api, [KEY_A] * 6) == [201, 201, 409, 409, 409, 409]
        owner = KeyRing(KEY_A).identify(KEY_A).owner
        assert len(api.store.owned_by(owner)) == 2

    def test_simultaneous_uploads_cannot_pass_the_daily_cap(
            self, make_api: Callable[..., Api],
            monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(daily_upload_cap=1)
        self._slow_ingest(monkeypatch)
        assert self._together(api, [KEY_A, KEY_B]) == [201, 429]
        assert caps._cap.snapshot().uploads == 1

    def test_a_refused_upload_gives_its_daily_slot_back(
            self, make_api: Callable[..., Api]) -> None:
        api = make_api(daily_upload_cap=1)
        assert api.upload("notes.pdf", b"x").status_code == 400
        assert caps._cap.snapshot().uploads == 0
        assert api.upload("a.csv", PEOPLE_CSV).status_code == 201

    def test_delete(self, api: Api) -> None:
        dataset = api.upload("people.csv", PEOPLE_CSV).json()["dataset_id"]
        response = api.delete(f"/datasets/{dataset}")
        assert response.status_code == 204 and response.content == b""
        assert not (api.store.root / dataset).exists()
        again = api.get(f"/datasets/{dataset}/schema")
        assert again.status_code == 404, "deleted, not expired"

    @pytest.mark.parametrize(("filename", "data", "word"), [
        ("notes.pdf", b"%PDF-1.4", "Upload CSV"),
        ("empty.csv", b"", "empty"),
        ("fake.db", b"not a sqlite file" * 10, ""),
        ("old.xls", b"\xd0\xcf\x11\xe0", ".xls"),
    ])
    def test_the_uis_upload_checks_apply(self, api: Api, filename: str, data: bytes,
                                         word: str) -> None:
        response = api.upload(filename, data)
        assert response.status_code == 400
        found = error(response)
        assert found["code"] == "upload_refused" and word in found["message"]
        assert list(api.store.root.iterdir()) == [], "nothing is left on disk"

    def test_a_hostile_sqlite_file_is_refused(self, api: Api, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "hostile.db"
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "a""b; DROP TABLE x" (c int)')
        conn.commit()
        conn.close()
        response = api.upload("hostile.db", path.read_bytes())
        assert response.status_code == 400
        assert error(response)["code"] == "upload_refused"

    def test_the_filename_cannot_leave_the_store(self, api: Api) -> None:
        response = api.upload("../../../evil.csv", PEOPLE_CSV)
        assert response.status_code == 201
        assert response.json()["label"] == "evil.csv"
        assert {p.parent.parent for p in api.store.root.rglob("data.db")} == {
            api.store.root}

    def test_filename_is_required(self, api: Api) -> None:
        response = api.client.post("/v1/datasets", content=PEOPLE_CSV,
                                   headers=_auth(KEY_A))
        assert response.status_code == 422

    def test_descriptions_are_optional(self, api: Api) -> None:
        api.model.error = QuotaExhaustedError("quota")
        made = api.upload("people.csv", PEOPLE_CSV)
        assert made.status_code == 201 and made.json()["descriptions"] == "empty"

    def test_uploads_can_be_switched_off(self, make_api: Callable[..., Api]) -> None:
        api = make_api(uploads_enabled="false")
        response = api.upload("people.csv", PEOPLE_CSV)
        assert response.status_code == 403
        assert error(response)["code"] == "uploads_disabled"

    @pytest.mark.parametrize("dataset_id", ["x", "0" * 31, "G" * 32, "0" * 33])
    def test_malformed_ids_are_404(self, api: Api, dataset_id: str) -> None:
        assert api.get(f"/datasets/{dataset_id}/schema").status_code == 404


# ---------------------------------------------------------------------------
# The daily caps, shared with the UI's counters
# ---------------------------------------------------------------------------
class TestDailyCaps:
    def test_daily_run_cap(self, make_api: Callable[..., Api]) -> None:
        api = make_api(daily_run_cap=1)
        assert api.post("/ask", {"question": "How many customers?"}).status_code == 200
        calls = api.model.calls
        response = api.post("/ask", {"question": "How many customers?"})
        assert response.status_code == 429
        assert error(response)["code"] == "daily_cap"
        assert api.model.calls == calls, "refused before any model call"
        assert api.post("/sql", {"sql": COUNT}).status_code == 200, (
            "SQL runs have their own counter")

    def test_daily_model_call_cap_stops_a_run_midway(
            self, make_api: Callable[..., Api]) -> None:
        api = make_api(daily_llm_call_cap=2)
        response = api.post("/ask", {"question": "How many customers?"})
        assert response.status_code == 429
        assert error(response)["code"] == "daily_cap"
        assert api.model.calls == 2
        assert caps._cap.snapshot().runs == 0, "an unfinished run is not counted"

    def test_daily_sql_cap(self, make_api: Callable[..., Api]) -> None:
        api = make_api(daily_sql_run_cap=1)
        assert api.post("/sql", {"sql": COUNT}).status_code == 200
        response = api.post("/sql", {"sql": COUNT})
        assert response.status_code == 429
        assert error(response)["code"] == "daily_cap"

    def test_daily_upload_cap(self, make_api: Callable[..., Api]) -> None:
        api = make_api(daily_upload_cap=1)
        assert api.upload("a.csv", PEOPLE_CSV).status_code == 201
        response = api.upload("b.csv", PEOPLE_CSV)
        assert response.status_code == 429
        assert error(response)["code"] == "daily_cap"

    def test_the_counters_are_the_uis(self, api: Api) -> None:
        api.post("/ask", {"question": "How many customers?"})
        assert service.deployment_cap() is caps._cap
        snapshot = caps._cap.snapshot()
        assert snapshot.runs == 1 and snapshot.llm_calls == 3


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
class TestLogs:
    def test_one_request_line_each_with_the_route_template(self, api: Api) -> None:
        dataset = api.upload("people.csv", PEOPLE_CSV).json()["dataset_id"]
        api.get(f"/datasets/{dataset}/schema")
        api.post("/sql", {"sql": COUNT}, key=None)
        api.post("/sql", {"sql": "DROP TABLE customers"})
        upload, schema, unauthorized, refused = api.requests()
        client = KeyRing(KEY_A).identify(KEY_A).id
        assert (upload["method"], upload["route"], upload["status"]) == (
            "POST", "/v1/datasets", 201)
        assert schema["route"] == "/v1/datasets/{dataset_id}/schema"
        assert dataset not in json.dumps(schema)
        assert upload["client"] == schema["client"] == refused["client"] == client
        assert unauthorized["status"] == 401 and unauthorized["client"] is None
        assert unauthorized["code"] == "unauthorized"
        assert refused["status"] == 403 and refused["code"] == "sql_refused"
        assert all(line["surface"] == "api" for line in api.requests())

    def test_no_key_ever_reaches_a_log(self, api: Api,
                                       caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            api.post("/ask", {"question": "How many customers?"})
            api.post("/sql", {"sql": COUNT})
            api.post("/sql", {"sql": COUNT}, key=KEY_B)
            api.post("/sql", {"sql": COUNT}, key="wrong-key-0123456789abcdef012345")
            api.upload("people.csv", PEOPLE_CSV)
        everything = "\n".join([*api.texts, caplog.text])
        for secret in (KEY_A, KEY_B, SERVER_MODEL_KEY, "wrong-key-0123456789"):
            assert secret not in everything
        assert KeyRing(KEY_A).identify(KEY_A).id in everything

    def test_content_only_with_trace_content(self, make_api: Callable[..., Api]) -> None:
        api = make_api(trace_content="true")
        api.post("/sql", {"sql": COUNT})
        assert api.runs()[-1]["final_sql"].startswith("SELECT COUNT")


# ---------------------------------------------------------------------------
# The gate fails closed (review findings)
# ---------------------------------------------------------------------------
class TestGateFailsClosed:
    """Every path but health needs a key, whatever prefix a proxy adds."""

    @pytest.mark.parametrize("path", ["/", "/anything", "/v2/ask", "/v1", "/v1/",
                                      "/v1/health/", "/v1/health/x"])
    def test_unknown_paths_need_a_key_too(self, api: Api, path: str) -> None:
        response = api.client.get(path)
        assert response.status_code == 401
        assert error(response)["code"] == "unauthorized"

    def test_a_root_path_prefix_does_not_open_the_routes(
            self, make_api: Callable[..., Api]) -> None:
        """With an ASGI root_path (a proxy prefix), the raw path is
        /api/v1/..., while the router matches /v1/...: the gate must decide
        on the same path the router does."""
        api = make_api()
        prefixed = TestClient(api.client.app, root_path="/api")
        sql = {"sql": COUNT}
        assert prefixed.post("/api/v1/sql", json=sql).status_code == 401
        assert prefixed.post("/api/v1/datasets?filename=a.csv", content=PEOPLE_CSV
                             ).status_code == 401
        big = b"x" * 500_000
        assert prefixed.post("/api/v1/sql", content=big).status_code == 401
        ok = prefixed.post("/api/v1/sql", json=sql, headers=_auth(KEY_A))
        assert ok.status_code == 200 and ok.json()["rows"] == [[1400]]
        over = prefixed.post("/api/v1/sql", headers=_auth(KEY_A),
                             json={"sql": "SELECT 1 -- " + "x" * 70_000})
        assert over.status_code == 413, "the body limit applies under a prefix too"
        assert prefixed.get("/api/v1/health").status_code == 200

    def test_a_route_without_a_client_is_unauthorized_not_a_crash(self) -> None:
        from types import SimpleNamespace

        from agentcrew.api.errors import ApiError
        from agentcrew.api.routes import _client

        with pytest.raises(ApiError) as caught:
            _client(SimpleNamespace(state=SimpleNamespace()))  # type: ignore[arg-type]
        assert caught.value.status == 401


class TestRequestsInProgress:
    def test_a_key_is_limited_in_how_many_run_at_once(
            self, make_api: Callable[..., Api],
            monkeypatch: pytest.MonkeyPatch) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        api = make_api(api_max_concurrent_per_key=2)
        started, release = threading.Semaphore(0), threading.Event()

        def slow_answer(question: str, **_: Any) -> service.Turn:
            started.release()
            release.wait(10)
            return service.Turn(question=question, notice="x", code="write_refused")

        monkeypatch.setattr(service, "answer", slow_answer)
        ask = {"question": "How many customers?"}
        with ThreadPoolExecutor(2) as pool:
            running = [pool.submit(api.post, "/ask", ask) for _ in range(2)]
            assert started.acquire(timeout=10) and started.acquire(timeout=10)
            # Two are in progress: a third from the same key is turned away,
            # another key is not, and health is answered at once.
            third = api.post("/sql", {"sql": COUNT})
            other = api.post("/sql", {"sql": COUNT}, key=KEY_B)
            health = api.client.get("/v1/health")
            release.set()
            done = [f.result(timeout=10).status_code for f in running]
        assert third.status_code == 429
        assert error(third)["code"] == "too_many_concurrent"
        assert third.headers["retry-after"] == "1"
        assert other.status_code == 200 and health.status_code == 200
        assert done == [200, 200]
        assert api.post("/sql", {"sql": COUNT}).status_code == 200, "freed again"

    def test_a_failed_request_frees_its_place(
            self, make_api: Callable[..., Api],
            monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(api_max_concurrent_per_key=1)

        def boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("x")

        monkeypatch.setattr(service, "answer", boom)
        for _ in range(3):
            assert api.post("/ask", {"question": "q"}).status_code == 500
        assert api.post("/sql", {"sql": COUNT}).status_code == 200

    def test_in_flight_unit(self) -> None:
        from agentcrew.api.ratelimit import InFlight

        counter = InFlight(2)
        assert [counter.enter("a") for _ in range(3)] == [True, True, False]
        assert counter.enter("b")
        counter.leave("a")
        assert counter.enter("a")
        for _ in range(5):
            counter.leave("a")
        assert counter._running.get("a") is None, "never negative, no leftover"
        assert all(InFlight(0).enter("a") for _ in range(50))


class TestUploadRefusedBeforeTheFileIsRead:
    @staticmethod
    def _body_must_not_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
        from starlette.requests import Request

        async def body(self: Any) -> bytes:
            raise AssertionError("the upload body was read")

        monkeypatch.setattr(Request, "body", body)

    def test_at_the_dataset_limit(self, api: Api,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
        api.upload("a.csv", PEOPLE_CSV)
        api.upload("b.csv", PEOPLE_CSV)
        self._body_must_not_be_read(monkeypatch)
        response = api.upload("c.csv", PEOPLE_CSV)
        assert response.status_code == 409

    def test_when_uploads_are_off(self, make_api: Callable[..., Api],
                                  monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(uploads_enabled="false")
        self._body_must_not_be_read(monkeypatch)
        assert api.upload("a.csv", PEOPLE_CSV).status_code == 403

    def test_when_the_daily_upload_cap_is_reached(
            self, make_api: Callable[..., Api],
            monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(daily_upload_cap=1)
        api.upload("a.csv", PEOPLE_CSV)
        self._body_must_not_be_read(monkeypatch)
        assert api.upload("b.csv", PEOPLE_CSV).status_code == 429


# ---------------------------------------------------------------------------
# Closed sets: every way a run can end is mapped to an error (review findings)
# ---------------------------------------------------------------------------
class TestEveryOutcomeIsMapped:
    def test_every_turn_code_has_an_api_error(self) -> None:
        from typing import get_args

        from agentcrew.api.routes import _TURN_ERRORS
        from agentcrew.service import TurnCode

        assert set(get_args(TurnCode)) == set(_TURN_ERRORS) | {"write_refused"}

    def test_every_editor_status_but_done_has_an_api_error(self) -> None:
        from typing import get_args

        from agentcrew.api.routes import _sql_failures
        from agentcrew.sql_editor import EditorStatus

        assert set(get_args(EditorStatus)) == set(_sql_failures("m")) | {"done"}

    def test_an_editor_run_must_say_how_it_ended(self) -> None:
        from agentcrew.schemas import ExecutionResult
        from agentcrew.sql_editor import EditorRun

        with pytest.raises(TypeError):
            EditorRun()  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="unknown editor status"):
            EditorRun(status="finished")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="done exactly when"):
            EditorRun(status="done", message="no result")
        good = ExecutionResult(ok=True, sql="SELECT 1", columns=["a"], rows=[[1]],
                               row_count=1)
        with pytest.raises(ValueError, match="done exactly when"):
            EditorRun(status="failed", result=good)
        assert EditorRun(status="done", result=good).ok

    def test_nothing_that_holds_a_secret_prints_it(
            self, make_api: Callable[..., Api]) -> None:
        from agentcrew.config import Settings

        api = make_api()
        state = api.client.app.state.api
        client = KeyRing(KEY_A).identify(KEY_A)
        shown = " ".join([repr(state), repr(client), repr(Settings()),
                          repr(state.settings)])
        assert SERVER_MODEL_KEY not in shown and KEY_A not in shown
        assert client.owner not in shown, "the key's full digest stays out too"
        assert client.id in shown


class TestFailuresAreLogged:
    def test_an_unresolvable_model_is_logged_with_its_type(
            self, api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentcrew.config import Settings

        def boom(self: Any) -> str:
            raise KeyError("SECRET-DETAIL")

        monkeypatch.setattr(Settings, "resolved_model", boom)
        response = api.post("/ask", {"question": "How many customers?"})
        assert response.status_code == 503
        assert error(response)["code"] == "not_configured"
        assert any("model not resolved: KeyError" in text for text in api.texts)
        assert not any("SECRET-DETAIL" in text for text in api.texts)

    def test_descriptions_are_drafted_only_if_they_were_stored(
            self, api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(type(api.store), "update",
                            lambda self, *a, **k: None)
        made = api.upload("college.xlsx", COLLEGE)
        assert made.status_code == 201
        assert made.json()["descriptions"] == "empty"
        assert any("no dataset to go to" in text for text in api.texts)


# ---------------------------------------------------------------------------
# The gate, called as ASGI: what it reads, and when (review findings)
# ---------------------------------------------------------------------------
async def _call(app: Any, *, path: str, method: str = "POST", key: str | None = None,
                headers: list[tuple[bytes, bytes]] | None = None,
                chunks: int = 100, chunk: bytes = b"x" * 8_000,
                query: bytes = b"") -> tuple[int, dict[str, Any], int]:
    """Send one request straight to the ASGI app with a body that arrives in
    chunks and never declares its length. Returns (status, json, how many
    times the app asked for more of the body)."""
    asked = 0
    sent: list[dict[str, Any]] = []
    head = list(headers or [])
    if key is not None:
        head.append((b"x-api-key", key.encode()))

    async def receive() -> dict[str, Any]:
        nonlocal asked
        asked += 1
        return {"type": "http.request", "body": chunk,
                "more_body": asked < chunks}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "path": path, "raw_path": path.encode(),
             "root_path": "", "scheme": "http", "query_string": query,
             "headers": head, "client": ("testclient", 1), "server": ("test", 80)}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], (json.loads(body) if body else {}), asked


class TestWhatTheGateReads:
    def _app(self, api: Api) -> Any:
        return api.client.app

    def test_no_key_means_the_body_is_never_asked_for(self, api: Api) -> None:
        import anyio

        for path in ("/v1/sql", "/v1/ask", "/v1/datasets", "/somewhere/else"):
            status, body, asked = anyio.run(
                lambda path=path: _call(self._app(api), path=path))
            assert (status, body["error"]["code"], asked) == (401, "unauthorized", 0)

    def test_a_rate_limited_request_is_not_read_either(
            self, make_api: Callable[..., Api]) -> None:
        import anyio

        api = make_api(per_minute=1)
        assert api.post("/sql", {"sql": COUNT}).status_code == 200
        status, body, asked = anyio.run(
            lambda: _call(self._app(api), path="/v1/sql", key=KEY_A))
        assert (status, body["error"]["code"], asked) == (429, "rate_limited", 0)

    def test_a_json_body_is_cut_at_the_chunk_that_crosses_the_limit(
            self, api: Api) -> None:
        """64 KB limit, 8 KB chunks: the app may ask nine times, not a hundred."""
        import anyio

        status, body, asked = anyio.run(lambda: _call(
            self._app(api), path="/v1/sql", key=KEY_A,
            headers=[(b"content-type", b"application/json")]))
        assert (status, body["error"]["code"]) == (413, "body_too_large")
        assert asked == 9, "64 KB / 8 KB, plus the chunk that crossed the line"
        assert api.runs() == []

    def test_an_upload_is_cut_while_it_is_read_and_charges_nothing(
            self, make_api: Callable[..., Api]) -> None:
        import anyio

        api = make_api(max_upload_mb=0.01, max_excel_upload_mb=0.01)  # 10 KB
        status, body, asked = anyio.run(lambda: _call(
            self._app(api), path="/v1/datasets", key=KEY_A,
            query=b"filename=a.csv", chunk=b"1,2\n" * 250, chunks=30))
        assert (status, body["error"]["code"]) == (413, "body_too_large")
        assert asked <= 12, "10 KB limit, 1 KB chunks"
        owner = KeyRing(KEY_A).identify(KEY_A).owner
        assert api.store.owned_by(owner) == []
        assert caps._cap is None or caps._cap.snapshot().uploads == 0
        assert api.store.remaining_bytes() == api.store.max_bytes

    def test_only_http_is_served(self, api: Api) -> None:
        """The gate passes other ASGI scopes through untouched, so there must
        be nothing for them to reach: a websocket route would be open."""
        from starlette.routing import Mount, WebSocketRoute

        routes = self._app(api).routes
        assert not [r for r in routes if isinstance(r, (WebSocketRoute, Mount))]
        with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal will do
            with api.client.websocket_connect("/v1/ask"):
                pass


class TestSlotsAndReservationsComeBack:
    def test_refused_requests_do_not_hold_a_keys_place(
            self, make_api: Callable[..., Api]) -> None:
        api = make_api(api_max_concurrent_per_key=1)
        for _ in range(4):
            over = api.post("/sql", {"sql": "SELECT 1 -- " + "x" * 70_000})
            assert over.status_code == 413
            assert api.post("/sql", {"sql": "DROP TABLE customers"}).status_code == 403
            assert api.post("/ask", {}).status_code == 422
        assert api.post("/sql", {"sql": COUNT}).status_code == 200

    def test_an_unexpected_failure_while_uploading_gives_everything_back(
            self, make_api: Callable[..., Api],
            monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(daily_upload_cap=1)

        def disk_full() -> Any:
            raise OSError("disk full: SECRET-DETAIL")

        real_new_folder = api.store.new_folder
        monkeypatch.setattr(api.store, "new_folder", disk_full)
        response = api.upload("a.csv", PEOPLE_CSV)
        assert response.status_code == 500
        assert error(response)["code"] == "internal_error"
        assert "SECRET-DETAIL" not in response.text
        assert not any("SECRET-DETAIL" in text for text in api.texts)
        assert caps._cap.snapshot().uploads == 0, "the daily slot came back"
        assert api.store.remaining_bytes() == api.store.max_bytes, "and the room"
        monkeypatch.setattr(api.store, "new_folder", real_new_folder)
        assert api.upload("a.csv", PEOPLE_CSV).status_code == 201, (
            "the one upload of the day is still available")

    def test_a_csv_over_its_own_limit_is_refused_by_the_upload_checks(
            self, make_api: Callable[..., Api]) -> None:
        """The gate allows the larger of the two upload limits; the kind's own
        limit is then applied by the same check as the UI's."""
        api = make_api(max_upload_mb=0.01, max_excel_upload_mb=1)
        response = api.upload("big.csv", b"a,b\n" + b"1,2\n" * 5_000)  # 20 KB
        assert response.status_code == 400
        assert error(response)["code"] == "upload_refused"

    def test_no_key_or_digest_reaches_a_log_on_failure_paths(
            self, make_api: Callable[..., Api],
            capfd: pytest.CaptureFixture[str],
            caplog: pytest.LogCaptureFixture,
            monkeypatch: pytest.MonkeyPatch) -> None:
        api = make_api(per_minute=6)
        with caplog.at_level(logging.DEBUG):
            api.client.post(f"/v1/sql?api_key={KEY_A}", json={"sql": COUNT})
            api.client.get(f"/v1/{KEY_A}")
            api.get(f"/{KEY_B}")  # an unknown path that holds a key, with a key
            api.post("/sql", {"sql": "SELECT 1 -- " + "x" * 70_000})
            api.upload("notes.pdf", b"x")
            monkeypatch.setattr(service, "answer",
                                lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
            api.post("/ask", {"question": "q"})
            for _ in range(8):
                api.post("/sql", {"sql": COUNT})
        out, err = capfd.readouterr()
        # The test's own HTTP client logs the URLs it requests; that is the
        # caller's log, not the server's, so it is left out.
        server_side = [r.getMessage() for r in caplog.records
                       if not r.name.startswith("httpx")]
        everything = "\n".join([*api.texts, *server_side, out, err])
        digest = KeyRing(KEY_A).identify(KEY_A).owner.removeprefix("api:")
        for secret in (KEY_A, KEY_B, SERVER_MODEL_KEY, digest):
            assert secret not in everything
        unmatched = [line for line in api.requests() if line["status"] == 404]
        assert unmatched and all(line["route"] == "-" for line in unmatched)
        assert {line["status"] for line in api.requests()} >= {401, 404, 413, 400,
                                                               500, 429}
