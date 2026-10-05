"""Rate-limit and overload handling for live runs.

A per-minute 429 is waited out with exponential backoff (honouring the
provider's "retry in Ns" hint when it gives one). A daily-quota 429, or a
per-minute limit that persists through every retry, raises DailyLimitReached
so the runner can save progress and stop; the run resumes later from cache.

A transient overload (503 / UNAVAILABLE / "high demand") is retried a few
times as well: the request never reached the model's reasoning, so it says
nothing about the architecture. One that persists is raised, and the runner
records the run as an ERROR (with its type and stage), never as an ordinary
failure.
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
_TRANSIENT = ("503", "unavailable", "high demand", "overloaded")
_DAILY = ("perday", "per day", "per_day", "daily")
_HINTS = (
    re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE),
    re.compile(r"retrydelay['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)s",
               re.IGNORECASE),
)


class DailyLimitReached(RuntimeError):
    """The provider's daily quota is used up (or the rate limit never cleared)."""


def is_daily_limit(message: str) -> bool:
    folded = message.lower().replace("-", "")
    return any(marker in folded for marker in _DAILY)


def is_transient(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT)


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
            if not is_transient(str(exc)) or transient_tries == len(transient_delays):
                raise
            wait = transient_delays[transient_tries]
            transient_tries += 1
        sleep(wait)
        slept += wait
