"""What one row of a table means, and the key endings join inference knows.

Both come from the messy unseen-schema run (eval/results/college/
college2.json): a per-student question was answered at a summary table's
student-and-course grain, and the course key crs_cd was not proposed as a
join. Pinned here: the draft asks for a row line per table, the line
survives editing and saving, it reaches the context every SQL-writing call
reads, NorthStar's context does not change, and _cd / _code / _no / _num make
a column a join candidate only when the data agrees.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from agentcrew.config import Settings
from agentcrew.context import ContextBuilder
from agentcrew.datasets.describe import (
    DESCRIBE_SYSTEM,
    ROW_LABEL,
    DraftNotes,
    Notes,
    draft_notes,
    render_dictionary,
)
from agentcrew.datasets.joins import KEY_SUFFIXES, infer_joins
from agentcrew.datasets.source import close_upload, upload_source
from agentcrew.datasets.store import DatasetStore
from agentcrew.db.engine import read_only_engine
from agentcrew.db.introspect import build_catalog
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext
from evalrun import college

GRAIN = "One row per student per course."


@pytest.fixture
def summary_db(tmp_path: Path):
    path = tmp_path / "s.db"
    with sqlite3.connect(path) as conn:
        pd.DataFrame({"stu_id": [1, 1, 2], "crs_cd": ["A", "B", "A"],
                      "att_pct": [80.0, 100.0, 60.0]}).to_sql("att_sum", conn,
                                                              index=False)
        pd.DataFrame({"stu_id": [1, 2], "stu_nm": ["A", "B"]}).to_sql(
            "stu_mst", conn, index=False)
    conn.close()
    engine = read_only_engine(path)
    yield engine
    engine.dispose()


def reply(row: str) -> dict[str, Any]:
    return {"tables": [{"name": "att_sum", "description": "Attendance summary",
                        "row": row, "columns": []}]}


class TestDraft:
    def test_the_prompt_asks_what_one_row_is(self) -> None:
        assert "what ONE row of the table is" in DESCRIBE_SYSTEM
        assert '"One row per student per course"' in DESCRIBE_SYSTEM
        assert "row" in DraftNotes.model_json_schema()["$defs"]["TableNote"][
            "properties"]

    def test_row_line_is_kept_per_table(self, summary_db) -> None:
        llm = FakeClient(responder=lambda s, u: reply("One row per student per course"))
        notes = draft_notes(llm, ToolContext.create(summary_db))
        assert notes.grains == {"att_sum": GRAIN}
        assert "stu_mst" not in notes.grains  # the model said nothing about it

    @pytest.mark.parametrize(("drafted", "kept"), [
        ("One row per student per course.", GRAIN),
        ("one row per student per course", "one row per student per course."),
        ("Each row is a student's attendance in one course",
         "Each row is a student's attendance in one course."),
        ("Student per course", GRAIN),
        ("  One row per student\nper course  ", GRAIN),
        ("", None),
    ])
    def test_row_line_is_normalised(self, summary_db, drafted: str,
                                    kept: str | None) -> None:
        llm = FakeClient(responder=lambda s, u: reply(drafted))
        notes = draft_notes(llm, ToolContext.create(summary_db))
        assert notes.grains.get("att_sum") == kept

    def test_a_reply_without_row_lines_still_works(self, summary_db) -> None:
        llm = FakeClient(responder=lambda s, u: {"tables": [
            {"name": "att_sum", "description": "x", "columns": []}]})
        assert draft_notes(llm, ToolContext.create(summary_db)).grains == {}


class TestEditingAndSaving:
    def test_editor_rows_carry_the_row_line(self, summary_db) -> None:
        catalog = build_catalog(summary_db)
        notes = Notes({"att_sum": "Attendance summary"}, {}, {"att_sum": GRAIN})
        rows = notes.rows(catalog)
        assert {"table": "att_sum", "column": ROW_LABEL, "description": GRAIN} in rows
        assert Notes.from_rows(rows, catalog) == notes

    def test_an_edited_row_line_is_kept_and_normalised(self, summary_db) -> None:
        catalog = build_catalog(summary_db)
        rows = Notes().rows(catalog)
        for row in rows:
            if row["table"] == "stu_mst" and row["column"] == ROW_LABEL:
                row["description"] = "student"
        assert Notes.from_rows(rows, catalog).grains == {
            "stu_mst": "One row per student."}

    def test_yaml_round_trip(self, summary_db) -> None:
        catalog = build_catalog(summary_db)
        notes = Notes({"att_sum": "Attendance summary"},
                      {("att_sum", "att_pct"): "Percent attended"},
                      {"att_sum": GRAIN})
        assert "row: One row per student per course." in notes.to_yaml()
        assert Notes.from_yaml(notes.to_yaml(), catalog) == notes

    def test_row_line_counts_for_schema_selection(self) -> None:
        notes = Notes({"t": "Summary"}, {}, {"t": "One row per student per course."})
        assert "student" in notes.table_text("t")


class TestReachesTheContext:
    NOTES = Notes({"att_sum": "Attendance summary", "stu_mst": "Students"}, {},
                  {"att_sum": GRAIN, "stu_mst": "One row per student."})

    def test_dictionary_puts_the_row_line_on_the_table_line(self, summary_db) -> None:
        text = render_dictionary(self.NOTES, [], build_catalog(summary_db))
        assert f"att_sum: Attendance summary. {GRAIN}" in text.splitlines()
        assert "stu_mst: Students. One row per student." in text.splitlines()
        assert "group by that thing alone" in text

    def test_no_row_line_no_trailing_text(self, summary_db) -> None:
        text = render_dictionary(Notes({"att_sum": "Summary."}), [],
                                 build_catalog(summary_db))
        assert "att_sum: Summary." in text.splitlines()

    @pytest.mark.parametrize("max_tables", [8, 1])
    def test_the_agents_context_contains_it(self, summary_db,
                                            max_tables: int) -> None:
        """Whole-schema context (8) and per-question selection (1)."""
        catalog = build_catalog(summary_db)
        builder = ContextBuilder.from_source(
            summary_db, catalog, today=date(2026, 1, 1), notes=self.NOTES,
            joins=[], max_tables=max_tables)
        context = builder.build("average att_pct per student").render()
        assert f"att_sum: Attendance summary. {GRAIN}" in context

    def test_an_uploads_run_sees_it(self, tmp_path: Path) -> None:
        """End to end through the store: the prompt A and D send holds it."""
        from agentcrew.archs.app_runner import build_runtime, run_arch_question

        settings = Settings(upload_dir=tmp_path, provider="fake", arch="baseline")
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        dataset = college.upload("messy", store, settings)
        dataset = store.update(dataset.id, "eval", notes=Notes(
            {"att_sum": "Attendance summary"}, {}, {"att_sum": GRAIN}))
        prompts: list[str] = []

        def model(system: str, user: str) -> dict[str, Any]:
            prompts.append(user)
            if system.startswith("You are a SQL analyst"):
                return {"sql": "SELECT COUNT(*) AS n FROM stu_mst", "rationale": "r"}
            return {"answer": "240.", "key_numbers": [], "caveats": []}

        runtime = build_runtime(settings, lambda s: FakeClient(responder=model),
                                source=upload_source(dataset, settings))
        run_arch_question("How many students?", settings=settings, runtime=runtime)
        close_upload(dataset)
        assert f"att_sum: Attendance summary. {GRAIN}" in prompts[0]

    def test_northstar_context_is_untouched(self, engine) -> None:
        context = ContextBuilder.from_database(
            engine, build_catalog(engine), today=date(2026, 10, 4)
        ).build("How many customers?").render()
        assert "group by that thing alone" not in context
        assert "customers: Customer accounts. One row per customer." in context


# ---------------------------------------------------------------------------
# Key endings
# ---------------------------------------------------------------------------
def two_tables(tmp_path: Path, column: str, *, parent: list, child: list):
    path = tmp_path / f"{column}.db"
    with sqlite3.connect(path) as conn:
        pd.DataFrame({column: parent, "label": [f"p{i}" for i in parent]}).to_sql(
            "products", conn, index=False)
        pd.DataFrame({column: child, "qty": range(len(child))}).to_sql(
            "sales", conn, index=False)
    conn.close()
    return read_only_engine(path)


class TestKeyEndings:
    def test_the_four_endings_are_key_endings(self) -> None:
        assert {"_cd", "_code", "_no", "_num"} <= set(KEY_SUFFIXES)

    @pytest.mark.parametrize("column", ["prod_cd", "prod_code", "prod_no",
                                        "prod_num"])
    def test_name_and_data_agree_gives_a_join(self, tmp_path: Path,
                                              column: str) -> None:
        engine = two_tables(tmp_path, column, parent=[1, 2, 3],
                            child=[1, 1, 2, 3, 3])
        found = [j.sql() for j in infer_joins(engine, build_catalog(engine))]
        engine.dispose()
        assert found == [f"sales.{column} = products.{column}"]

    @pytest.mark.parametrize("column", ["prod_cd", "prod_code", "prod_no",
                                        "prod_num"])
    def test_name_alone_is_not_enough(self, tmp_path: Path, column: str) -> None:
        # The values do not match: the name agrees, the data does not.
        engine = two_tables(tmp_path, column, parent=[1, 2, 3],
                            child=[7, 8, 9, 9])
        assert infer_joins(engine, build_catalog(engine)) == []
        engine.dispose()

    def test_no_unique_side_means_no_join(self, tmp_path: Path) -> None:
        engine = two_tables(tmp_path, "dept_cd", parent=["A", "A", "B"],
                            child=["A", "B", "B"])
        assert infer_joins(engine, build_catalog(engine)) == []
        engine.dispose()

    def test_messy_college_copy_gets_its_course_joins(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        dataset = college.upload("messy", store, Settings(upload_dir=tmp_path))
        joins = {j.sql() for j in dataset.joins}
        assert joins == {
            "att_log.stu_id = stu_mst.stu_id", "att_sum.stu_id = stu_mst.stu_id",
            "exm_mk.stu_id = stu_mst.stu_id", "att_log.crs_cd = crs_mst.crs_cd",
            "att_sum.crs_cd = crs_mst.crs_cd", "exm_mk.crs_cd = crs_mst.crs_cd",
        }
        assert not any("dept_cd" in j for j in joins)

    def test_clean_college_copy_is_unchanged(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        dataset = college.upload("clean", store, Settings(upload_dir=tmp_path))
        assert len(dataset.joins) == 4
