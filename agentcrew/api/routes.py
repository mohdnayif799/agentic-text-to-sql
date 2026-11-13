"""The endpoints under /v1. Each is a thin caller of the shared core.

    GET    /health                  no key
    POST   /ask                     service.answer          (the UI's ask flow)
    POST   /sql                     service.run_sql_on      (sql_editor.run_sql)
    POST   /datasets                datasets.ingest         (the UI's converter)
    GET    /datasets/{id}/schema
    DELETE /datasets/{id}

By the time a route runs, the gate (app.py) has checked the key, the rate
limit and the body size, and put the caller on `request.state.client`.

What each request is charged, the same counters as the UI:
* /ask: the daily run cap and, per model call, the daily call cap; on the
  server's key with the server-key budgets (service.answer).
* /sql: the daily SQL-run counter (sql_editor.run_sql).
* POST /datasets: the daily upload cap, and the daily call cap for the
  calls that draft descriptions.

The routes that run the agent or a query are plain functions, so the
framework runs them in its thread pool and the event loop stays free.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from agentcrew import service
from agentcrew.api import errors
from agentcrew.api.app import ApiState
from agentcrew.api.auth import Client
from agentcrew.api.errors import ApiError
from agentcrew.api.schemas import (
    AskRequest,
    AskResponse,
    ColumnSchema,
    DatasetCreated,
    DatasetSchema,
    Health,
    JoinSchema,
    SqlRequest,
    SqlResponse,
    TableSchema,
    table,
)
from agentcrew.datasets.errors import UploadError
from agentcrew.datasets.ingest import STORE_FULL, ingest
from agentcrew.datasets.source import DataSource, postgres_source, upload_source
from agentcrew.datasets.store import Dataset
from agentcrew.refusal import WRITE_REFUSAL
from agentcrew.service import Caller

router = APIRouter()

_DATASET_ID = re.compile(r"[0-9a-f]{32}")

NOT_CONFIGURED = "The API has no model key configured, so it cannot answer questions."
UNAVAILABLE = "The service is temporarily unavailable. Try again later."
MODEL_QUOTA = "The model provider's quota is used up for now. Try again later."
MODEL_TIMEOUT = "The model is responding slowly right now. Try again in a minute."
AGENT_ERROR = "The agent could not complete this request."
UPLOADS_DISABLED = "Uploads are switched off on this deployment."
DATASET_LIMIT = (
    "This API key already holds {limit} dataset(s), the most allowed. Delete "
    "one, or wait for it to expire."
)
SQL_DAILY_CAP = "SQL runs have reached their daily limit. Try again tomorrow."

#: service.Turn.code -> the API's error for it.
_TURN_ERRORS: dict[str, tuple[int, str, str]] = {
    "no_model": (503, "not_configured", NOT_CONFIGURED),
    "no_key": (503, "not_configured", NOT_CONFIGURED),
    "daily_cap": (429, "daily_cap", errors.DAILY_CAP),
    "session_limit": (429, "daily_cap", errors.DAILY_CAP),
    "quota": (503, "model_quota", MODEL_QUOTA),
    "timeout": (504, "model_timeout", MODEL_TIMEOUT),
    "start_failed": (503, "unavailable", UNAVAILABLE),
    "error": (500, "agent_error", AGENT_ERROR),
}


def _api(request: Request) -> ApiState:
    return request.app.state.api


def _client(request: Request) -> Client:
    return request.state.client


def _caller(client: Client) -> Caller:
    return Caller(surface="api", client=client.id)


# ---- Datasets a key owns -----------------------------------------------------------
def _dataset(api: ApiState, client: Client, dataset_id: str) -> Dataset:
    """The caller's dataset, marked used. Another key's dataset does not
    exist as far as this caller can tell (404). The caller's own dataset
    that expired is 410, so the fix (upload again) is clear."""
    if not _DATASET_ID.fullmatch(dataset_id):
        raise errors.dataset_not_found()
    store = service.store(api.settings)
    store.sweep()  # expiry is exact, not up to a sweeper interval late
    found = store.get(dataset_id, client.owner)
    if found is not None:
        return found
    if api.known.owner(dataset_id) == client.owner:
        minutes = max(1, api.settings.api_dataset_ttl_seconds // 60)
        raise ApiError(410, "dataset_expired",
                       errors.DATASET_EXPIRED.format(minutes=minutes))
    raise errors.dataset_not_found()


def _source(api: ApiState, client: Client,
            dataset_id: str | None) -> DataSource | None:
    """What a request reads from: the caller's dataset, else the deployment's
    default (the self-hosted database when one is configured, else None,
    which is NorthStar)."""
    settings = api.settings
    if dataset_id is not None:
        return upload_source(_dataset(api, client, dataset_id), settings)
    if settings.database_url:
        try:
            return postgres_source(settings)
        except Exception as exc:
            service.log.warning("api: database unavailable: %s", type(exc).__name__)
            raise ApiError(503, "unavailable", UNAVAILABLE) from None
    return None


# ---- Endpoints ---------------------------------------------------------------------
@router.get("/health", response_model=Health)
def health() -> Health:
    """Liveness only. Says nothing about keys, caps or data."""
    return Health()


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request) -> AskResponse:
    api, client = _api(request), _client(request)
    source = _source(api, client, body.dataset_id)
    settings = api.settings
    try:
        model = settings.resolved_model()
    except Exception:
        raise ApiError(503, "not_configured", NOT_CONFIGURED) from None
    turn = service.answer(
        body.question, provider=settings.provider, model=model, own_key="",
        max_attempts=service.DEMO_MAX_ATTEMPTS, max_steps=service.DEMO_MAX_STEPS,
        state=None, source=source, caller=_caller(client))
    return _ask_response(turn, body.dataset_id, settings.max_result_rows)


def _ask_response(turn: service.Turn, dataset_id: str | None,
                  max_rows: int) -> AskResponse:
    if turn.code == "write_refused":
        return AskResponse(status="refused", answer=WRITE_REFUSAL,
                           dataset_id=dataset_id)
    if turn.code is not None:
        status, code, message = _TURN_ERRORS.get(
            turn.code, (500, "agent_error", AGENT_ERROR))
        raise ApiError(status, code, message)
    if turn.arch_run is not None:
        run, outcome = turn.arch_run, turn.arch_run.outcome
        selected = outcome.selected
        status = outcome.status if outcome.status in ("done", "refused") else "failed"
        return AskResponse(
            status=status,
            answer=service.redact(outcome.answer or "", ""),
            sql=selected.sql if selected is not None else None,
            path=outcome.path, llm_calls=run.llm_calls,
            caveats=list(outcome.caveats), dataset_id=dataset_id,
            seconds=round(run.seconds, 3), **table(selected, max_rows))
    if turn.outcome is None:
        raise ApiError(500, "agent_error", AGENT_ERROR)
    # The original agent (AGENTCREW_ARCH=v1): its last step is the result.
    outcome = turn.outcome
    last = outcome.step_results[-1] if outcome.step_results else None
    trace = outcome.trace
    status = (outcome.status if outcome.status in ("done", "needs_clarification")
              else "failed")
    return AskResponse(
        status=status, answer=service.redact(outcome.answer or "", ""),
        sql=last.sql if last is not None else None, path="v1",
        llm_calls=int(trace["counters"].get("llm_calls", 0)),
        caveats=list(outcome.caveats), dataset_id=dataset_id,
        seconds=round(float(trace.get("elapsed_seconds", 0.0)), 3),
        **table(last.result if last is not None else None, max_rows))


@router.post("/sql", response_model=SqlResponse)
def sql(body: SqlRequest, request: Request) -> SqlResponse:
    api, client = _api(request), _client(request)
    source = _source(api, client, body.dataset_id)
    # session_limit 0: an API key has no browser session; its rate limit and
    # the daily SQL counter bound it instead.
    run, max_rows = service.run_sql_on(body.sql, source, state={}, session_limit=0,
                                       caller=_caller(client))
    if run.ok and run.result is not None:
        return SqlResponse(sql=run.result.sql, dataset_id=body.dataset_id,
                           seconds=round(run.result.duration_ms / 1000, 3),
                           **table(run.result, max_rows))
    message = run.message or AGENT_ERROR
    failures: dict[str, tuple[int, str, str]] = {
        "refused": (403, "sql_refused", message),
        "failed": (400, "sql_error", message),
        "invalid": (422, "invalid_request", message),
        "daily_cap": (429, "daily_cap", SQL_DAILY_CAP),
        "start_failed": (503, "unavailable", UNAVAILABLE),
    }
    status, code, text = failures.get(run.status, (500, "internal_error",
                                                   errors.INTERNAL))
    raise ApiError(status, code, text)


@router.post("/datasets", response_model=DatasetCreated, status_code=201)
async def create_dataset(
    request: Request,
    filename: Annotated[str, Query(min_length=1, max_length=200)],
) -> DatasetCreated:
    """The file is the raw request body; its name (which decides CSV, Excel
    or SQLite) is the `filename` query parameter. The gate has already
    stopped the body at the upload limit while it was read."""
    data = await request.body()
    return await run_in_threadpool(_create_dataset, _api(request),
                                   _client(request), filename, data)


_upload_locks: dict[str, threading.Lock] = {}
_upload_locks_guard = threading.Lock()


def _upload_lock(owner: str) -> threading.Lock:
    """One lock per key (so at most as many as there are keys): a key's
    uploads run one at a time, which makes "count, then add" a single step."""
    with _upload_locks_guard:
        return _upload_locks.setdefault(owner, threading.Lock())


def _create_dataset(api: ApiState, client: Client, filename: str,
                    data: bytes) -> DatasetCreated:
    settings = api.settings
    if not settings.uploads_enabled:
        raise ApiError(403, "uploads_disabled", UPLOADS_DISABLED)
    store = service.store(settings)
    cap = service.deployment_cap()
    limit = settings.api_datasets_per_key
    # Requests run in a thread pool, so two uploads from one key can arrive
    # together. Checking a limit and counting against it must be one step, or
    # both would pass the check: the per-key count is taken under the key's
    # lock, held until the dataset is in the store, and the daily cap is
    # reserved (checked and counted at once) and given back on failure.
    with _upload_lock(client.owner):
        store.sweep()
        if len(store.owned_by(client.owner)) >= limit:
            raise ApiError(409, "dataset_limit", DATASET_LIMIT.format(limit=limit))
        if not cap.reserve_upload():
            raise errors.daily_cap()
        try:
            # The same converter and checks as the UI's upload
            # (datasets/ingest.py).
            dataset = ingest(store, owner=client.owner, settings=settings,
                             files=[(Path(filename).name, data)])
        except UploadError as exc:
            cap.release_upload()
            if str(exc) == STORE_FULL:
                raise ApiError(503, "store_full", STORE_FULL) from None
            raise ApiError(400, "upload_refused", str(exc)) from None
        except BaseException:
            cap.release_upload()
            raise
    api.known.add(dataset.id, client.owner)

    described = False
    try:
        describe = service.describer(provider=settings.provider,
                                     model=settings.resolved_model(), own_key="",
                                     caller=_caller(client))
        notes, _ = describe(upload_source(dataset, settings).tool_context(settings))
        if notes is not None:
            dataset = store.update(dataset.id, client.owner, notes=notes) or dataset
            described = True
    except Exception as exc:
        # Descriptions are a help, not a requirement: the dataset stands.
        service.log.warning("api: descriptions not drafted: %s", type(exc).__name__)
    return DatasetCreated(
        dataset_id=dataset.id, label=dataset.label, kind=dataset.kind,
        tables=list(dataset.tables), size_bytes=dataset.size_bytes,
        descriptions="drafted" if described else "empty",
        expires_after_seconds=settings.api_dataset_ttl_seconds)


@router.get("/datasets/{dataset_id}/schema", response_model=DatasetSchema)
def dataset_schema(dataset_id: str, request: Request) -> DatasetSchema:
    api, client = _api(request), _client(request)
    dataset = _dataset(api, client, dataset_id)
    source = upload_source(dataset, api.settings)
    notes = source.notes
    tables: list[TableSchema] = []
    for name in sorted(source.catalog.tables):
        found: Any = source.catalog.tables[name]
        tables.append(TableSchema(
            name=name, description=notes.tables.get(name, ""),
            row_meaning=notes.grains.get(name, ""), row_count=found.row_count,
            columns=[ColumnSchema(name=c.name, type=str(c.type),
                                  description=notes.columns.get((name, c.name), ""))
                     for c in found.columns]))
    joins = [JoinSchema(from_table=j.from_table, from_column=j.from_column,
                        to_table=j.to_table, to_column=j.to_column, reason=j.reason)
             for j in source.joins]
    return DatasetSchema(
        dataset_id=dataset.id, label=dataset.label, kind=dataset.kind,
        tables=tables, joins=joins,
        expires_after_seconds=api.settings.api_dataset_ttl_seconds)


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: str, request: Request) -> Response:
    api, client = _api(request), _client(request)
    dataset = _dataset(api, client, dataset_id)
    service.store(api.settings).remove(dataset.id)
    api.known.forget(dataset.id)
    return Response(status_code=204)
