"""The second holdout and the safety set: offline checks, no model calls."""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from agentcrew.context.examples import EXAMPLES_PATH
from evalkit.gold import connect_read_only
from evalkit.questions import question_problems
from evalrun.runner import HOLDOUT_SETS
from evalrun.sets import ALL_SET_FILES, V2_GOLD_FILES, load_questions

ROOT = Path(__file__).resolve().parents[1]
SET_NAME = "holdout_agent_v2"
SAFETY = ROOT / "eval" / "safety_set.yaml"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def conn(db_path):
    return connect_read_only(db_path)


@pytest.fixture(scope="module")
def builder():
    return _script("build_holdout_v2_gold")


class TestQuestions:
    def test_seventeen_questions_in_the_fixed_mix(self, builder):
        questions = load_questions(SET_NAME)
        assert len(questions) == 17
        assert dict(Counter(q["type"] for q in questions)) == builder.COMPOSITION

    def test_each_question_is_structurally_valid(self):
        for q in load_questions(SET_NAME):
            assert question_problems(q) == [], q["id"]

    def test_is_treated_as_a_holdout_by_the_runner(self):
        assert SET_NAME in HOLDOUT_SETS


class TestGold:
    def test_stored_gold_is_what_the_builder_produces(self, conn, builder):
        stored = json.loads(V2_GOLD_FILES[SET_NAME].read_text("utf-8"))
        gold_builder = _script("build_gold")
        assert gold_builder.canonical(stored) == gold_builder.canonical(
            builder.build(conn)
        )

    def test_screening_finds_nothing(self, conn, builder):
        assert builder.screen(conn, builder.build(conn)) == []

    def test_a_wrong_second_query_is_caught(self, conn, builder):
        problems = builder._verified(conn, "x", [[1]], "SELECT 2")
        assert problems and "disagrees" in problems[0]

    def test_a_missing_second_query_is_caught(self, conn, builder):
        assert "no independent second SQL" in builder._verified(conn, "x", [[1]], None)[0]

    def test_numbers_within_rounding_count_as_the_same(self, builder):
        assert builder._same([["a", 1.0000000001]], [["a", 1.0]])
        assert not builder._same([["a", 1.01]], [["a", 1.0]])


class TestExamplePool:
    def test_the_new_holdout_does_not_change_the_agents_pool(self, conn):
        # Frozen agent context: with the new set included, the pool itself must be
        # identical, so no pool item resembles or gives away a new question.
        rebuilt = _script("build_example_pool").build(conn, list(ALL_SET_FILES))
        stored = yaml.safe_load(EXAMPLES_PATH.read_text("utf-8"))
        assert rebuilt["pool"] == stored["pool"]


@pytest.fixture(scope="module")
def safety():
    return yaml.safe_load(SAFETY.read_text("utf-8"))


class TestSafetySet:
    def test_has_both_kinds(self, safety):
        assert len(safety["must_refuse"]) >= 5
        assert len(safety["must_answer"]) >= 5

    def test_is_not_a_scored_set(self, safety):
        assert "safety_set" not in ALL_SET_FILES
        ids = {q["id"] for name in ALL_SET_FILES for q in load_questions(name)}
        items = safety["must_refuse"] + safety["must_answer"]
        assert not ids & {item["id"] for item in items}

    def test_gold_figures_run(self, conn, safety):
        for item in safety["must_answer"]:
            if item.get("gold_sql"):
                assert conn.execute(item["gold_sql"]).fetchall(), item["id"]
