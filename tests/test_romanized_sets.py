"""The romanized Hindi and Telugu sets (Phase 5): the same ten NorthStar
questions typed in English letters, frozen before any run.

Offline. The gold is rebuilt from the YAML and compared with the stored
file, so a change to a question, its SQL or the database shows up here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from evalkit.gold import connect_read_only
from evalrun.sets import (
    ALL_SET_FILES,
    ROMANIZED_GOLD_FILES,
    ROMANIZED_SET_FILES,
    load_questions,
)

ROOT = Path(__file__).resolve().parents[1]
FROZEN_50 = ("holdout_easy", "holdout_agent", "holdout_agent_v2")


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _script("build_romanized_gold")


@pytest.fixture(scope="module")
def conn(db_path):
    return connect_read_only(db_path)


class TestSets:
    def test_ten_questions_each_in_plain_ascii(self):
        for name in ROMANIZED_SET_FILES:
            questions = load_questions(name)
            assert len(questions) == 10
            assert all(q["question"].isascii() for q in questions), name
            assert all(q["type"] == "easy" for q in questions)

    def test_the_two_languages_are_the_same_questions(self, builder):
        assert builder.pair_problems() == []
        hindi, telugu = (load_questions(name) for name in ROMANIZED_SET_FILES)
        for h, t in zip(hindi, telugu, strict=True):
            assert h["question"] != t["question"]
            assert h["english"] == t["english"]
            assert h["gold"]["sql"] == t["gold"]["sql"]

    def test_ids_are_unique_across_every_set(self):
        ids = [q["id"] for name in ALL_SET_FILES for q in load_questions(name)]
        assert len(ids) == len(set(ids))

    def test_none_is_a_frozen_50_question(self):
        frozen = {" ".join(q["question"].lower().split())
                  for name in FROZEN_50 for q in load_questions(name)}
        assert len(frozen) == 50
        for name in ROMANIZED_SET_FILES:
            for q in load_questions(name):
                assert " ".join(q["english"].lower().split()) not in frozen


class TestGold:
    @pytest.mark.parametrize("name", list(ROMANIZED_SET_FILES))
    def test_gold_is_reproducible_verified_and_screened(self, name, builder, conn):
        frozen_builder = _script("build_gold")
        payload = builder.build(conn, name)  # raises if a second SQL disagrees
        stored = json.loads(ROMANIZED_GOLD_FILES[name].read_text("utf-8"))
        assert frozen_builder.canonical(stored) == frozen_builder.canonical(payload)
        assert builder.screen(conn, name, payload) == []

    def test_both_languages_have_the_same_gold_rows(self):
        hindi, telugu = (json.loads(path.read_text("utf-8"))["gold"]
                         for path in ROMANIZED_GOLD_FILES.values())
        assert [e["rows"] for e in hindi.values()] == [
            e["rows"] for e in telugu.values()]

    def test_a_wrong_second_query_would_be_caught(self, conn):
        v2 = _script("build_holdout_v2_gold")
        assert v2._verified(conn, "x", [[798]], "SELECT 799")

    def test_the_check_command_passes(self, builder, monkeypatch):
        monkeypatch.setattr("sys.argv", ["build_romanized_gold.py", "--check"])
        assert builder.main() == 0


class TestFreeze:
    def test_frozen_files_match(self):
        lines = (ROOT / "eval" / "FREEZE_v4.sha256").read_text("utf-8").splitlines()
        assert len(lines) == 5
        for line in lines:
            digest, rel = line.split(" *", 1)
            actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
            assert actual == digest, rel

    def test_the_agents_example_pool_is_built_without_these_sets(self):
        pool = _script("build_example_pool")
        assert not set(ROMANIZED_SET_FILES) & set(pool.POOL_SETS)
