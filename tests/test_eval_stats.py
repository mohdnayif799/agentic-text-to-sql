"""Wilson intervals and the exact McNemar test (eval protocol v2)."""

from __future__ import annotations

import pytest

from evalkit.stats import mcnemar_exact, paired_counts, wilson_interval


class TestWilson:
    def test_zero_successes(self):
        lo, hi = wilson_interval(0, 10)
        assert lo == pytest.approx(0.0)
        assert hi == pytest.approx(0.2775, abs=1e-4)

    def test_all_successes(self):
        lo, hi = wilson_interval(10, 10)
        assert lo == pytest.approx(0.7225, abs=1e-4)
        assert hi == pytest.approx(1.0)

    def test_half(self):
        lo, hi = wilson_interval(10, 20)
        assert lo == pytest.approx(0.2993, abs=1e-4)
        assert hi == pytest.approx(0.7007, abs=1e-4)

    def test_no_trials_is_uninformative(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_rejects_impossible_counts(self):
        with pytest.raises(ValueError):
            wilson_interval(5, 3)


class TestMcNemar:
    @pytest.mark.parametrize(
        ("b", "c", "p"),
        [(6, 0, 0.03125), (0, 6, 0.03125), (8, 1, 0.0390625), (3, 3, 1.0),
         (0, 0, 1.0), (5, 0, 0.0625)],
    )
    def test_two_sided_exact_p(self, b, c, p):
        assert mcnemar_exact(b, c) == pytest.approx(p)

    def test_paired_counts(self):
        a = {"q1": True, "q2": False, "q3": True, "q4": False}
        d = {"q1": True, "q2": True, "q3": False, "q4": True}
        assert paired_counts(a, d) == {"both": 1, "neither": 0,
                                       "only_a": 1, "only_b": 2}

    def test_paired_counts_requires_same_questions(self):
        with pytest.raises(ValueError):
            paired_counts({"q1": True}, {"q2": True})
