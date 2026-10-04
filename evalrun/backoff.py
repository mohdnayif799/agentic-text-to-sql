"""Rate-limit handling for live runs.

A per-minute 429 is waited out with exponential backoff (honouring the
provider's "retry in Ns" hint when it gives one). A daily-quota 429, or a
per-minute limit that persists through every retry, raises DailyLimitReached
so the runner can save progress and stop; the run resumes later from cache.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

from agentcrew.llm import QuotaExhaustedError

T = TypeVar("T")

DELAYS = (4.0, 8.0, 16.0, 32.0, 64.0, 64.0)
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
) -> tuple[T, float]:
    """Run `fn`, waiting out per-minute rate limits. Returns (value, seconds slept)."""
    slept = 0.0
    for attempt in range(len(delays) + 1):
        try:
            return fn(), slept
        except QuotaExhaustedError as exc:
            message = str(exc)
            if is_daily_limit(message):
                raise DailyLimitReached(message) from exc
            if attempt == len(delays):
                raise DailyLimitReached(
                    f"rate limit persisted through {attempt} retries: {message}"
                ) from exc
            wait = max(delays[attempt], (retry_hint(message) or 0.0) + 1.0)
            sleep(wait)
            slept += wait
    raise AssertionError("unreachable")  # pragma: no cover
