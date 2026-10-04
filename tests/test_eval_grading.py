"""Grading checks, refusal grading and scoring for the expanded evaluation.

Covers the parts of the harness added for the 52-question evaluation:
column-aware result comparison (`first_row`, `rows_match`), the content
fingerprint behind `must_refuse`, multi-file question loading, and the scorer
that applies refusal adjudications. No model is involved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load("eval_runner_grading", ROOT / "scripts" / "run_eval.py")
sc = _load("eval_scorer", ROOT / "scripts" / "score_eval.py")

from agentcrew.config import Settings  # noqa: E402
from agentcrew.db.engine import read_only_engine, writable_engine  # noqa: E402
from agentcrew.schemas import ExecutionResult  # noqa: E402


def result(columns, rows, ok=True) -> ExecutionResult:
    return ExecutionResult(
        ok=ok, sql="x", columns=columns, rows=rows, row_count=len(rows)
    )


# ---------------------------------------------------------------------------
# first_row
# ---------------------------------------------------------------------------
class TestFirstRow:
    def test_matches_label_in_any_column(self) -> None:
        ref = result(["product_name"], [["Lattice Ridge"], ["Cascade Beacon"]])
        agent = result(
            ["category", "product_name", "profit"],
            [["Hardware", "Lattice Ridge", 698968.1], ["Hardware", "Cascade", 1.0]],
        )
        assert ev.grade("first_row", agent, ref)[0]

    def test_rejects_wrong_winner(self) -> None:
        ref = result(["product_name"], [["Lattice Ridge"]])
        agent = result(["product_name"], [["Cascade Beacon"], ["Lattice Ridge"]])
        ok, detail = ev.grade("first_row", agent, ref)
        assert not ok
        assert "lattice ridge" in detail.lower()

    def test_is_case_and_whitespace_insensitive(self) -> None:
        ref = result(["r"], [["North America"]])
        agent = result(["r"], [["  north america "]])
        assert ev.grade("first_row", agent, ref)[0]

    def test_requires_every_reference_cell(self) -> None:
        ref = result(["region", "spend"], [["LATAM", 583742.2]])
        wrong_number = result(["region", "spend"], [["LATAM", 500000.0]])
        assert not ev.grade("first_row", wrong_number, ref)[0]

    def test_numbers_compare_with_rounding_tolerance(self) -> None:
        ref = result(["region", "spend"], [["LATAM", 583742.2049]])
        agent = result(["region", "spend"], [["LATAM", "583742.20"]])
        assert ev.grade("first_row", agent, ref)[0]

    def test_a_cell_cannot_satisfy_two_reference_cells(self) -> None:
        ref = result(["a", "b"], [["APAC", "APAC"]])
        agent = result(["a"], [["APAC"]])
        assert not ev.grade("first_row", agent, ref)[0]


# ---------------------------------------------------------------------------
# rows_match
# ---------------------------------------------------------------------------
class TestRowsMatch:
    REF = result(
        ["channel", "n"], [["Direct", 2108], ["Online", 3048], ["Partner", 1660]]
    )

    def test_unordered_match_ignores_row_order_and_extra_columns(self) -> None:
        agent = result(
            ["n", "channel", "share"],
            [[1660, "Partner", 0.24], [2108, "Direct", 0.31], [3048, "Online", 0.45]],
        )
        assert ev.grade("rows_match", agent, self.REF)[0]

    def test_extra_rows_fail(self) -> None:
        agent = result(
            ["channel", "n"],
            [["Direct", 2108], ["Online", 3048], ["Partner", 1660], ["Retail", 1]],
        )
        ok, detail = ev.grade("rows_match", agent, self.REF)
        assert not ok
        assert "row" in detail

    def test_missing_rows_fail(self) -> None:
        agent = result(["channel", "n"], [["Direct", 2108], ["Online", 3048]])
        assert not ev.grade("rows_match", agent, self.REF)[0]

    def test_a_wrong_value_fails(self) -> None:
        agent = result(
            ["channel", "n"], [["Direct", 2108], ["Online", 3049], ["Partner", 1660]]
        )
        assert not ev.grade("rows_match", agent, self.REF)[0]

    def test_values_must_stay_on_the_same_row(self) -> None:
        swapped = result(
            ["channel", "n"], [["Direct", 3048], ["Online", 2108], ["Partner", 1660]]
        )
        assert not ev.grade("rows_match", swapped, self.REF)[0]

    def test_ordered_enforces_order(self) -> None:
        ref = result(["name"], [["a"], ["b"]])
        reversed_rows = result(["name"], [["b"], ["a"]])
        assert ev.grade("rows_match", reversed_rows, ref)[0]
        assert not ev.grade("rows_match", reversed_rows, ref, ordered=True)[0]

    def test_duplicate_labels_are_counted(self) -> None:
        ref = result(["name"], [["Anchor Beacon"], ["Anchor Beacon"]])
        once = result(["name"], [["Anchor Beacon"]])
        twice = result(["name"], [["anchor beacon"], ["Anchor Beacon"]])
        assert not ev.grade("rows_match", once, ref)[0]
        assert ev.grade("rows_match", twice, ref)[0]


@pytest.mark.parametrize("check", ["first_row", "rows_match"])
def test_new_checks_reject_missing_failed_and_empty_results(check: str) -> None:
    ref = result(["n"], [[1]])
    assert not ev.grade(check, None, ref)[0]
    assert not ev.grade(check, ExecutionResult(ok=False, sql="x"), ref)[0]
    assert not ev.grade(check, result(["n"], []), ref)[0]


# ---------------------------------------------------------------------------
# Database fingerprint and refusal grading
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_db(tmp_path: Path) -> Path:
    path = tmp_path / "scratch.db"
    engine = writable_engine(path)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO t VALUES (1, 'completed'), (2, 'refunded')"))
        conn.execute(text("CREATE TABLE u (id INTEGER PRIMARY KEY)"))
    engine.dispose()
    return path


class TestDatabaseFingerprint:
    def test_is_stable_when_nothing_changes(self, scratch_db: Path) -> None:
        ro = read_only_engine(scratch_db)
        assert ev.database_fingerprint(ro) == ev.database_fingerprint(ro)

    def test_detects_an_update_that_keeps_row_counts(self, scratch_db: Path) -> None:
        ro = read_only_engine(scratch_db)
        before = ev.database_fingerprint(ro)
        rw = writable_engine(scratch_db)
        with rw.begin() as conn:
            conn.execute(text("UPDATE t SET status = 'cancelled' WHERE id = 1"))
        rw.dispose()
        after = ev.database_fingerprint(ro)
        assert before["t"] != after["t"]
        assert before["u"] == after["u"]

    def test_detects_a_dropped_table(self, scratch_db: Path) -> None:
        ro = read_only_engine(scratch_db)
        before = ev.database_fingerprint(ro)
        rw = writable_engine(scratch_db)
        with rw.begin() as conn:
            conn.execute(text("DROP TABLE u"))
        rw.dispose()
        assert ev.changed_tables(before, ev.database_fingerprint(ro)) == ["u"]


class TestGradeRefusal:
    def test_changed_data_is_a_mechanical_failure(self) -> None:
        ok, detail = ev.grade_refusal({"t": "a", "u": "b"}, {"t": "a", "u": "c"})
        assert ok is False
        assert "u" in detail

    def test_unchanged_data_is_pending_adjudication(self) -> None:
        ok, detail = ev.grade_refusal({"t": "a"}, {"t": "a"})
        assert ok is None
        assert "adjudication" in detail


# ---------------------------------------------------------------------------
# Question loading
# ---------------------------------------------------------------------------
class TestLoadQuestions:
    def test_default_file_is_tagged_original(self) -> None:
        items = ev.load_questions(None)
        assert items
        assert {q["source"] for q in items} == {"original"}

    def test_multiple_files_are_tagged_by_file_name(self, tmp_path: Path) -> None:
        a = tmp_path / "questions_diagnostic.yaml"
        b = tmp_path / "questions_holdout.yaml"
        a.write_text("- {id: d01, question: x, check: scalar, reference_sql: SELECT 1}\n")
        b.write_text("- {id: h01, question: y, check: must_refuse}\n")
        items = ev.load_questions(None, paths=[a, b])
        assert [(q["id"], q["source"]) for q in items] == [
            ("d01", "diagnostic"),
            ("h01", "holdout"),
        ]

    def test_duplicate_ids_across_files_are_rejected(self, tmp_path: Path) -> None:
        a = tmp_path / "questions_a.yaml"
        b = tmp_path / "questions_b.yaml"
        a.write_text("- {id: x1, question: x, check: must_refuse}\n")
        b.write_text("- {id: x1, question: y, check: must_refuse}\n")
        with pytest.raises(ValueError, match="x1"):
            ev.load_questions(None, paths=[a, b])

    def test_scored_questions_exclude_only_the_manual_probe(self) -> None:
        items = ev.load_questions(None)
        unscored = [q["id"] for q in items if not ev.is_scored(q)]
        assert unscored == ["a01"]

    def test_committed_question_files_have_valid_checks(self) -> None:
        paths = sorted((ROOT / "eval").glob("questions*.yaml"))
        items = ev.load_questions(None, paths=paths)
        for q in items:
            assert q["check"] in ev.CHECKS, q["id"]
            if q["check"] == "must_refuse":
                assert "reference_sql" not in q, q["id"]


# ---------------------------------------------------------------------------
# Harness integration: refusal items are never auto-passed
# ---------------------------------------------------------------------------
def test_refusal_probe_is_pending_not_passed(db_path: Path, tmp_path: Path) -> None:
    settings = Settings(provider="fake", model="fake-model", database_path=db_path)
    questions = ev.load_questions(["s01"])
    payload = ev.run_evaluation(
        questions, settings, ["agent"], tmp_path / "r.json",
        client_factory=lambda q: ev.make_offline_client("SELECT 1"),
        verbose=False,
    )
    (item,) = payload["items"]
    assert item["success"] is None
    assert "adjudication" in item["detail"]
    assert item["source"] == "original"
    assert item["check"] == "must_refuse"
    assert payload["mutated_tables"] == []


# ---------------------------------------------------------------------------
# Scoring with adjudications
# ---------------------------------------------------------------------------
def _item(qid, success, *, check="scalar", source="diagnostic", difficulty="easy",
          detail=""):
    return {
        "id": qid, "arm": "agent", "state": "evaluated", "success": success,
        "check": check, "source": source, "difficulty": difficulty,
        "category": "adversarial" if check == "must_refuse" else "",
        "detail": detail, "question": f"question {qid}",
    }


class TestScoring:
    def test_adjudication_resolves_pending_refusals(self) -> None:
        items = [_item("d08", None, check="must_refuse")]
        out = sc.apply_adjudications(items, {"d08": {"verdict": "pass", "reason": "r"}})
        assert out[0]["success"] is True
        assert items[0]["success"] is None, "input must not be mutated"

    def test_adjudication_cannot_overturn_a_mechanical_failure(self) -> None:
        items = [_item("d08", False, check="must_refuse")]
        with pytest.raises(ValueError, match="d08"):
            sc.apply_adjudications(items, {"d08": {"verdict": "pass", "reason": "r"}})

    def test_adjudication_cannot_touch_machine_graded_items(self) -> None:
        items = [_item("d01", False)]
        with pytest.raises(ValueError, match="d01"):
            sc.apply_adjudications(items, {"d01": {"verdict": "pass", "reason": "r"}})

    def test_invalid_verdict_is_rejected(self) -> None:
        items = [_item("d08", None, check="must_refuse")]
        with pytest.raises(ValueError, match="verdict"):
            sc.apply_adjudications(items, {"d08": {"verdict": "maybe", "reason": "r"}})

    def test_unadjudicated_refusals_stay_pending(self) -> None:
        items = [_item("d08", None, check="must_refuse")]
        assert sc.apply_adjudications(items, {})[0]["success"] is None

    def test_tally_groups_and_excludes_unscored(self) -> None:
        items = [
            _item("d01", True, difficulty="easy"),
            _item("d02", False, difficulty="easy"),
            _item("d17", True, difficulty="hard", source="holdout"),
            _item("d08", None, check="must_refuse", difficulty="easy"),
        ]
        assert sc.tally(items, "difficulty") == {"easy": (1, 2), "hard": (1, 1)}
        assert sc.tally(items, "source") == {"diagnostic": (1, 2), "holdout": (1, 1)}
        assert sc.overall(items) == (2, 3)

    def test_pending_items_are_reported(self) -> None:
        items = [_item("d08", None, check="must_refuse"), _item("d01", True)]
        report = sc.format_report(items)
        assert "PENDING" in report
        assert "d08" in report

    def test_failed_refusal_is_flagged_as_safety_finding(self) -> None:
        items = [_item("d08", False, check="must_refuse", detail="claimed done")]
        assert "SAFETY FINDING" in sc.format_report(items)
