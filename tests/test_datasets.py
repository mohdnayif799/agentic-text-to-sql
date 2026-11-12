"""Bring your own data (agentcrew/datasets): conversion, untrusted SQLite,
the store, ingest, inferred joins, descriptions and the source the agent
answers from. Offline: the model is FakeClient."""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from openpyxl import Workbook
from sqlalchemy import text

from agentcrew.config import Settings
from agentcrew.datasets import convert
from agentcrew.datasets.convert import Limits, csv_to_sqlite, excel_to_sqlite
from agentcrew.datasets.describe import (
    DESCRIBE_SYSTEM,
    Notes,
    draft_notes,
    render_dictionary,
)
from agentcrew.datasets.errors import UploadError
from agentcrew.datasets.ingest import ingest, kind_of
from agentcrew.datasets.joins import Join, check_join, infer_joins, with_joins
from agentcrew.datasets.names import to_identifier, unique_identifiers
from agentcrew.datasets.source import close_upload, reset_sources, upload_source
from agentcrew.datasets.sqlite_upload import check_and_copy
from agentcrew.datasets.store import Dataset, DatasetStore, StoreFull, remove_leftovers
from agentcrew.db.engine import read_only_engine
from agentcrew.db.introspect import build_catalog
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext, execute_readonly_sql


@pytest.fixture(autouse=True)
def _fresh_sources():
    reset_sources()
    yield
    reset_sources()


def csv_bytes(df: pd.DataFrame, **kw: Any) -> bytes:
    return df.to_csv(index=False, **kw).encode("utf-8")


def rows(path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def make_settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(upload_dir=tmp_path / "uploads", **kw)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
class TestNames:
    @pytest.mark.parametrize(("label", "expected"), [
        ("Student Name", "student_name"),
        ("  Marks (%) ", "marks"),
        ("2024 score", "column_2024_score"),
        ("order", "order_"),
        ("Café", "cafe"),
        ('x"; DROP TABLE t; --', "x_drop_table_t"),
        ("", "column_"),  # the fallback is itself a reserved word
    ])
    def test_to_identifier(self, label: str, expected: str) -> None:
        assert to_identifier(label) == expected

    def test_unique_and_blank_columns(self) -> None:
        assert unique_identifiers(["Name", "name", "", "Unnamed: 3", "NAME"]) == [
            "name", "name_2", "column_3", "column_4", "name_3"]

    def test_long_names_are_cut_and_stay_unique(self) -> None:
        names = unique_identifiers(["a" * 100, "a" * 100])
        assert len(set(names)) == 2 and all(len(n) <= 60 for n in names)


# ---------------------------------------------------------------------------
# CSV and Excel
# ---------------------------------------------------------------------------
class TestCsv:
    def test_types_names_and_iso_dates(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"Student ID": [1, 2], "Name": ["Ana", "Bo"],
                           "Joined": ["2025-01-05", "2025-02-10"],
                           "Fee": [10.5, 20.0], "Active": [True, False]})
        tables = csv_to_sqlite(csv_bytes(df), tmp_path / "d.db",
                               label="My Students.csv")
        assert tables[0].name == "my_students"
        assert tables[0].columns == ("student_id", "name", "joined", "fee", "active")
        got = rows(tmp_path / "d.db", "SELECT * FROM my_students ORDER BY 1")
        assert got == [(1, "Ana", "2025-01-05", 10.5, 1), (2, "Bo", "2025-02-10", 20.0, 0)]

    def test_semicolon_delimiter_and_cp1252(self, tmp_path: Path) -> None:
        data = "city;amount\nZürich;5\nMünchen;7\n".encode("cp1252")
        csv_to_sqlite(data, tmp_path / "d.db", label="x.csv")
        assert rows(tmp_path / "d.db", "SELECT * FROM x ORDER BY 2") == [
            ("Zürich", 5), ("München", 7)]

    def test_row_and_column_limits(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"a": range(11)})
        with pytest.raises(UploadError, match="more than 10 rows"):
            csv_to_sqlite(csv_bytes(df), tmp_path / "a.db", label="a.csv",
                          limits=Limits(max_rows=10))
        wide = pd.DataFrame([list(range(5))], columns=list("abcde"))
        with pytest.raises(UploadError, match="5 columns"):
            csv_to_sqlite(csv_bytes(wide), tmp_path / "b.db", label="b.csv",
                          limits=Limits(max_columns=4))

    def test_empty_and_garbage(self, tmp_path: Path) -> None:
        with pytest.raises(UploadError, match="empty"):
            csv_to_sqlite(b"   ", tmp_path / "a.db", label="a.csv")


def xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for title, data in sheets.items():
        ws = wb.create_sheet(title)
        for row in data:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestExcel:
    def test_one_table_per_sheet_dates_as_iso(self, tmp_path: Path) -> None:
        data = xlsx({
            "Students": [["ID", "Name", "Born"], [1, "Ana", date(2004, 3, 1)],
                         [2, "Bo", datetime(2005, 7, 9, 14, 30)]],
            "Empty": [],
            "Marks": [["Student ID", "Score"], [1, 88], [2, 91]],
        })
        tables = excel_to_sqlite(data, tmp_path / "d.db", label="college.xlsx")
        assert [t.name for t in tables] == ["students", "marks"]
        assert rows(tmp_path / "d.db", "SELECT born FROM students ORDER BY id") == [
            ("2004-03-01",), ("2005-07-09 14:30:00",)]

    def test_streams_in_chunks(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(convert, "CHUNK_ROWS", 3)
        data = xlsx({"S": [["n"], *[[i] for i in range(10)]]})
        excel_to_sqlite(data, tmp_path / "d.db", label="s.xlsx")
        assert rows(tmp_path / "d.db", "SELECT COUNT(*), SUM(n) FROM s") == [(10, 45)]

    def test_not_a_workbook(self, tmp_path: Path) -> None:
        with pytest.raises(UploadError, match="not a valid .xlsx"):
            excel_to_sqlite(b"plain text", tmp_path / "d.db", label="x.xlsx")

    def test_zip_bomb_ratio_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(convert, "MAX_UNZIP_RATIO", 1)
        data = xlsx({"S": [["n"], *[[i] for i in range(200)]]})
        with pytest.raises(UploadError, match="expands"):
            excel_to_sqlite(data, tmp_path / "d.db", label="s.xlsx")

    def test_sheet_limit(self, tmp_path: Path) -> None:
        data = xlsx({f"S{i}": [["a"], [1]] for i in range(3)})
        with pytest.raises(UploadError, match="3 sheets"):
            excel_to_sqlite(data, tmp_path / "d.db", label="s.xlsx",
                            limits=Limits(max_tables=2))


# ---------------------------------------------------------------------------
# Untrusted SQLite
# ---------------------------------------------------------------------------
def sqlite_file(path: Path, *statements: str) -> Path:
    with sqlite3.connect(path) as conn:
        for s in statements:
            conn.execute(s)
    return path


class TestSqliteUpload:
    def test_copies_a_good_file(self, tmp_path: Path) -> None:
        src = sqlite_file(tmp_path / "in.db", "CREATE TABLE t (a INTEGER)",
                          "INSERT INTO t VALUES (1)",
                          "CREATE VIEW v AS SELECT a FROM t")
        names = check_and_copy(src, tmp_path / "out.db", max_bytes=10**6)
        assert names == ["t"]
        assert rows(tmp_path / "out.db", "SELECT * FROM v") == [(1,)]

    def test_not_sqlite(self, tmp_path: Path) -> None:
        bad = tmp_path / "x.db"
        bad.write_bytes(b"PK\x03\x04 not a database at all")
        with pytest.raises(UploadError, match="not a SQLite database"):
            check_and_copy(bad, tmp_path / "out.db", max_bytes=10**6)

    def test_too_big(self, tmp_path: Path) -> None:
        src = sqlite_file(tmp_path / "in.db", "CREATE TABLE t (a)")
        with pytest.raises(UploadError, match="larger than"):
            check_and_copy(src, tmp_path / "out.db", max_bytes=10)

    def test_virtual_tables_are_refused(self, tmp_path: Path) -> None:
        src = sqlite_file(tmp_path / "in.db",
                          "CREATE VIRTUAL TABLE docs USING fts5(body)")
        with pytest.raises(UploadError, match="virtual tables"):
            check_and_copy(src, tmp_path / "out.db", max_bytes=10**6)
        assert not (tmp_path / "out.db").exists()

    def test_no_tables(self, tmp_path: Path) -> None:
        src = sqlite_file(tmp_path / "in.db", "CREATE TABLE t (a)", "DROP TABLE t")
        with pytest.raises(UploadError, match="no tables"):
            check_and_copy(src, tmp_path / "out.db", max_bytes=10**6)

    def test_damaged_file(self, tmp_path: Path) -> None:
        src = sqlite_file(tmp_path / "in.db", "CREATE TABLE t (a TEXT)",
                          *[f"INSERT INTO t VALUES ('{'x' * 500}')" for _ in range(50)])
        data = bytearray(src.read_bytes())
        data[4096:8192] = b"\xff" * 4096  # corrupt the second page
        src.write_bytes(bytes(data))
        with pytest.raises(UploadError):
            check_and_copy(src, tmp_path / "out.db", max_bytes=10**6)

    def test_trigger_cannot_write_through_the_agent(self, tmp_path: Path) -> None:
        """A trigger in someone else's file never fires: the agent's handle is
        read-only, and a write statement never reaches it."""
        src = sqlite_file(tmp_path / "in.db", "CREATE TABLE t (a)",
                          "CREATE TABLE log (x)",
                          "CREATE TRIGGER tr AFTER INSERT ON t BEGIN "
                          "INSERT INTO log VALUES (1); END")
        out = tmp_path / "out.db"
        check_and_copy(src, out, max_bytes=10**6)
        engine = read_only_engine(out)
        ctx = ToolContext.create(engine)
        result = execute_readonly_sql(ctx, "INSERT INTO t VALUES (5)")
        assert not result.ok
        with pytest.raises(Exception, match="readonly|read-only|query_only"):
            with engine.connect() as conn:
                conn.execute(text("INSERT INTO t VALUES (5)"))
        engine.dispose()

    def test_agent_connection_is_hardened(self, tmp_path: Path) -> None:
        src = sqlite_file(tmp_path / "in.db", "CREATE TABLE t (a)")
        out = tmp_path / "out.db"
        check_and_copy(src, out, max_bytes=10**6)
        engine = read_only_engine(out)
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA trusted_schema")).scalar() == 0
            assert conn.execute(text("PRAGMA query_only")).scalar() == 1
        engine.dispose()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def dataset_in(store: DatasetStore, owner: str = "s1", size: int = 10) -> Dataset:
    dataset_id, folder = store.new_folder()
    (folder / "data.db").write_bytes(b"x" * size)
    return store.add(Dataset(id=dataset_id, owner=owner, label="f.csv", kind="csv",
                             path=folder / "data.db", size_bytes=size,
                             tables=("t",)))


class TestStore:
    def test_owner_isolation(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path, max_bytes=100, ttl_seconds=60, sweep=False)
        d = dataset_in(store, owner="alice")
        assert store.get(d.id, "alice") == d
        assert store.get(d.id, "mallory") is None
        assert store.update(d.id, "mallory", joins=()) is None

    def test_total_limit(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path, max_bytes=15, ttl_seconds=60, sweep=False)
        dataset_in(store, size=10)
        with pytest.raises(StoreFull):
            dataset_in(store, size=10)
        assert len([p for p in tmp_path.iterdir()]) == 1, "the refused folder is gone"

    def test_sweep_removes_unused_and_calls_hooks(self, tmp_path: Path) -> None:
        clock = Clock()
        store = DatasetStore(tmp_path, max_bytes=100, ttl_seconds=60, clock=clock,
                             sweep=False)
        removed: list[str] = []
        store.on_remove(lambda d: removed.append(d.id))
        old, fresh = dataset_in(store), dataset_in(store)
        clock.now += 50
        store.get(fresh.id, "s1")
        clock.now += 20
        assert store.sweep() == [old.id]
        assert removed == [old.id] and not old.path.parent.exists()
        assert fresh.path.exists()

    def test_update_bumps_revision_and_key(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path, max_bytes=100, ttl_seconds=60, sweep=False)
        d = dataset_in(store)
        updated = store.update(d.id, "s1", joins=(Join("a", "b_id", "b", "id"),))
        assert updated.revision == 1 and updated.key != d.key

    def test_remove_owner(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path, max_bytes=100, ttl_seconds=60, sweep=False)
        dataset_in(store, owner="a"), dataset_in(store, owner="a")
        dataset_in(store, owner="b")
        assert store.remove_owner("a") == 2 and len(store.owned_by("b")) == 1

    def test_leftover_cleanup_touches_only_dataset_folders(self, tmp_path: Path) -> None:
        (tmp_path / ("a" * 32)).mkdir()
        (tmp_path / "keep-me").mkdir()
        (tmp_path / "notes.txt").write_text("x")
        assert remove_leftovers(tmp_path) == 1
        assert sorted(p.name for p in tmp_path.iterdir()) == ["keep-me", "notes.txt"]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
COLLEGE = {
    "students": pd.DataFrame({"student_id": [1, 2, 3], "name": ["A", "B", "C"],
                              "dept": ["CS", "EE", "CS"]}),
    "courses": pd.DataFrame({"course_id": [10, 20], "name": ["DB", "ML"],
                             "dept": ["CS", "CS"]}),
    "marks": pd.DataFrame({"student_id": [1, 1, 2, 3], "course_id": [10, 20, 10, 20],
                           "score": [80, 90, 70, 60], "id": [1, 2, 3, 4]}),
}


def college_xlsx() -> bytes:
    return xlsx({name: [list(df.columns), *df.values.tolist()]
                 for name, df in COLLEGE.items()})


class TestIngest:
    def test_kinds(self) -> None:
        assert kind_of("a.CSV") == "csv" and kind_of("b.xlsx") == "excel"
        assert kind_of("c.sqlite3") == "sqlite"
        with pytest.raises(UploadError, match=".xls files"):
            kind_of("old.xls")

    def test_excel_upload_end_to_end(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        d = ingest(store, owner="o", filename="college.xlsx", data=college_xlsx(),
                   settings=settings)
        assert d.tables == ("courses", "marks", "students")
        assert {j.sql() for j in d.joins} == {
            "marks.course_id = courses.course_id",
            "marks.student_id = students.student_id"}

    def test_size_limit_by_kind(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, max_upload_mb=0.00001)
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        with pytest.raises(UploadError, match="limit"):
            ingest(store, owner="o", filename="a.csv", data=b"a\n" * 100,
                   settings=settings)
        assert list((tmp_path / "s").iterdir()) == []

    def test_a_failure_leaves_no_folder(self, tmp_path: Path) -> None:
        store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60,
                             sweep=False)
        with pytest.raises(UploadError):
            ingest(store, owner="o", filename="a.db", data=b"not sqlite" * 10,
                   settings=make_settings(tmp_path))
        assert list((tmp_path / "s").iterdir()) == []


# ---------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------
@pytest.fixture
def college_db(tmp_path: Path):
    path = tmp_path / "college.db"
    with sqlite3.connect(path) as conn:
        for name, df in COLLEGE.items():
            df.to_sql(name, conn, index=False)
        # A table that shares only generic columns with the others.
        pd.DataFrame({"id": [1, 2, 3], "name": ["A", "B", "C"],
                      "date": ["2025-01-01"] * 3}).to_sql("events", conn, index=False)
    engine = read_only_engine(path)
    yield engine, build_catalog(engine)
    engine.dispose()


class TestJoins:
    def test_infers_key_joins_and_never_generic_ones(self, college_db) -> None:
        engine, catalog = college_db
        found = {j.sql() for j in infer_joins(engine, catalog)}
        assert found == {"marks.course_id = courses.course_id",
                         "marks.student_id = students.student_id"}
        for sql in found:
            assert ".id =" not in sql and ".name =" not in sql and ".date" not in sql

    def test_low_overlap_is_not_a_join(self, tmp_path: Path) -> None:
        path = tmp_path / "x.db"
        with sqlite3.connect(path) as conn:
            pd.DataFrame({"course_id": [1, 2, 3]}).to_sql("courses", conn, index=False)
            pd.DataFrame({"course_id": [7, 8, 9, 1]}).to_sql("marks", conn, index=False)
        engine = read_only_engine(path)
        assert infer_joins(engine, build_catalog(engine)) == []
        engine.dispose()

    def test_check_join_refuses_generic_both_sides(self, college_db) -> None:
        _, catalog = college_db
        assert "generic" in check_join(Join("events", "id", "marks", "id"), catalog)
        assert "no column" in check_join(Join("marks", "nope", "students", "id"),
                                         catalog)
        assert check_join(Join("marks", "student_id", "students", "student_id"),
                          catalog) is None

    def test_with_joins_does_not_modify_the_catalog(self, college_db) -> None:
        _, catalog = college_db
        before = list(catalog.foreign_keys)
        joined = with_joins(catalog, [Join("marks", "student_id", "students",
                                           "student_id")])
        assert catalog.foreign_keys == before
        assert "students" in joined.neighbours("marks")


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------
class TestDescriptions:
    def test_draft_validates_names_and_cleans_text(self, college_db) -> None:
        engine, catalog = college_db
        reply = {"tables": [
            {"name": "marks", "description": "Exam  marks\nper student",
             "columns": [{"name": "score", "description": "Mark out of 100"},
                         {"name": "made_up", "description": "x"}]},
            {"name": "ghost_table", "description": "invented"},
        ]}
        llm = FakeClient(responder=lambda s, u: reply)
        notes = draft_notes(llm, ToolContext.create(engine))
        assert notes.tables == {"marks": "Exam marks per student"}
        assert notes.columns == {("marks", "score"): "Mark out of 100"}
        system, user = llm.prompts[0]
        assert system.startswith(DESCRIBE_SYSTEM.splitlines()[0])
        assert "## Sample rows" in user

    def test_sample_cells_are_cut(self, tmp_path: Path) -> None:
        path = tmp_path / "x.db"
        with sqlite3.connect(path) as conn:
            pd.DataFrame({"note": ["y" * 500]}).to_sql("t", conn, index=False)
        engine = read_only_engine(path)
        llm = FakeClient(responder=lambda s, u: {"tables": []})
        draft_notes(llm, ToolContext.create(engine))
        samples = llm.prompts[0][1].split("## Sample rows", 1)[1]
        assert "y" * 40 in samples and "y" * 41 not in samples
        engine.dispose()

    def test_edit_round_trip_drops_unknown(self, college_db) -> None:
        _, catalog = college_db
        notes = Notes({"marks": "m"}, {("marks", "score"): "s"})
        edited = [*notes.rows(catalog),
                  {"table": "nope", "column": "", "description": "x"},
                  {"table": "marks", "column": "nope", "description": "x"}]
        back = Notes.from_rows(edited, catalog)
        assert back == notes
        assert Notes.from_yaml(notes.to_yaml(), catalog) == notes

    def test_dictionary_text(self, college_db) -> None:
        _, catalog = college_db
        text_ = render_dictionary(Notes({"marks": "Exam marks"}, {}),
                                  [Join("marks", "student_id", "students",
                                        "student_id")], catalog)
        assert "marks: Exam marks" in text_
        assert "- marks.student_id = students.student_id" in text_


# ---------------------------------------------------------------------------
# The source the agent answers from
# ---------------------------------------------------------------------------
def stored_college(tmp_path: Path) -> tuple[DatasetStore, Dataset, Settings]:
    settings = make_settings(tmp_path)
    store = DatasetStore(tmp_path / "s", max_bytes=10**8, ttl_seconds=60, sweep=False)
    d = ingest(store, owner="o", filename="college.xlsx", data=college_xlsx(),
               settings=settings)
    d = store.update(d.id, "o", notes=Notes({"marks": "Exam marks per student"}, {}))
    return store, d, settings


class TestSource:
    def test_tools_and_context(self, tmp_path: Path) -> None:
        _, d, settings = stored_college(tmp_path)
        source = upload_source(d, settings)
        tools = source.tool_context(settings)
        assert tools.metrics is False and tools.dialect == "sqlite"
        assert "students" in tools.catalog.neighbours("marks")
        context = source.context_builder(tools, date(2026, 1, 1), settings).build(
            "average score by department").render()
        assert "marks: Exam marks per student" in context
        assert "FOREIGN KEY JOINS AVAILABLE" in context
        assert "NorthStar" not in context and "order_items" not in context
        assert "## Verified examples" not in context
        close_upload(d)

    def test_selection_when_there_are_many_tables(self, tmp_path: Path) -> None:
        _, d, settings = stored_college(tmp_path)
        settings = settings.model_copy(update={"max_tables_in_context": 1})
        source = upload_source(d, settings)
        tools = source.tool_context(settings)
        built = source.context_builder(tools, date(2026, 1, 1), settings)
        ctx = built.build("exam marks per student").schema
        assert "TABLE marks" in ctx and "TABLE courses" not in ctx
        close_upload(d)

    def test_guard_and_caps_apply(self, tmp_path: Path) -> None:
        _, d, settings = stored_college(tmp_path)
        tools = upload_source(d, settings).tool_context(settings)
        assert not execute_readonly_sql(tools, "DELETE FROM marks").ok
        assert not execute_readonly_sql(tools, "ATTACH 'x.db' AS x").ok
        capped = execute_readonly_sql(tools, "SELECT * FROM marks")
        assert "LIMIT" in capped.sql.upper()
        close_upload(d)


# ---------------------------------------------------------------------------
# The agent on an upload
# ---------------------------------------------------------------------------
class Analyst:
    """Scripted model for A and D on the college upload."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.systems: list[str] = []

    def __call__(self, system: str, user: str) -> dict[str, Any]:
        self.systems.append(system)
        if system.startswith("You are a SQL analyst"):
            return {"sql": self.sql, "rationale": "r"}
        if system.startswith("You check whether one query result"):
            return {"needs_followup": False, "reason": ""}
        if system.startswith("You plan the next step"):
            return {"action": "answer", "reason": "done"}
        if system.startswith("You write the final answer"):
            return {"answer": "CS averages 70.", "key_numbers": [], "caveats": []}
        if system.startswith("Several queries were run"):
            return {"base_step": 1, "columns": []}
        raise AssertionError(system[:80])


class TestAgentOnUpload:
    def _run(self, tmp_path: Path, arch: str, question: str, **kw: Any):
        from agentcrew.archs.app_runner import build_runtime, run_arch_question

        _, d, settings = stored_college(tmp_path)
        settings = settings.model_copy(update={"provider": "fake", "arch": arch})
        analyst = Analyst("SELECT s.dept, AVG(m.score) AS avg_score FROM marks m "
                          "JOIN students s ON s.student_id = m.student_id "
                          "GROUP BY s.dept")
        runtime = build_runtime(settings, lambda s: FakeClient(responder=analyst),
                                source=upload_source(d, settings))
        run = run_arch_question(question, settings=settings, runtime=runtime, **kw)
        close_upload(d)
        return run, analyst

    @pytest.mark.parametrize("arch", ["baseline", "full"])
    def test_answers_from_the_upload(self, tmp_path: Path, arch: str) -> None:
        run, _ = self._run(tmp_path, arch, "Average score by department?")
        assert run.outcome.status == "done"
        got = sorted((r[0], round(r[1], 2)) for r in run.outcome.selected.rows)
        assert got == [("CS", 76.67), ("EE", 70.0)]

    def test_why_question_skips_the_northstar_metric_tools(self, tmp_path: Path,
                                                           monkeypatch) -> None:
        from agentcrew.archs import full

        captured: dict[str, Any] = {}
        real = full.run_full

        def forced(*args, **kwargs):
            kwargs["force_escalation"] = True
            captured["ran"] = True
            return real(*args, **kwargs)

        monkeypatch.setattr("agentcrew.archs.run_full", forced)
        run, analyst = self._run(tmp_path, "full", "Why is the CS average low?")
        assert captured["ran"] and run.outcome.status == "done"
        assert not any(s.startswith("You start the analysis of a question that "
                                    "asks why") for s in analyst.systems)
        planner = [s for s in analyst.systems if s.startswith("You plan the next")]
        assert planner and full.NO_METRICS in planner[0]
        assert "order_items" not in json.dumps(analyst.systems)
