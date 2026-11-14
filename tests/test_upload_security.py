"""Security fixes to the upload path, one class per finding.

A review of the bring-your-own-data commit found five ways an upload could
misuse the server without ever writing (the connection is read-only), plus
a file handle left open. Each class below pins one fix and is named after
the finding:

1. Identifiers from uploaded schemas were spliced into internal SQL between
   hand-written quotes, so a name containing a quote could continue as SQL.
2. Virtual tables were recognised by the text of their CREATE statement,
   which the uploader controls.
3. The CSV column limit was checked after pandas had parsed the whole file.
4. The store's total limit was checked only after a conversion finished.
5. Nothing capped the size of the file a conversion wrote.
6. The converter left its SQLite connection to the garbage collector
   (on Windows the file could not be deleted).
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from openpyxl import Workbook

from agentcrew.archs.explore import NextAction, ValueIndex, lookup
from agentcrew.archs.fanout import KeyIndex
from agentcrew.config import Settings
from agentcrew.context.values import profile_columns
from agentcrew.datasets import convert, joins
from agentcrew.datasets import ingest as ingest_module
from agentcrew.datasets.convert import Limits, csv_to_sqlite, excel_to_sqlite
from agentcrew.datasets.errors import ConvertedTooLarge, UploadError
from agentcrew.datasets.ingest import STORE_FULL, ingest
from agentcrew.datasets.joins import infer_joins
from agentcrew.datasets.sqlite_upload import check_and_copy, name_problem
from agentcrew.datasets.store import Dataset, DatasetStore
from agentcrew.db.engine import read_only_engine
from agentcrew.db.introspect import build_catalog, quote_name, sample_values
from agentcrew.tools import ToolContext, get_sample_rows

#: A table name written to break out of f'"{name}"' quoting and read the
#: schema table instead, and a column name with a quote in it.
EVIL_TABLE = 't" UNION SELECT sql FROM sqlite_master --'
EVIL_COLUMN = 'a"b'


def make_store(tmp_path: Path, max_bytes: int = 10**8) -> DatasetStore:
    return DatasetStore(tmp_path / "store", max_bytes=max_bytes, ttl_seconds=60,
                        sweep=False)


def folders(store: DatasetStore) -> list[Path]:
    return list(store.root.iterdir())


def big_csv(rows: int = 20_000) -> bytes:
    return ("n,label\n" + "".join(f"{i},row number {i}\n" for i in range(rows))).encode()


# ---------------------------------------------------------------------------
# 1. Identifiers are quoted by the dialect, and unsafe names are refused
# ---------------------------------------------------------------------------
@pytest.fixture
def hostile_db(tmp_path: Path):
    """A database whose names contain quotes, built directly (an upload of it
    is refused; a self-hosted PostgreSQL schema could still look like this)."""
    path = tmp_path / "hostile.db"
    with sqlite3.connect(path) as conn:
        q = lambda name: '"' + name.replace('"', '""') + '"'  # noqa: E731
        conn.execute(f"CREATE TABLE {q(EVIL_TABLE)} ({q(EVIL_COLUMN)} TEXT, "
                     f"{q('cust_id')} INTEGER)")
        conn.executemany(f"INSERT INTO {q(EVIL_TABLE)} VALUES (?, ?)",
                         [("x", 1), ("y", 2), ("x", 3)])
        conn.execute("CREATE TABLE customers (cust_id INTEGER, name TEXT)")
        conn.executemany("INSERT INTO customers VALUES (?, ?)",
                         [(1, "A"), (2, "B"), (3, "C")])
    conn.close()
    engine = read_only_engine(path)
    yield engine
    engine.dispose()


class TestIdentifierQuoting:
    def test_quote_name_doubles_embedded_quotes(self, hostile_db) -> None:
        assert quote_name(hostile_db, "orders") == '"orders"'  # unchanged for NorthStar
        assert quote_name(hostile_db, EVIL_COLUMN) == '"a""b"'

    def test_every_internal_query_treats_the_name_as_a_name(self, hostile_db) -> None:
        catalog = build_catalog(hostile_db)
        # A broken-out name would fail (row_count -1) or count the wrong thing.
        assert catalog.tables[EVIL_TABLE].row_count == 3
        assert sorted(sample_values(hostile_db, EVIL_TABLE, EVIL_COLUMN)) == ["x", "y"]

        profile = profile_columns(hostile_db, catalog)
        assert [(v.table, v.column, v.values) for v in profile.values
                if v.table == EVIL_TABLE] == [(EVIL_TABLE, EVIL_COLUMN, ("x", "y"))]

        index = ValueIndex(hostile_db, catalog)
        assert sorted(index.values_of(EVIL_TABLE, EVIL_COLUMN)) == ["x", "y"]
        assert KeyIndex(hostile_db, catalog).unique(EVIL_TABLE,
                                                    frozenset({"cust_id"}))
        assert not KeyIndex(hostile_db, catalog).unique(EVIL_TABLE,
                                                        frozenset({EVIL_COLUMN}))

        tools = ToolContext.create(hostile_db, catalog=catalog)
        assert get_sample_rows(tools, EVIL_TABLE)["rows"][0] == ["x", 1]
        found = lookup(NextAction(action="get_distinct_values", table=EVIL_TABLE,
                                  column=EVIL_COLUMN), tools)
        assert found.ok and found.rows == [["x"], ["y"]]

        # One join on cust_id between the two tables (the data is one-to-one,
        # so either direction is a correct proposal).
        (join,) = infer_joins(hostile_db, catalog)
        assert {join.from_table, join.to_table} == {EVIL_TABLE, "customers"}
        assert (join.from_column, join.to_column) == ("cust_id", "cust_id")

    def test_no_schema_text_leaks_through_a_name(self, hostile_db) -> None:
        """The injected UNION would return CREATE TABLE text as values."""
        catalog = build_catalog(hostile_db)
        seen = repr([profile_columns(hostile_db, catalog),
                     ValueIndex(hostile_db, catalog).values,
                     sample_values(hostile_db, EVIL_TABLE, EVIL_COLUMN)])
        assert "CREATE TABLE" not in seen

    @pytest.mark.parametrize(("name", "reason"), [
        (EVIL_TABLE, "quote character"), ("a`b", "quote character"),
        ("line\nbreak", "control character"), ("nul\x00", "control character"),
        ("x" * 129, "longer than"), ("  ", "empty"),
    ])
    def test_unsafe_names_are_named_as_such(self, name: str, reason: str) -> None:
        assert reason in name_problem(name)

    @pytest.mark.parametrize("name", ["Order Details", "customer_id", "naïve",
                                      "2024 sales", "it's"])
    def test_ordinary_names_are_accepted(self, name: str) -> None:
        assert name_problem(name) is None

    def test_an_upload_with_such_names_is_refused(self, tmp_path: Path,
                                                  hostile_db) -> None:
        src = Path(hostile_db.url.database.replace("file:", "").split("?")[0])
        with pytest.raises(UploadError, match="table name contains a quote"):
            check_and_copy(src, tmp_path / "out.db", max_bytes=10**6)
        assert not (tmp_path / "out.db").exists()

    def test_a_quote_in_a_column_name_is_refused_too(self, tmp_path: Path) -> None:
        src = tmp_path / "c.db"
        with sqlite3.connect(src) as conn:
            conn.execute('CREATE TABLE t ("a""b" TEXT)')
        conn.close()
        with pytest.raises(UploadError, match="column name in table t"):
            check_and_copy(src, tmp_path / "out.db", max_bytes=10**6)

    def test_join_checks_run_under_the_statement_timeout(
        self, hostile_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[float] = []
        real = joins.execute_select

        def spy(engine: Any, sql: str, *, max_rows: int, timeout: float) -> Any:
            calls.append(timeout)
            return real(engine, sql, max_rows=max_rows, timeout=timeout)

        monkeypatch.setattr(joins, "execute_select", spy)
        infer_joins(hostile_db, build_catalog(hostile_db))
        assert calls and set(calls) == {joins.QUERY_TIMEOUT}

    def test_join_inference_stops_at_its_budget(self, hostile_db) -> None:
        assert infer_joins(hostile_db, build_catalog(hostile_db), budget=-1) == []


# ---------------------------------------------------------------------------
# 2. Virtual tables are detected from SQLite's metadata
# ---------------------------------------------------------------------------
class TestVirtualTableDetection:
    def _disguised(self, tmp_path: Path) -> Path:
        """A virtual table whose stored CREATE text no longer starts with
        CREATE VIRTUAL TABLE: a comment is edited into sqlite_master."""
        src = tmp_path / "v.db"
        conn = sqlite3.connect(src)
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql = replace(sql, "
                     "'CREATE VIRTUAL TABLE', 'CREATE /* x */ VIRTUAL TABLE') "
                     "WHERE name = 'docs'")
        conn.commit()
        conn.close()
        return src

    def test_the_old_text_check_would_have_missed_it(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(self._disguised(tmp_path))
        (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'docs'"
                              ).fetchone()
        kind = [r[2] for r in conn.execute("PRAGMA table_list") if r[1] == "docs"]
        conn.close()
        assert not sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
        assert kind == ["virtual"]  # SQLite still knows what it is

    def test_disguised_virtual_table_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(UploadError, match="virtual tables"):
            check_and_copy(self._disguised(tmp_path), tmp_path / "out.db",
                           max_bytes=10**6)
        assert not (tmp_path / "out.db").exists()

    def test_plain_tables_and_views_still_pass(self, tmp_path: Path) -> None:
        src = tmp_path / "ok.db"
        conn = sqlite3.connect(src)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("CREATE VIEW v AS SELECT a FROM t")
        conn.commit()
        conn.close()
        assert check_and_copy(src, tmp_path / "out.db", max_bytes=10**6) == ["t"]


# ---------------------------------------------------------------------------
# 3. The CSV column count is checked from the header, before the parse
# ---------------------------------------------------------------------------
class TestCsvHeaderCheck:
    def test_wide_header_is_refused_without_parsing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_parse(*_: Any, **__: Any) -> None:
            raise AssertionError("the file was parsed before the column check")

        monkeypatch.setattr(convert.pd, "read_csv", no_parse)
        header = ",".join(f"c{i}" for i in range(300)) + "\n"
        with pytest.raises(UploadError, match="300 columns; the limit is 200"):
            csv_to_sqlite(header.encode() + b"1\n", tmp_path / "d.db", label="w.csv")
        assert not (tmp_path / "d.db").exists()

    def test_a_million_empty_fields(self, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(convert.pd, "read_csv",
                            lambda *a, **k: pytest.fail("parsed"))
        with pytest.raises(UploadError, match="columns; the limit"):
            csv_to_sqlite(b"," * 1_000_000 + b"\n", tmp_path / "d.db", label="m.csv")

    def test_semicolon_files_are_counted_with_their_delimiter(
        self, tmp_path: Path
    ) -> None:
        tables = csv_to_sqlite(b"a;b;c\n1;2;3\n4;5;6\n", tmp_path / "d.db",
                               label="s.csv", limits=Limits(max_columns=3))
        assert tables[0].columns == ("a", "b", "c")


# ---------------------------------------------------------------------------
# 4. The store limit is checked before conversion, and enforced during it
# ---------------------------------------------------------------------------
def fill(store: DatasetStore, size: int) -> None:
    dataset_id, folder = store.new_folder()
    (folder / "data.db").write_bytes(b"x" * 10)
    store.add(Dataset(id=dataset_id, owner="other", label="f", kind="csv",
                      path=folder / "data.db", size_bytes=size, tables=("t",)))


class TestStoreLimit:
    def test_full_store_refuses_before_converting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = make_store(tmp_path, max_bytes=1000)
        fill(store, 1000)
        monkeypatch.setattr(ingest_module, "csv_to_sqlite",
                            lambda *a, **k: pytest.fail("converted a refused upload"))
        before = folders(store)
        with pytest.raises(UploadError) as caught:
            ingest(store, owner="o", filename="a.csv", data=b"x\n1\n",
                   settings=Settings(upload_dir=tmp_path))
        assert str(caught.value) == STORE_FULL
        assert folders(store) == before, "no folder was even created"

    def test_upload_larger_than_the_room_left_is_refused_up_front(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = make_store(tmp_path, max_bytes=5000)
        fill(store, 4000)  # 1000 bytes of room
        monkeypatch.setattr(ingest_module, "csv_to_sqlite",
                            lambda *a, **k: pytest.fail("converted"))
        with pytest.raises(UploadError, match="holding as much"):
            ingest(store, owner="o", filename="a.csv", data=b"x\n" + b"1\n" * 2000,
                   settings=Settings(upload_dir=tmp_path))

    def test_conversion_stops_when_it_would_overfill_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upload fits the room as raw bytes but not once converted: the
        write is refused part-way, not after the whole file exists."""
        monkeypatch.setattr(convert, "CHUNK_ROWS", 200)
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("big")
        ws.append(["n", "label"])
        for i in range(4000):
            ws.append([i, f"a fairly long label for row number {i}"])
        buf = io.BytesIO()
        wb.save(buf)
        raw = buf.getvalue()
        store = make_store(tmp_path, max_bytes=len(raw) + 20_000)
        said: list[str] = []
        with pytest.raises(UploadError) as caught:
            ingest(store, owner="o", filename="big.xlsx", data=raw,
                   settings=Settings(upload_dir=tmp_path), progress=said.append)
        assert str(caught.value) == STORE_FULL
        assert isinstance(caught.value.__cause__, ConvertedTooLarge)
        chunks = [m for m in said if "rows so far" in m]
        assert 0 < len(chunks) < 4000 // 200, "it stopped mid-conversion"
        assert folders(store) == [], "the partial file is gone"
        assert store.total_bytes() == 0

    def test_room_is_what_other_datasets_leave(self, tmp_path: Path) -> None:
        store = make_store(tmp_path, max_bytes=1000)
        assert store.remaining_bytes() == 1000
        fill(store, 300)
        assert store.remaining_bytes() == 700
        fill(store, 700)
        assert store.remaining_bytes() == 0


