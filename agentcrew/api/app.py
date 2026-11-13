"""The API application and the gate every request passes.

    uvicorn agentcrew.api.app:create_app --factory --host 0.0.0.0 --port 8080

One process, one worker: the daily caps, the rate limiter and the dataset
store are in memory (agentcrew/caps.py says why), so a second worker would
be a second set of limits.

The gate
--------
`Gate` is a plain ASGI middleware, outside the framework's routing and body
parsing, because the order matters and a route cannot enforce it:

1. The key is checked from the headers. A request without a valid key is
   answered 401 without its body being read at all.
2. The key's rate limit is checked (429 with Retry-After).
3. The body limit is enforced while the body is read: a declared
   Content-Length over the limit is refused before reading, and a body that
   lies about its length (or sends none) is cut off at the first chunk that
   crosses the line. Nothing is buffered and then measured.
4. Any exception that escapes a route becomes the 500 envelope here, so no
   stack trace can reach a caller, whatever the framework would have done.
5. One request line is logged (agentcrew/runlog.py), with the caller's key
   hash, the route template, the status and the error code.

There is no CORS middleware: the API sends no Access-Control headers, so a
browser on another origin cannot read its responses. It is meant to be
called from servers and scripts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentcrew import runlog
from agentcrew.api import errors
from agentcrew.api.auth import Client, KeyRing, presented_key
from agentcrew.api.errors import ApiError, envelope
from agentcrew.api.ratelimit import RateLimiter
from agentcrew.config import Settings

log = logging.getLogger("agentcrew.app")

PREFIX = "/v1"
HEALTH_PATH = f"{PREFIX}/health"
UPLOAD_PATH = f"{PREFIX}/datasets"
KNOWN_DATASETS = 4096

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class Known:
    """Which key uploaded which dataset id, kept after the dataset is gone,
    so its owner can be told "expired" rather than "no such dataset". Any
    other key is told "no such dataset" either way. Bounded: the oldest ids
    are forgotten first."""

    def __init__(self, limit: int = KNOWN_DATASETS) -> None:
        self._limit = limit
        self._owners: OrderedDict[str, str] = OrderedDict()

    def add(self, dataset_id: str, owner: str) -> None:
        self._owners[dataset_id] = owner
        while len(self._owners) > self._limit:
            self._owners.popitem(last=False)

    def owner(self, dataset_id: str) -> str | None:
        return self._owners.get(dataset_id)

    def forget(self, dataset_id: str) -> None:
        self._owners.pop(dataset_id, None)


@dataclass
class ApiState:
    """What the gate and the routes share. Built once per application."""

    settings: Settings
    """The deployment's settings, with the dataset lifetime set to the
    API's (api_dataset_ttl_seconds)."""
    keys: KeyRing
    limiter: RateLimiter
    known: Known = field(default_factory=Known)

    @property
    def json_limit(self) -> int:
        return int(self.settings.api_max_json_kb * 1024)

    @property
    def upload_limit(self) -> int:
        """The larger of the two upload limits; ingest then applies the one
        for the file's kind, exactly as it does for the UI."""
        s = self.settings
        return int(max(s.max_upload_mb, s.max_excel_upload_mb) * 1e6)


class _BodyTooLarge(Exception):
    pass


