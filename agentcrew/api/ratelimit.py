"""A request rate limit per API key.

A sliding window: a key may make `per_minute` requests in any 60 seconds.
Each key's recent request times are kept in a deque, so the limit is exact
(a fixed window lets a caller send twice the limit across a boundary) and
the wait reported in Retry-After is the real one.

In memory, for the same reason the daily cap is (agentcrew/caps.py): the
API runs as one instance and one process, so a process-wide limiter is a
global one, and there is no store to run or secure. The memory is bounded
by the number of configured keys times the limit, because only requests
that passed the key check reach it. Unauthenticated requests are refused
before this, without reading their body; they are not counted.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

WINDOW_SECONDS = 60.0


class RateLimiter:
    """`per_minute` of 0 means no limit."""

    def __init__(self, per_minute: int, *, window: float = WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = max(0, int(per_minute))
        self.window = float(window)
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(self, client: str) -> int:
        """Count one request for `client`. Returns 0 when it is allowed, or
        the whole seconds to wait when it is not (a refused request is not
        counted, so waiting that long is always enough)."""
        if self.per_minute == 0:
            return 0
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(client, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return max(1, math.ceil(self.window - (now - hits[0])))
            hits.append(now)
            return 0