# ---------------------------------------------------------------------------
# 5. The converted file's size is capped while it is written
# ---------------------------------------------------------------------------
class TestConvertedSizeCap:
    def test_csv_conversion_stops_at_the_cap(self, tmp_path: Path) -> None:
        dest = tmp_path / "d.db"
        with pytest.raises(ConvertedTooLarge, match="larger than 0.1 MB"):
            csv_to_sqlite(big_csv(), dest, label="big.csv",
                          limits=Limits(max_bytes=100_000))
        assert dest.stat().st_size <= 100_000 + 4096

    def test_excel_conversion_stops_at_the_cap(self, tmp_path: Path) -> None:
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("s")
        ws.append(["n", "label"])
        for i in range(5000):
            ws.append([i, f"label {i}"])
        buf = io.BytesIO()
        wb.save(buf)
        dest = tmp_path / "d.db"
        with pytest.raises(ConvertedTooLarge):
            excel_to_sqlite(buf.getvalue(), dest, label="s.xlsx",
                            limits=Limits(max_bytes=40_000))
        assert dest.stat().st_size <= 40_000 + 4096

    def test_several_csv_files_share_one_cap(self, tmp_path: Path) -> None:
        dest = tmp_path / "d.db"
        limits = Limits(max_bytes=300_000)
        half = big_csv(6000)
        csv_to_sqlite(half, dest, label="a.csv", limits=limits)
        with pytest.raises(ConvertedTooLarge):
            csv_to_sqlite(big_csv(12_000), dest, label="b.csv", limits=limits)

    def test_ingest_reports_the_per_upload_limit_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        store = make_store(tmp_path)
        settings = Settings(upload_dir=tmp_path, max_converted_mb=0.1)
        with pytest.raises(UploadError, match="larger than 0.1 MB once converted"):
            ingest(store, owner="o", filename="big.csv", data=big_csv(),
                   settings=settings)
        assert folders(store) == [] and store.total_bytes() == 0

    def test_under_the_cap_nothing_changes(self, tmp_path: Path) -> None:
        tables = csv_to_sqlite(big_csv(100), tmp_path / "d.db", label="s.csv")
        assert tables[0].rows == 100
        assert Settings().max_converted_mb == 50


