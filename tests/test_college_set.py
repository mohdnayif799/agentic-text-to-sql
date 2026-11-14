"""The unseen-schema test's data, questions and gold (offline, no model).

Keeps three promises in CI: the frozen files still match their manifest,
the gold answers still verify (clean SQL = messy SQL = pandas, no ties), and
the messy copy really is cryptic. The runs themselves need a model and are
reported in docs/DATA_SOURCES.md.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import clean_subprocess_env

from evalrun import college

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_college_gold")


class TestFrozen:
    def test_files_match_the_manifest(self) -> None:
        assert college.manifest_problems() == []

    def test_manifest_covers_questions_and_both_copies(self) -> None:
        names = [line.split(" *", 1)[1] for line in
                 college.FREEZE.read_text("utf-8").splitlines()]
        assert names[0] == "eval/college_questions.yaml"
        assert sum(n.startswith("eval/college/") for n in names) == 4
        assert sum(n.startswith("eval/college_messy/") for n in names) == 5

    def test_a_changed_file_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = college.sha256

        def edited(path: Path) -> str:
            return "0" * 64 if path == college.QUESTIONS else real(path)

        monkeypatch.setattr(college, "sha256", edited)
        assert college.manifest_problems() == [
            "eval/college_questions.yaml changed since it was frozen"]

    def test_the_build_script_reproduces_the_files(self, tmp_path: Path) -> None:
        """Seeded: running it again changes no byte of either copy."""
        before = college.manifest_text()
        subprocess.run([sys.executable, str(ROOT / "scripts" /
                                            "build_college_dataset.py")],
                       check=True, cwd=ROOT, env=clean_subprocess_env(),
                       capture_output=True)
        assert college.manifest_text() == before

    def test_runner_refuses_when_the_manifest_does_not_match(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        runner = _load("run_unseen_schema")
        monkeypatch.setattr(college, "manifest_problems",
                            lambda: ["eval/college_questions.yaml changed"])
        monkeypatch.setattr(sys, "argv", ["run_unseen_schema.py", "--run", "x"])
        assert runner.main() == 3
        assert "REFUSED" in capsys.readouterr().out


class TestGold:
    def test_copies_hold_the_same_rows(self) -> None:
        assert check.check_copies() == []

    def test_clean_messy_and_pandas_agree(self) -> None:
        problems, gold = check.check_gold()
        assert problems == []
        assert len(gold) == 15 and all(entry["rows"] for entry in gold.values())

    def test_no_answer_rests_on_a_tie(self) -> None:
        problems, notes = check.check_margins()
        assert problems == [] and notes

    def test_gold_sql_passes_the_guard(self) -> None:
        from agentcrew.db.safety import guard_sql

        for q in college.load_questions():
            for key in college.SQL_KEY.values():
                guard_sql(q["gold"][key], max_rows=500)


class TestQuestions:
    def test_ten_single_query_and_five_multi_step(self) -> None:
        ids = [q["id"] for q in college.load_questions()]
        assert len(ids) == len(set(ids)) == 15
        assert sum(i.startswith("co") for i in ids) == 10
        assert sum(i.startswith(college.HARD_PREFIX) for i in ids) == 5

    def test_multi_step_gold_really_has_more_than_one_step(self) -> None:
        for q in college.load_questions():
            if q["id"].startswith(college.HARD_PREFIX):
                for key in college.SQL_KEY.values():
                    sql = q["gold"][key].upper()
                    assert sql.count("SELECT") >= 2, q["id"]

    def test_questions_do_not_name_messy_columns_or_codes(self) -> None:
        """The same plain-English question is asked of both copies: it must
        not hand the model the cryptic names or the codes."""
        cryptic = {"stu_", "crs_", "mk_obt", "att_", "exm_", "dept_cd", " CSE", " ECE",
                   "'FN'", "'MT'"}
        for q in college.load_questions():
            assert not any(c in q["question"] for c in cryptic), q["id"]


class TestMessyCopy:
    def test_names_are_cryptic_and_values_are_codes(self) -> None:
        import pandas as pd

        frames = {p.stem: pd.read_csv(p) for p in college.csv_files("messy")}
        assert set(frames) == {"stu_mst", "crs_mst", "exm_mk", "att_log", "att_sum"}
        assert list(frames["exm_mk"].columns) == ["stu_id", "crs_cd", "exm_typ",
                                                  "mk_obt", "exm_dt"]
        assert set(frames["stu_mst"].dept_cd) == set(college.CODES)
        assert set(frames["exm_mk"].exm_typ) == {"MT", "FN"}
        assert set(frames["att_log"].att_st) == {"P", "A", "L"}
        assert "att_pct" in frames["att_sum"].columns
        english = {"student", "course", "score", "department", "attendance", "name"}
        for frame in frames.values():
            assert not english & set(frame.columns)
