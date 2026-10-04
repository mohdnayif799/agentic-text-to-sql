"""Rule-based checks on the final answer text (eval protocol v2)."""

from __future__ import annotations

import pytest

from evalkit.answer_checks import (
    check_answer,
    extract_numbers,
    number_present,
    states_assumption,
    text_present,
)


class TestNumbers:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("Revenue was $342,941.73 in 2025.", 342941.73),
            ("about 1.22 million dollars", 1_220_000.0),
            ("roughly 1.2M", 1_200_000.0),
            ("we spent 532k on marketing", 532_000.0),
            ("a correlation of 0.14", 0.14),
            ("67 customers", 67.0),
        ],
    )
    def test_extracts_values_with_units(self, text, value):
        assert any(n.value == pytest.approx(value) for n in extract_numbers(text))

    def test_percent_is_flagged(self):
        (n,) = [x for x in extract_numbers("fell 43%") if x.value == 43]
        assert n.percent

    def test_number_present_with_tolerance(self):
        assert number_present("Revenue was $342,941.73.", 342941.7312)
        assert number_present("about $1.22 million", 1_220_726.85)
        assert not number_present("about $1.2 million", 1_220_726.85)

    def test_sign_is_ignored(self):
        assert number_present("revenue fell 43%", -42.96)

    def test_percent_matches_fraction_gold(self):
        assert number_present("a refund rate of 15.9%", 0.15904)

    def test_integer_needs_to_be_close(self):
        assert number_present("there are 24 tickets", 24)
        assert not number_present("there are 25 tickets", 24)


class TestText:
    def test_case_and_hyphen_insensitive(self):
        assert text_present("The mid market segment led.", "Mid-Market")

    def test_whole_words_only(self):
        assert not text_present("APACHE servers", "APAC")


class TestAssumption:
    @pytest.mark.parametrize(
        "text",
        [
            "Assuming 'best' means highest completed revenue, May 2025.",
            "Measured by units sold, Vertex Nimbus is the most popular.",
            "If you mean revenue per customer, it is Mid-Market.",
            "I interpreted 'most valuable' as total revenue.",
            "Based on completed revenue, SMB leads.",
        ],
    )
    def test_detects_stated_assumptions(self, text):
        assert states_assumption(text)

    def test_plain_answer_states_nothing(self):
        assert not states_assumption("The best month was May 2025.")


class TestCheckAnswer:
    def test_all_checks_must_pass(self):
        checks = [{"text": "APAC"}, {"text": "Lattice Ridge"}]
        assert check_answer("In APAC, Lattice Ridge fell most.", checks).passed
        verdict = check_answer("In APAC, Anchor Summit fell most.", checks)
        assert not verdict.passed
        assert verdict.failed == ("text: Lattice Ridge",)

    def test_any_of_and_pattern(self):
        checks = [{"any_of": [{"pattern": r"fewer\s+orders"},
                              {"pattern": r"order (volume|count)"}]}]
        assert check_answer("APAC simply had fewer orders.", checks).passed
        assert not check_answer("APAC had smaller baskets.", checks).passed

    def test_number_check_with_custom_tolerance(self):
        checks = [{"number": 0.1403, "abs": 0.001}]
        assert check_answer("r = 0.140", checks).passed
        assert not check_answer("r = 0.15", checks).passed

    def test_empty_answer_fails_nonempty_checks(self):
        assert not check_answer("", [{"text": "APAC"}]).passed

    def test_no_checks_passes(self):
        assert check_answer("anything", []).passed

    def test_unknown_check_is_an_error(self):
        with pytest.raises(ValueError):
            check_answer("x", [{"nope": 1}])