# ---------------------------------------------------------------------------
# 6. The converter closes its connection
# ---------------------------------------------------------------------------
class Tracked(sqlite3.Connection):
    opened: list[Tracked] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.was_closed = False
        Tracked.opened.append(self)

    def close(self) -> None:
        self.was_closed = True
        super().close()


class TestConnectionIsClosed:
    @pytest.fixture(autouse=True)
    def tracked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        Tracked.opened = []
        real = sqlite3.connect
        monkeypatch.setattr(convert.sqlite3, "connect",
                            lambda path, **kw: real(path, factory=Tracked, **kw))

    def test_csv(self, tmp_path: Path) -> None:
        dest = tmp_path / "d.db"
        csv_to_sqlite(b"a,b\n1,2\n", dest, label="x.csv")
        assert Tracked.opened and all(c.was_closed for c in Tracked.opened)
        dest.unlink()  # on Windows this fails while a handle is open

    def test_excel(self, tmp_path: Path) -> None:
        wb = Workbook()
        wb.active.append(["a"])
        wb.active.append([1])
        buf = io.BytesIO()
        wb.save(buf)
        dest = tmp_path / "d.db"
        excel_to_sqlite(buf.getvalue(), dest, label="x.xlsx")
        assert Tracked.opened and all(c.was_closed for c in Tracked.opened)
        dest.unlink()

    def test_closed_even_when_the_conversion_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ConvertedTooLarge):
            csv_to_sqlite(big_csv(), tmp_path / "d.db", label="big.csv",
                          limits=Limits(max_bytes=50_000))
        assert Tracked.opened and all(c.was_closed for c in Tracked.opened)

    def test_a_removed_dataset_leaves_no_folder(self, tmp_path: Path) -> None:
        """The visible symptom on Windows: the folder survived removal."""
        store = make_store(tmp_path)
        dataset = ingest(store, owner="o", settings=Settings(upload_dir=tmp_path),
                         files=[("a.csv", b"k,v\n1,2\n"),
                                ("b.csv", pd.DataFrame({"k": [1]}).to_csv(
                                    index=False).encode())])
        assert store.remove(dataset.id)
        assert folders(store) == []
