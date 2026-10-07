"""The v2 question sets and their gold files stay valid and reproducible."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from evalkit.gold import file_sha256, load_gold
from evalkit.questions import (
    GOLD_FILES,
    RESULT_TYPES,
    SET_FILES,
    load_set,
    question_problems,
    set_problems,
)
from evalrun.sets import ALL_SET_FILES, DEV_GOLD_FILES, DEV_SET_FILES, load_questions

ROOT = Path(__file__).resolve().parents[1]
SETS = list(SET_FILES)


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_gold", ROOT / "scripts" / "build_gold.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", SETS)
def test_set_is_well_formed(name):
    assert set_problems(name, load_set(name)) == []


def test_ids_are_unique_across_sets():
    ids = [q["id"] for name in SETS for q in load_set(name)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", SETS)
def test_gold_file_matches_question_file(name):
    gold = load_gold(GOLD_FILES[name])
    assert gold["questions_sha256"] == file_sha256(SET_FILES[name])
    questions = load_set(name)
    assert set(gold["gold"]) == {q["id"] for q in questions}
    for q in questions:
        entry = gold["gold"][q["id"]]
        if q["type"] in RESULT_TYPES:
            assert entry["rows"], q["id"]


@pytest.mark.parametrize("name", SETS)
def test_gold_is_reproducible_from_the_database(name, db_path):
    builder = _load_builder()
    from evalkit.gold import connect_read_only

    rebuilt = builder.build_set(connect_read_only(db_path), name)
    stored = load_gold(GOLD_FILES[name])
    assert builder.canonical(rebuilt) == builder.canonical(stored)


class TestBuildGuards:
    @pytest.fixture
    def conn(self):
        import sqlite3

        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE products (product_id INTEGER, product_name TEXT)")
        db.executemany("INSERT INTO products VALUES (?, ?)", [(1, "A"), (2, "B")])
        return db

    def test_name_without_id_is_refused(self, conn):
        builder = _load_builder()
        q = {"id": "x", "type": "easy",
             "gold": {"sql": "SELECT product_name FROM products LIMIT 1"}}
        with pytest.raises(RuntimeError, match="without product_id"):
            builder.build_entry(conn, q)

    def test_top_n_must_match_gold_rows(self, conn):
        builder = _load_builder()
        q = {"id": "y", "type": "easy", "top_n": 1,
             "gold": {"sql": "SELECT product_id, product_name FROM products"}}
        with pytest.raises(RuntimeError, match="top_n=1"):
            builder.build_entry(conn, q)

    def test_top_n_is_recorded_in_the_gold(self, conn):
        builder = _load_builder()
        q = {"id": "z", "type": "easy", "top_n": 1,
             "gold": {"sql": "SELECT product_id, product_name FROM products LIMIT 1"}}
        assert builder.build_entry(conn, q)["top_n"] == 1


def test_validator_catches_problems():
    bad = {"id": "x", "type": "why", "question": "Why?", "driver_checks": [],
           "judge_rubric": "", "evidence": {}}
    assert len(question_problems(bad)) == 3
    assert question_problems({"id": "y", "type": "nope", "question": "?"})
    malformed = {"id": "z", "type": "easy", "question": "?",
                 "gold": {"sql": "SELECT 1"}, "answer_checks": [{"pattern": "("}]}
    assert any("bad regex" in p for p in question_problems(malformed))


def test_rubric_must_be_structured():
    base = {"id": "w", "type": "why", "question": "Why?",
            "driver_checks": [{"text": "x"}], "evidence": {"t": "SELECT 1"}}
    good = {"required_facts": ["A fact."], "disqualifying": ["A claim."]}
    assert question_problems({**base, "judge_rubric": good}) == []
    assert question_problems({**base, "judge_rubric": {**good, "allowed": ["ok"]}}) == []
    assert question_problems({**base, "judge_rubric": "free text"})
    assert question_problems({**base, "judge_rubric": {**good, "required_facts": []}})
    assert question_problems({**base, "judge_rubric": {**good, "extra": []}})


def test_every_why_rubric_lists_facts_and_disqualifiers():
    for name in SETS:
        for q in load_set(name):
            if q["type"] == "why":
                assert len(q["judge_rubric"]["required_facts"]) >= 2, q["id"]
                assert q["judge_rubric"]["disqualifying"], q["id"]


class TestDevelopmentSets:
    """Development sets (evalrun/sets.py): not frozen, but built, verified
    and screened against the protocol's sets like the showcase."""

    @staticmethod
    def _dev_builder():
        spec = importlib.util.spec_from_file_location(
            "build_dev_gold", ROOT / "scripts" / "build_dev_gold.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.parametrize("name", list(DEV_SET_FILES))
    def test_questions_are_well_formed_and_ids_unique(self, name):
        questions = load_questions(name)
        assert [p for q in questions for p in question_problems(q)] == []
        ids = [q["id"] for n in ALL_SET_FILES for q in load_questions(n)]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("name", list(DEV_SET_FILES))
    def test_gold_is_reproducible_verified_and_screened(self, name, db_path):
        from evalkit.gold import connect_read_only

        builder, frozen = self._dev_builder(), _load_builder()
        conn = connect_read_only(db_path)
        rebuilt = builder.build(conn, name)  # raises if SQL and pandas disagree
        stored = load_gold(DEV_GOLD_FILES[name])
        assert stored["questions_sha256"] == file_sha256(DEV_SET_FILES[name])
        assert frozen.canonical(rebuilt) == frozen.canonical(stored)
        assert builder.screen(conn, name, rebuilt) == []

    def test_dev_sets_are_not_protocol_sets(self):
        assert set(DEV_SET_FILES).isdisjoint(SET_FILES)
