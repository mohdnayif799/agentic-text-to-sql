"""Partial-evaluation and quota-exhaustion behaviour.

Every test here runs the real harness, the real graph and real SQL against the
real database. Only the provider is simulated, and it is simulated by raising
the *actual* error text Gemini returns on quota exhaustion. No API quota is
spent.

The property under test: a run that stops early must never be mistakable for a
complete one, and a question the model never saw must never be scored as a
failure.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "eval_runner_quota", ROOT / "scripts" / "run_eval.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_runner_quota"] = module
    spec.loader.exec_module(module)
    return module


ev = _load_runner()

from agentcrew.config import Settings  # noqa: E402
from agentcrew.llm import QuotaExhaustedError  # noqa: E402

GEMINI_QUOTA = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
    "'You exceeded your current quota.', 'status': 'RESOURCE_EXHAUSTED'}}"
)


def make_factory(quota_from: set[str], quota_arms: set[str] | None = None):
    """Client factory that raises the real quota error for chosen question ids.

    Triggering on question id rather than a call count keeps the tests readable
    and deterministic regardless of how many calls a question happens to make.
    """
    quota_arms = quota_arms or {"agent", "baseline"}

    def factory(q):
        client = ev.make_offline_client(q.get("reference_sql") or "SELECT 1")
        if q["id"] not in quota_from:
            return client

        original = client.complete

        def complete(**kwargs):
            system = kwargs.get("system", "")
            is_baseline = "SQL analyst" in system
            arm = "baseline" if is_baseline else "agent"
            if arm in quota_arms:
                raise QuotaExhaustedError(f"Gemini quota/rate limit: {GEMINI_QUOTA}")
            return original(**kwargs)

        client.complete = complete
        return client

    return factory


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(provider="fake", model="fake-model", database_path=db_path)


@pytest.fixture
def questions():
    return ev.load_questions(None)


def run(settings, questions, out, factory, arms=("agent", "baseline"), resume=False):
    return ev.run_evaluation(
        questions, settings, list(arms), out,
        resume=resume, client_factory=factory, verbose=False,
    )


# ---------------------------------------------------------------------------
# Baseline behaviour: nothing changes when there is no quota error
# ---------------------------------------------------------------------------
class TestNoQuotaError:
    def test_complete_run_is_marked_complete(self, settings, questions, tmp_path):
        p = run(settings, questions, tmp_path / "r.json", make_factory(set()))
        assert p["complete"] is True
        assert p["stopped_reason"] is None
        assert p["stopped_at"] is None

    def test_all_questions_evaluated(self, settings, questions, tmp_path):
        p = run(settings, questions, tmp_path / "r.json", make_factory(set()))
        assert p["coverage"]["evaluated"] == len(questions)
        assert p["coverage"]["not_evaluated_quota"] == 0
        assert all(i["state"] == ev.EVALUATED for i in p["items"])

    def test_existing_scoring_is_unchanged(self, settings, questions, tmp_path):
        """The harness self-test still scores 17/17 on both arms."""
        p = run(settings, questions, tmp_path / "r.json", make_factory(set()))
        for arm in ("agent", "baseline"):
            assert p["summaries"][arm]["task_success"] == "17/17 (100%)"
            assert p["summaries"][arm]["denominator"] == 17

    def test_database_untouched(self, settings, questions, tmp_path):
        p = run(settings, questions, tmp_path / "r.json", make_factory(set()))
        assert p["mutated_tables"] == []


# ---------------------------------------------------------------------------
# Quota exhaustion partway through
# ---------------------------------------------------------------------------
class TestQuotaExhaustion:
    @pytest.fixture
    def partial(self, settings, questions, tmp_path):
        """Quota dies on the 16th question and every one after it."""
        ids = [q["id"] for q in questions]
        self.stop_id = ids[15]
        dead = set(ids[15:])
        out = tmp_path / "r.json"
        return run(settings, questions, out, make_factory(dead)), out, ids

    def test_run_is_marked_incomplete(self, partial):
        p, _, ids = partial
        assert p["complete"] is False
        assert p["stopped_reason"] == "quota_exhausted"
        assert p["stopped_at"] == ids[15]

    def test_completed_questions_are_preserved(self, partial):
        p, _, ids = partial
        assert p["coverage"]["evaluated"] == 15
        evaluated = {i["id"] for i in p["items"] if i["state"] == ev.EVALUATED}
        assert evaluated == set(ids[:15])

    def test_quota_question_and_all_later_are_not_evaluated(self, partial):
        p, _, ids = partial
        not_eval = {i["id"] for i in p["items"]
                    if i["state"] == ev.NOT_EVALUATED_QUOTA}
        assert not_eval == set(ids[15:])
        assert p["coverage"]["not_evaluated_quota"] == 3

    def test_unevaluated_success_is_null_not_false(self, partial):
        """The core requirement: never score a question the model never saw."""
        p, _, _ = partial
        unevaluated = [i for i in p["items"] if i["state"] == ev.NOT_EVALUATED_QUOTA]
        assert unevaluated
        for item in unevaluated:
            assert item["success"] is None, f"{item['id']} was scored as {item['success']}"

    def test_denominator_counts_only_evaluated_questions(self, partial):
        p, _, _ = partial
        for arm in ("agent", "baseline"):
            s = p["summaries"][arm]
            assert s["questions_evaluated"] == 15
            # 15 evaluated, none of which is the unscorable ambiguity probe
            assert s["denominator"] == 15
            assert s["task_success"].endswith("(100%)")
            assert s["task_success"].startswith("15/15")

    def test_arms_stay_paired(self, partial):
        """No question may have one arm evaluated and the other not."""
        p, _, _ = partial
        by_id: dict[str, set[str]] = {}
        for i in p["items"]:
            by_id.setdefault(i["id"], set()).add(i["state"])
        for qid, states in by_id.items():
            assert len(states) == 1, f"{qid} has mismatched arm states: {states}"

    def test_both_arms_recorded_for_every_question(self, partial):
        p, _, ids = partial
        for qid in ids:
            arms = {i["arm"] for i in p["items"] if i["id"] == qid}
            assert arms == {"agent", "baseline"}, f"{qid} missing an arm"

    def test_results_file_written_despite_early_stop(self, partial):
        p, out, _ = partial
        assert out.exists()
        assert json.loads(out.read_text())["complete"] is False

    def test_not_evaluated_ids_are_reported(self, partial):
        p, _, ids = partial
        assert p["coverage"]["not_evaluated_ids"] == sorted(ids[15:])


class TestQuotaInBaselineArmOnly:
    """The baseline swallows exceptions, so quota must be caught from its text."""

    def test_baseline_quota_stops_the_run(self, settings, questions, tmp_path):
        ids = [q["id"] for q in questions]
        factory = make_factory({ids[2]}, quota_arms={"baseline"})
        p = run(settings, questions, tmp_path / "r.json", factory)
        assert p["complete"] is False
        assert p["stopped_at"] == ids[2]

    def test_partial_agent_result_is_discarded_not_counted(
        self, settings, questions, tmp_path
    ):
        """The agent arm finished for that question; the pair did not."""
        ids = [q["id"] for q in questions]
        factory = make_factory({ids[2]}, quota_arms={"baseline"})
        p = run(settings, questions, tmp_path / "r.json", factory)
        states = {i["state"] for i in p["items"] if i["id"] == ids[2]}
        assert states == {ev.NOT_EVALUATED_QUOTA}
        assert p["summaries"]["agent"]["questions_evaluated"] == 2


# ---------------------------------------------------------------------------
# Incremental saving
# ---------------------------------------------------------------------------
class TestIncrementalSave:
    def test_partial_file_exists_before_the_run_ends(
        self, settings, questions, tmp_path
    ):
        """A killed process must not destroy completed work."""
        out = tmp_path / "r.json"
        ids = [q["id"] for q in questions]
        run(settings, questions[:4], out, make_factory({ids[3]}))
        data = json.loads(out.read_text())
        assert data["coverage"]["evaluated"] == 3


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------
class TestResume:
    def test_resume_completes_the_remaining_questions(
        self, settings, questions, tmp_path
    ):
        out = tmp_path / "r.json"
        ids = [q["id"] for q in questions]

        first = run(settings, questions, out, make_factory(set(ids[15:])))
        assert first["coverage"]["evaluated"] == 15

        second = run(settings, questions, out, make_factory(set()), resume=True)
        assert second["complete"] is True
        assert second["coverage"]["evaluated"] == len(questions)
        assert second["coverage"]["not_evaluated_quota"] == 0

    def test_resume_does_not_rerun_completed_questions(
        self, settings, questions, tmp_path
    ):
        out = tmp_path / "r.json"
        ids = [q["id"] for q in questions]
        run(settings, questions, out, make_factory(set(ids[15:])))

        asked: list[str] = []

        def counting_factory(q):
            asked.append(q["id"])
            return ev.make_offline_client(q.get("reference_sql") or "SELECT 1")

        run(settings, questions, out, counting_factory, resume=True)
        assert set(asked) == set(ids[15:]), "resume re-ran already-evaluated questions"

    def test_resume_without_a_prior_file_starts_fresh(
        self, settings, questions, tmp_path
    ):
        p = run(settings, questions[:3], tmp_path / "none.json",
                make_factory(set()), resume=True)
        assert p["coverage"]["evaluated"] == 3

    def test_resume_ignores_a_corrupt_prior_file(self, settings, questions, tmp_path):
        out = tmp_path / "r.json"
        out.write_text("{not json")
        p = run(settings, questions[:3], out, make_factory(set()), resume=True)
        assert p["coverage"]["evaluated"] == 3

    def test_resumed_summary_merges_both_batches(self, settings, questions, tmp_path):
        out = tmp_path / "r.json"
        ids = [q["id"] for q in questions]
        run(settings, questions, out, make_factory(set(ids[15:])))
        final = run(settings, questions, out, make_factory(set()), resume=True)
        for arm in ("agent", "baseline"):
            assert final["summaries"][arm]["task_success"] == "17/17 (100%)"
