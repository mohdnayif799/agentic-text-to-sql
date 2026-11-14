"""The process-wide daily cap on runs and model calls.

Why this exists
---------------
The public demo answers on the server's key. The per-session allowance
(agentcrew/service.py, five runs in the session's state) is a courtesy
limit: a page reload starts a new session and a new allowance, so on its own
it bounds nothing. This module is the ceiling on total spend. One counter
per UTC day each for runs, model calls, SQL runs and uploads, shared by
every session and every request in the process. The UI and the REST API are
separate services, so separate processes: each counts its own day.

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
* ``allows_run()`` before a run on the server key starts
  (service.answer). It is false when the day's runs OR the day's model calls
  are used up: a run that cannot make a single call is refused up front
  rather than failing after the question was typed.
* ``charge_call()`` on every model call (archs/app_runner.CountingClient), so
  a run that would cross the line stops at the call that crosses it.
* ``charge_run()`` once a run completed, the same moment the per-session
  allowance is charged.
* ``reserve_sql_run()`` for the SQL editor and the API's /sql
  (agentcrew/sql_editor.py). A counter of its own: these runs make no model
  call, so they must not use up the day's agent runs.
* ``reserve_upload()`` / ``release_upload()`` for an upload, from the UI
  (agentcrew/data_panel.py) and the API (agentcrew/api/routes.py).

Check and count in one step
---------------------------
API requests run on a thread pool and UI sessions on a thread each, so two
can reach a limit at the same moment. ``reserve_*`` checks and counts under
one lock, so they cannot both take the last one; a separate "allowed?" then
"charge" would let both through. Model calls were always counted this way
(``charge_call``). The run counter is the exception: it is checked before a
run and charged only when the run completes, so runs in flight can take it a
few past its limit. The model-call cap, which is exact, is the bound on
spend. ``allows_upload`` remains as a cheap early check before a file is
read; the reservation that follows is what decides.

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
    sql_runs: int = 0
    max_sql_runs: int = 0


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
        max_sql_runs: int = 0,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self.max_runs = max(0, int(max_runs))
        self.max_llm_calls = max(0, int(max_llm_calls))
        self.max_uploads = max(0, int(max_uploads))
        self._uploads = 0
        self.max_sql_runs = max(0, int(max_sql_runs))
        self._sql_runs = 0
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
            self._sql_runs = 0

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
        """Whether one more dataset may be uploaded today: a cheap early
        answer, before a file is read. It counts nothing; reserve_upload is
        what decides. An upload's description calls are counted by
        charge_call like any other."""
        with self._lock:
            self._roll()
            return self.max_uploads == 0 or self._uploads < self.max_uploads

    def reserve_upload(self) -> bool:
        """Check and count one upload in a single step, for callers that can
        run uploads at the same time (the API): two requests can never both
        take the day's last upload. False when the day's uploads are used up.
        Give it back with release_upload() if the upload then fails."""
        with self._lock:
            self._roll()
            if self.max_uploads and self._uploads >= self.max_uploads:
                return False
            self._uploads += 1
            return True

    def release_upload(self) -> None:
        with self._lock:
            self._roll()
            self._uploads = max(0, self._uploads - 1)

    def reserve_sql_run(self) -> bool:
        """Check and count one SQL run in a single step, so simultaneous
        requests cannot all pass the check on the day's last run. False when
        the day's SQL runs are used up (nothing is counted then)."""
        with self._lock:
            self._roll()
            if self.max_sql_runs and self._sql_runs >= self.max_sql_runs:
                return False
            self._sql_runs += 1
            return True

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
                sql_runs=self._sql_runs,
                max_sql_runs=self.max_sql_runs,
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
                max_sql_runs=getattr(settings, "daily_sql_run_cap", 0),
            )
        return _cap


def reset_daily_cap() -> None:
    """Test helper: forget the process-wide cap."""
    global _cap
    with _cap_lock:
        _cap = None
