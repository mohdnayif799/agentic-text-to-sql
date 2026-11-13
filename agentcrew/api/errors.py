"""One error envelope for every failure.

    {"error": {"code": "rate_limited", "message": "...", "request_id": "..."}}

`code` is stable and meant for programs; `message` is one plain sentence
meant for a person. Neither ever carries a stack trace, an exception's
class or text, a key, or another caller's data. The request id is also in
the X-Request-ID header and in the request's log line, so a caller can
quote it and an operator can find the line.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """A failure with a status, a code and a sentence that is safe to show."""

    def __init__(self, status: int, code: str, message: str,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def envelope(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


# ---- The failures every route can meet -------------------------------------------
UNAUTHORIZED = "Send a valid API key in the X-API-Key header."
RATE_LIMITED = "Too many requests for this API key. Try again shortly."
BODY_TOO_LARGE = "The request body is larger than this endpoint accepts."
INTERNAL = "Something went wrong on the server."
NOT_FOUND = "No such endpoint."
METHOD_NOT_ALLOWED = "That method is not allowed on this endpoint."
DAILY_CAP = "The API has reached its daily limit. Try again tomorrow."
DATASET_NOT_FOUND = "No such dataset."
DATASET_EXPIRED = (
    "That dataset expired after {minutes} minutes without use and was deleted. "
    "Upload it again."
)

#: Codes for statuses raised by the framework itself (unknown path, wrong
#: method), so those failures use the envelope too.
STATUS_CODES = {
    404: ("not_found", NOT_FOUND),
    405: ("method_not_allowed", METHOD_NOT_ALLOWED),
}


def daily_cap() -> ApiError:
    return ApiError(429, "daily_cap", DAILY_CAP)


def dataset_not_found() -> ApiError:
    return ApiError(404, "dataset_not_found", DATASET_NOT_FOUND)
