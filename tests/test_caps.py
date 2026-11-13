"""The process-wide daily cap on runs and model calls (agentcrew/caps.py).

The public demo runs on the server's key. The per-session allowance (5 runs
in st.session_state) is reset by a page reload, so on its own it bounds
nothing. The daily cap is the ceiling on total spend: one counter per UTC
day for runs and one for model calls, shared by every session in the
process. The live service runs at most one instance, so process-wide is
global. It resets on a cold start (there is no store), which is documented.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest

from agentcrew import caps
from agentcrew.caps import DailyCap, DailyCapExceeded
from agentcrew.config import Settings


class Clock:
    def __init__(self, today: date) -> None:
        self.today = today

    def __call__(self) -> date:
        return self.today


@pytest.fixture
def clock() -> Clock:
    return Clock(date(2026, 11, 12))


class TestRuns:
    def test_allows_runs_up_to_the_cap(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=2, max_llm_calls=0, today=clock)
        assert cap.allows_run()
        cap.charge_run()
        assert cap.allows_run()
        cap.charge_run()
        assert not cap.allows_run()

    def test_zero_means_unlimited(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=0, today=clock)
        for _ in range(1000):
            cap.charge_run()
            cap.charge_call()
        assert cap.allows_run()

    def test_resets_on_the_next_utc_day(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=1, max_llm_calls=0, today=clock)
        cap.charge_run()
        assert not cap.allows_run()
        clock.today = date(2026, 11, 13)
        assert cap.allows_run()
        assert cap.snapshot().runs == 0

    def test_snapshot_reports_used_and_limits(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=5, max_llm_calls=50, today=clock)
        cap.charge_run()
        cap.charge_call()
        cap.charge_call()
        snap = cap.snapshot()
        assert (snap.runs, snap.max_runs) == (1, 5)
        assert (snap.llm_calls, snap.max_llm_calls) == (2, 50)
        assert snap.day == date(2026, 11, 12)


class TestCalls:
    def test_charge_call_raises_once_the_cap_is_reached(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=2, today=clock)
        cap.charge_call()
        cap.charge_call()
        with pytest.raises(DailyCapExceeded):
            cap.charge_call()
        # The refused call is not counted.
        assert cap.snapshot().llm_calls == 2

    def test_run_is_refused_when_no_calls_are_left(self, clock: Clock) -> None:
        """A run needs at least one call; starting one that cannot make any
        would only fail after the question was typed."""
        cap = DailyCap(max_runs=0, max_llm_calls=1, today=clock)
        cap.charge_call()
        assert not cap.allows_run()

    def test_counters_are_thread_safe(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=0, today=clock)

        def work() -> None:
            for _ in range(500):
                cap.charge_call()
                cap.charge_run()

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = cap.snapshot()
        assert (snap.runs, snap.llm_calls) == (4000, 4000)


class TestUploads:
    def test_upload_cap(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=0, max_uploads=1, today=clock)
        assert cap.allows_upload()
        cap.charge_upload()
        assert not cap.allows_upload()
        clock.today = date(2026, 11, 13)
        assert cap.allows_upload() and cap.snapshot().uploads == 0

    def test_uploads_unlimited_by_default(self, clock: Clock) -> None:
        cap = DailyCap(max_runs=0, max_llm_calls=0, today=clock)
        for _ in range(100):
            cap.charge_upload()
        assert cap.allows_upload()


class TestProcessWideInstance:
    def test_built_from_settings_once(self) -> None:
        caps.reset_daily_cap()
        cap = caps.daily_cap(Settings(daily_run_cap=7, daily_llm_call_cap=70))
        assert cap is caps.daily_cap(Settings(daily_run_cap=1, daily_llm_call_cap=1))
        snap = cap.snapshot()
        assert (snap.max_runs, snap.max_llm_calls) == (7, 70)
        caps.reset_daily_cap()

    def test_defaults_are_finite(self) -> None:
        """A deployment that sets nothing still has a ceiling."""
        s = Settings()
        assert s.daily_run_cap > 0
        assert s.daily_llm_call_cap > 0
        assert s.daily_llm_call_cap >= s.daily_run_cap

    def test_exceeded_is_not_a_provider_error(self) -> None:
        """The app classifies LLMError subclasses as provider failures; the cap
        refusal is a notice of its own, so it must not be one."""
        from agentcrew.llm import LLMError

        assert not issubclass(DailyCapExceeded, LLMError)


def test_reserve_upload_checks_and_counts_in_one_step() -> None:
    from agentcrew.caps import DailyCap

    cap = DailyCap(max_runs=0, max_llm_calls=0, max_uploads=2)
    assert [cap.reserve_upload() for _ in range(3)] == [True, True, False]
    cap.release_upload()
    assert cap.snapshot().uploads == 1 and cap.reserve_upload()
    unlimited = DailyCap(max_runs=0, max_llm_calls=0)
    assert all(unlimited.reserve_upload() for _ in range(5))
    for _ in range(9):
        unlimited.release_upload()
    assert unlimited.snapshot().uploads == 0
