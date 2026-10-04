"""Strict result-set grader (eval protocol v2).

These pin down the rules in eval/PROTOCOL.md: multiset row match, every gold
column present, tolerance on floats only, exact integers, and exactly N rows
for a top-N question. No "number appears anywhere" or "first cell only".
"""

from __future__ import annotations

import pytest

from evalkit.grader import Gold, Tolerance, grade, normalize


def gold(*rows, ordered=False, tolerance=None):
    return Gold(rows=tuple(tuple(r) for r in rows), ordered=ordered,
                tolerance=tolerance or Tolerance())


class TestNormalize:
    def test_numeric_text_becomes_a_number(self):
        assert normalize("2096.64") == pytest.approx(2096.64)
        assert normalize(" 67 ") == 67

    def test_text_is_case_and_whitespace_insensitive(self):
        assert normalize("  North   America ") == "north america"

    def test_none_and_bool(self):
        assert normalize(None) is None
        assert normalize(True) == 1


class TestScalars:
    def test_exact_integer_passes(self):
        assert grade(gold([1400]), [[1400]]).passed

    def test_integer_gold_is_exact_even_when_large(self):
        # Relative tolerance would let 1401 pass for 1400; counts must be exact.
        assert not grade(gold([1400]), [[1401]]).passed

    def test_float_equal_to_integer_gold_passes(self):
        assert grade(gold([913]), [[913.0]]).passed

    def test_money_rounded_to_cents_passes(self):
        assert grade(gold([342941.7312]), [[342941.73]]).passed

    def test_float_outside_tolerance_fails(self):
        assert not grade(gold([2127780.36]), [[2240199.32]]).passed

    def test_small_float_rounded_to_two_places_passes(self):
        assert grade(gold([0.1403]), [[0.14]]).passed

    def test_tolerance_override(self):
        tight = Tolerance(rel=0.0, abs=0.0)
        assert not grade(gold([0.1403], tolerance=tight), [[0.14]]).passed

    def test_percent_versus_fraction_is_a_mismatch(self):
        assert not grade(gold([15.904]), [[0.15904]]).passed


class TestColumns:
    def test_extra_columns_allowed_in_any_position(self):
        assert grade(gold(["APAC"]), [[44, "APAC", 0.53]]).passed

    def test_gold_columns_may_be_reordered(self):
        g = gold(["Lattice Ridge", 1586.06])
        assert grade(g, [[1586.06, "lattice ridge"]]).passed

    def test_missing_gold_column_fails(self):
        g = gold(["LATAM", 1736364.86, 583742.2])
        verdict = grade(g, [["LATAM", 1736364.86]])
        assert not verdict.passed
        assert "column" in verdict.reason

    def test_one_agent_column_cannot_cover_two_gold_columns(self):
        assert not grade(gold([5, 5]), [[5]]).passed

    def test_mapping_is_reported(self):
        verdict = grade(gold(["x", 1]), [[1, "y", "x"]])
        assert verdict.passed
        assert verdict.mapping == (2, 0)


class TestRows:
    def test_top_n_requires_exactly_n_rows(self):
        verdict = grade(gold(["Mid-Market"]),
                        [["Mid-Market", 0.18], ["SMB", 0.16], ["Enterprise", 0.15]])
        assert not verdict.passed
        assert "expected 1 row" in verdict.reason

    def test_missing_rows_fail(self):
        assert not grade(gold(["a"], ["b"]), [["a"]]).passed

    def test_row_order_ignored_by_default(self):
        g = gold(["2025-07"], ["2025-08"], ["2025-09"])
        assert grade(g, [["2025-09"], ["2025-07"], ["2025-08"]]).passed

    def test_row_order_enforced_when_ordered(self):
        g = gold(["a"], ["b"], ordered=True)
        assert grade(g, [["a"], ["b"]]).passed
        assert not grade(g, [["b"], ["a"]]).passed

    def test_rows_are_a_multiset_not_a_set(self):
        assert not grade(gold(["a"], ["a"], ["b"]), [["a"], ["b"], ["b"]]).passed

    def test_columns_must_line_up_row_by_row(self):
        g = gold(["Online", 10.0], ["Direct", 20.0])
        assert not grade(g, [["Online", 20.0], ["Direct", 10.0]]).passed

    def test_none_matches_only_none(self):
        assert grade(gold([None]), [[None]]).passed
        assert not grade(gold([None]), [[0]]).passed

    def test_empty_gold_needs_empty_result(self):
        assert grade(gold(), []).passed
        assert not grade(gold(), [[1]]).passed

    def test_empty_result_fails_nonempty_gold(self):
        verdict = grade(gold([1]), [])
        assert not verdict.passed

    def test_wide_results_are_handled(self):
        row = list(range(30))
        assert grade(gold([3, 7]), [row]).passed