class Gate:
    """Authentication, rate limit, body limit, last-resort error handling and
    the request log, in that order (the module docstring says why here)."""

    def __init__(self, app: Any, *, api: ApiState) -> None:
        self.app = app
        self.api = api

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:16]
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        path, method = scope.get("path", ""), scope.get("method", "")
        seen = {"status": 0, "started": False, "over": False, "replaced": False}
        base_headers = [
            (b"x-request-id", request_id.encode()),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]

        async def respond(error: ApiError) -> None:
            """Send the envelope straight from the gate."""
            body = json.dumps(envelope(error.code, error.message, request_id),
                              separators=(",", ":")).encode()
            seen.update(status=error.status, started=True)
            state["error_code"] = error.code
            extra = [(k.lower().encode(), v.encode())
                     for k, v in error.headers.items()]
            await send({"type": "http.response.start", "status": error.status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode()),
                                    *base_headers, *extra]})
            await send({"type": "http.response.body", "body": body})

        too_large = ApiError(413, "body_too_large", errors.BODY_TOO_LARGE)
        client: Client | None = None
        limit = 0
        try:
            if path.startswith(f"{PREFIX}/") and path != HEALTH_PATH:
                client = self.api.keys.identify(presented_key(scope["headers"]))
                if client is None:
                    await respond(ApiError(401, "unauthorized", errors.UNAUTHORIZED))
                    return
                state["client"] = client
                wait = self.api.limiter.check(client.id)
                if wait:
                    await respond(ApiError(429, "rate_limited", errors.RATE_LIMITED,
                                           {"Retry-After": str(wait)}))
                    return
                limit = (self.api.upload_limit
                         if method == "POST" and path == UPLOAD_PATH
                         else self.api.json_limit)
                if _declared_length(scope["headers"]) > limit:
                    await respond(too_large)
                    return

            read = 0

            async def bounded_receive() -> Message:
                nonlocal read
                message = await receive()
                if limit and message["type"] == "http.request":
                    read += len(message.get("body", b""))
                    if read > limit:
                        seen["over"] = True
                        raise _BodyTooLarge
                return message

            async def tracked_send(message: Message) -> None:
                if seen["replaced"]:
                    return  # the route's own reply to an over-long body
                if message["type"] == "http.response.start":
                    if seen["over"]:
                        # Whatever the framework made of the interrupted
                        # read, the caller gets the 413 envelope.
                        seen["replaced"] = True
                        await respond(too_large)
                        return
                    seen.update(status=message["status"], started=True)
                    message = {**message, "headers": [
                        *message.get("headers", []), *base_headers]}
                await send(message)

            try:
                await self.app(scope, bounded_receive, tracked_send)
            except _BodyTooLarge:
                pass
            except Exception as exc:
                # The type only: an exception's text can hold request data.
                log.error("api request failed: %s", type(exc).__name__)
                if not seen["started"]:
                    await respond(ApiError(500, "internal_error", errors.INTERNAL))
            if seen["over"] and not seen["started"]:
                await respond(too_large)
        finally:
            if path != HEALTH_PATH:
                runlog.emit_request(runlog.RequestRecord(
                    method=method,
                    route=_route_template(scope, path),
                    status=int(seen["status"]),
                    seconds=time.perf_counter() - started,
                    client=client.id if client else None,
                    code=state.get("error_code"),
                    request_id=request_id,
                ))


def _route_template(scope: Scope, path: str) -> str:
    """The matched route with its parameters as names
    (/v1/datasets/{dataset_id}/schema), or "-" when no route matched: the
    log never holds a raw path, which a caller controls."""
    if scope.get("route") is None:
        return "-"
    for name, value in (scope.get("path_params") or {}).items():
        path = path.replace(str(value), "{" + name + "}")
    return path


def _declared_length(headers: list[tuple[bytes, bytes]]) -> int:
    """Content-Length as an int; 0 when absent or not a number (the body is
    then counted as it is read)."""
    for name, value in headers:
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    request.state.error_code = error.code
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(envelope(error.code, error.message, request_id),
                        status_code=error.status, headers=error.headers)


def _invalid(exc: RequestValidationError) -> str:
    """One sentence about the first problem: where and what, never the value
    that was sent."""
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    what = str(first.get("msg", "invalid"))
    return f"{where}: {what}" if where else what


def configure_logging() -> None:
    """The same two streams as the Streamlit app: the JSON run log on stdout
    and one plain line per event on stderr."""
    runlog.configure()
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


def create_app(settings: Settings | None = None, *,
               limiter: RateLimiter | None = None) -> FastAPI:
    """Build the application. `settings` and `limiter` are for tests; a
    deployment reads everything from the environment."""
    from agentcrew.api.routes import router

    configure_logging()
    base = settings or Settings()
    settings = base.model_copy(
        update={"dataset_ttl_seconds": base.api_dataset_ttl_seconds})
    keys = KeyRing(settings.api_keys.get_secret_value()
                   if settings.api_keys else None)
    if keys.ignored:
        log.warning("api: %d configured key(s) ignored: shorter than the minimum",
                    keys.ignored)
    if not len(keys):
        log.warning("api: AGENTCREW_API_KEYS has no usable key; every request "
                    "except %s will be refused", HEALTH_PATH)
    api = ApiState(
        settings=settings, keys=keys,
        limiter=limiter or RateLimiter(settings.api_rate_limit_per_minute))

    docs = settings.api_docs
    app = FastAPI(
        title="AgentCrew API", version="1",
        docs_url="/docs" if docs else None, redoc_url=None,
        openapi_url="/openapi.json" if docs else None)
    app.state.api = api
    app.add_middleware(Gate, api=api)
    app.include_router(router, prefix=PREFIX)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request,
                          exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request, ApiError(422, "invalid_request", _invalid(exc)))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = errors.STATUS_CODES.get(
            exc.status_code, ("error", "The request could not be handled."))
        return _error_response(request, ApiError(exc.status_code, code, message))

    return app
