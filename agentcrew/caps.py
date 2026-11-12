"""The process-wide daily cap on runs and model calls.

Why this exists
---------------
The public demo answers on the server's key. The per-session allowance
(app.py, five runs in ``st.session_state``) is a courtesy limit: a page
reload starts a new session and a new allowance, so on its own it bounds
nothing. This module is the ceiling on total spend. One counter per UTC day
for runs and one for model calls, shared by every session, every upload and
(later) every API request in the process.

Why process-wide is enough
--------------------------
The live service runs at most one instance (Cloud Run ``maxScale: 1``), so a
process-wide counter is a global one. There is deliberately no store behind
it: the filesystem is tmpfs and vanishes on scale-to-zero, and a database
for one integer would be a new service to run and secure. The cost of that
choice is that a cold start resets the day's count. docs/OPERATIONS.md says
so, and the provider's own daily quota remains the hard stop behind this one.

Where it is enforced
--------------------
* ``allows_run()`` before a run on the server key starts (app.py). A run that
  cannot make a single call is refused up front rather than failing after the
  question was typed.
* ``charge_call()`` on every model call (archs/app_runner.CountingClient), so
  a run that would cross the line stops at the call that crosses it.
* ``charge_run()`` once a run completed, the same moment the per-session
  allowance is charged.

A visitor's own key is never counted: they pay for those calls.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any


class DailyCapExceeded(Exception):
    """A run or a model call was refused because today's cap is reached.

    Not an ``LLMError``: the provider was never asked, and the app shows it
    as a notice (like the per-session limit), not as a failed run.
    """


@dataclass(frozen=True)
class CapSnapshot:
    day: date
    runs: int
    max_runs: int
    llm_calls: int
    max_llm_calls: int
    uploads: int = 0
    max_uploads: int = 0


def _utc_today() -> date:
    return datetime.now(UTC).date()


class DailyCap:
    """Thread-safe counters that reset when the UTC date changes.

    ``max_runs`` or ``max_llm_calls`` of 0 means that counter is unlimited.
    """

    def __init__(
        self,
        *,
        max_runs: int,
        max_llm_calls: int,
        max_uploads: int = 0,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self.max_runs = max(0, int(max_runs))
        self.max_llm_calls = max(0, int(max_llm_calls))
        self.max_uploads = max(0, int(max_uploads))
        self._uploads = 0
        self._today = today
        self._lock = threading.Lock()
        self._day = today()
        self._runs = 0
        self._calls = 0

    # -- internal ----------------------------------------------------------
    def _roll(self) -> None:
        """Called with the lock held: start a new day when the date moved."""
        now = self._today()
        if now != self._day:
            self._day = now
            self._runs = 0
            self._calls = 0
            self._uploads = 0

    def _calls_left(self) -> bool:
        return self.max_llm_calls == 0 or self._calls < self.max_llm_calls

    # -- public ------------------------------------------------------------
    def allows_run(self) -> bool:
        """Whether one more run may start today."""
        with self._lock:
            self._roll()
            runs_left = self.max_runs == 0 or self._runs < self.max_runs
            return runs_left and self._calls_left()

    def charge_run(self) -> None:
        """Count one completed run."""
        with self._lock:
            self._roll()
            self._runs += 1

    def charge_call(self) -> None:
        """Count one model call, or refuse it when the day's calls are used up.
        The refused call is not counted."""
        with self._lock:
            self._roll()
            if not self._calls_left():
                raise DailyCapExceeded(
                    f"daily model-call cap of {self.max_llm_calls} reached"
                )
            self._calls += 1

    def allows_upload(self) -> bool:
        """Whether one more dataset may be uploaded today. An upload's
        description calls are counted by charge_call like any other."""
        with self._lock:
            self._roll()
            return self.max_uploads == 0 or self._uploads < self.max_uploads

    def charge_upload(self) -> None:
        with self._lock:
            self._roll()
            self._uploads += 1

    def snapshot(self) -> CapSnapshot:
        with self._lock:
            self._roll()
            return CapSnapshot(
                day=self._day,
                runs=self._runs,
                max_runs=self.max_runs,
                llm_calls=self._calls,
                max_llm_calls=self.max_llm_calls,
                uploads=self._uploads,
                max_uploads=self.max_uploads,
            )


_cap: DailyCap | None = None
_cap_lock = threading.Lock()


def daily_cap(settings: Any) -> DailyCap:
    """The process-wide cap, built from the first Settings it is given.

    Later calls return the same object whatever settings they pass: the cap
    is a property of the deployment, read from the environment once, not of
    the run that happens to ask.
    """
    global _cap
    with _cap_lock:
        if _cap is None:
            _cap = DailyCap(
                max_runs=settings.daily_run_cap,
                max_llm_calls=settings.daily_llm_call_cap,
                max_uploads=getattr(settings, "daily_upload_cap", 0),
            )
        return _cap


def reset_daily_cap() -> None:
    """Test helper: forget the process-wide cap."""
    global _cap
    with _cap_lock:
        _cap = None
