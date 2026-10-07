"""Rate-limit and overload handling for live runs.

A per-minute 429 is waited out with exponential backoff (honouring the
provider's "retry in Ns" hint when it gives one). A daily-quota 429, or a
per-minute limit that persists through every retry, raises DailyLimitReached
so the runner can save progress and stop; the run resumes later from cache.

A provider error (HTTP 5xx, UNAVAILABLE / "high demand", or a transport
timeout) is retried after 10, 30 and 60 s: the request never reached the
model's reasoning, so it says nothing about the architecture. One that
persists raises ProviderUnavailable; the runner marks the question not
evaluated and a later run resumes it (protocol v2, pre-run amendment 3).
Any other error (a 4xx, invalid model output) is raised at once.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

from agentcrew.llm import LLMError, QuotaExhaustedError

T = TypeVar("T")

DELAYS = (4.0, 8.0, 16.0, 32.0, 64.0, 64.0)
TRANSIENT_DELAYS = (10.0, 30.0, 60.0)
_STATUS_5XX = re.compile(r"\b5\d\d\s+[A-Z][A-Z_]+")  # "503 UNAVAILABLE", "500 INTERNAL"
_PROVIDER_WORDS = ("unavailable", "high demand", "overloaded", "internal server error",
                   "bad gateway", "gateway timeout", "timed out", "timeout",
                   "deadline exceeded", "connection reset", "connection aborted")
_DAILY = ("perday", "per day", "per_day", "daily")
_HINTS = (
    re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE),
    re.compile(r"retrydelay['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)s",
               re.IGNORECASE),
)


class DailyLimitReached(RuntimeError):
    """The provider's daily quota is used up (or the rate limit never cleared)."""


class ProviderUnavailable(RuntimeError):
    """A provider error persisted through every retry (5xx, UNAVAILABLE or a
    transport timeout): the question is not evaluated and resumes later."""


def is_daily_limit(message: str) -> bool:
    folded = message.lower().replace("-", "")
    return any(marker in folded for marker in _DAILY)


def is_provider_error(exc: BaseException) -> bool:
    """HTTP 5xx, UNAVAILABLE or a transport timeout, anywhere in the cause chain
    (SDK exceptions carry a status code; wrapped ones keep it in the text)."""
    seen: BaseException | None = exc
    while seen is not None:
        for attr in ("status_code", "code", "http_status"):
            value = getattr(seen, attr, None)
            if isinstance(value, int) and 500 <= value <= 599:
                return True
        if isinstance(seen, TimeoutError | ConnectionError) \
                or "timeout" in type(seen).__name__.lower():
            return True
        seen = seen.__cause__ or seen.__context__
    text = str(exc)
    return bool(_STATUS_5XX.search(text)) or any(
        word in text.lower() for word in _PROVIDER_WORDS)


def retry_hint(message: str) -> float | None:
    for pattern in _HINTS:
        found = pattern.search(message)
        if found:
            return float(found.group(1))
    return None


def call_with_backoff(
    fn: Callable[[], T],
    *,
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = DELAYS,
    transient_delays: tuple[float, ...] = TRANSIENT_DELAYS,
) -> tuple[T, float]:
    """Run `fn`, waiting out per-minute rate limits and transient overloads.
    Returns (value, seconds slept)."""
    slept = 0.0
    quota_tries = transient_tries = 0
    while True:
        try:
            return fn(), slept
        except QuotaExhaustedError as exc:
            message = str(exc)
            if is_daily_limit(message):
                raise DailyLimitReached(message) from exc
            if quota_tries == len(delays):
                raise DailyLimitReached(
                    f"rate limit persisted through {quota_tries} retries: {message}"
                ) from exc
            wait = max(delays[quota_tries], (retry_hint(message) or 0.0) + 1.0)
            quota_tries += 1
        except LLMError as exc:
            if not is_provider_error(exc):
                raise
            if transient_tries == len(transient_delays):
                raise ProviderUnavailable(
                    f"provider error persisted through {transient_tries} retries: "
                    f"{exc}") from exc
            wait = transient_delays[transient_tries]
            transient_tries += 1
        sleep(wait)
        slept += wait
