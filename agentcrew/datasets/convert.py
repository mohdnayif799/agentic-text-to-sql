"""CSV and Excel to a fresh SQLite file.

Why convert at all
------------------
The agent's safety rests on its database handle being read-only at the
connection level (db/engine.py), and SQLite gives that with one URI flag.
Converting a spreadsheet into a new SQLite file, then reopening that file
read-only, means an upload goes through exactly the code NorthStar does. No
second query engine (pandas, DuckDB) has to be made safe.

What the conversion does
------------------------
* Table and column names become safe identifiers (datasets/names.py).
* Dates are written as ISO text ('YYYY-MM-DD', or 'YYYY-MM-DD HH:MM:SS' when
  a value has a time), the format NorthStar uses, so the agent's date
  handling (value profile, date-format check, period filters) applies as is.
  Excel cells holding dates become ISO text; CSV text that is already ISO is
  left alone. Other date spellings (03/04/2025) stay text, because guessing
  day-first or month-first would silently change the data.
* Booleans become 0 and 1. Everything else keeps the type pandas infers.
* Limits on rows, columns and tables are checked as the data is read, so an
  oversized file is refused before it is fully in memory.

Progress
--------
A 10 MB workbook takes about 16 seconds to convert, so both converters take
an optional `progress` callback and report what they are doing in plain
words (which file or sheet, how many rows so far). The app shows the latest
message while it waits; nothing else depends on it.

Memory
------
CSV is read with pandas in one go: at the upload limit (10 MB) that is a
small multiple of the file's size. Excel is different: an .xlsx file is
zipped XML, often 5 to 10 times larger unzipped, so the workbook is opened
in openpyxl's streaming mode and written to SQLite in chunks of rows. Peak
memory for both was measured in the production image (docs/DATA_SOURCES.md).
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

from agentcrew.datasets.errors import UploadError
from agentcrew.datasets.names import to_identifier, unique_identifiers

#: Rows written to SQLite per insert batch (Excel is streamed in these too).
CHUNK_ROWS = 20_000
#: How much of a CSV is read to guess its delimiter.
SNIFF_BYTES = 64 * 1024
#: An .xlsx file whose members unzip to more than this many times the file's
#: own size is refused as a likely zip bomb.
MAX_UNZIP_RATIO = 60


@dataclass(frozen=True)
class Limits:
    max_rows: int = 500_000
    max_columns: int = 200
    max_tables: int = 30


DEFAULT_LIMITS = Limits()

Progress = Callable[[str], None]
"""Receives one short sentence each time a conversion step starts or a chunk
of rows is done. The text is shown to the visitor as it is."""


def _say(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


@dataclass(frozen=True)
class TableInfo:
    name: str
    """The SQL name."""
    source: str
    """The file or sheet it came from, as the visitor named it."""
    rows: int
    columns: tuple[str, ...]


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def _iso(value: Any) -> Any:
    """Dates and datetimes as ISO text; anything else unchanged."""
    if isinstance(value, datetime | pd.Timestamp):
        if pd.isna(value):
            return None
        if value.time() == time(0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    return value


def _normalise_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Dates to ISO text, booleans to 0/1, other columns untouched."""
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            out[col] = series.map(_iso).astype(object)
        elif pd.api.types.is_bool_dtype(series):
            out[col] = series.astype("Int64")
        elif series.dtype == object:
            if series.map(lambda v: isinstance(v, date | datetime | time)).any():
                out[col] = series.map(_iso)
    return out


def _write(conn: sqlite3.Connection, table: str, df: pd.DataFrame,
           *, first: bool) -> None:
    df.to_sql(table, conn, index=False, if_exists="fail" if first else "append",
              chunksize=5_000)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    """UTF-8 (with or without BOM), else Windows-1252, which never fails."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _delimiter(text: str) -> str:
    try:
        return csv.Sniffer().sniff(text[:SNIFF_BYTES], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def csv_to_sqlite(data: bytes, dest: Path, *, label: str,
                  limits: Limits = DEFAULT_LIMITS,
                  table: str | None = None,
                  progress: Progress | None = None) -> list[TableInfo]:
    """One table, named after the file unless `table` is given (several CSVs
    uploaded together each become a table of one file). Raises UploadError."""
    name = Path(label).name
    _say(progress, f"Reading {name} ({len(data) / 1e6:.1f} MB)")
    text = _decode(data)
    if not text.strip():
        raise UploadError(f"{name} is empty.")
    try:
        df = pd.read_csv(io.StringIO(text), sep=_delimiter(text),
                         nrows=limits.max_rows + 1, low_memory=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        raise UploadError(f"{name} could not be read as CSV: "
                          f"{str(exc).splitlines()[0][:200]}") from exc
    del text
    if len(df) > limits.max_rows:
        raise UploadError(f"{name} has more than {limits.max_rows:,} rows.")
    if df.shape[1] > limits.max_columns:
        raise UploadError(f"{name} has {df.shape[1]} columns; the limit is "
                          f"{limits.max_columns}.")
    if df.empty and not len(df.columns):
        raise UploadError(f"{name} has no columns.")
    df.columns = unique_identifiers(df.columns)
    table = table or to_identifier(Path(label).stem, fallback="data")
    _say(progress, f"Converting {name}: {len(df):,} rows")
    with sqlite3.connect(dest) as conn:
        _write(conn, table, _normalise_frame(df), first=True)
    return [TableInfo(table, label, len(df), tuple(df.columns))]


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            unzipped = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise UploadError("The file is not a valid .xlsx workbook.") from exc
    if unzipped > MAX_UNZIP_RATIO * max(len(data), 1):
        raise UploadError("The workbook expands to far more data than its size "
                          "suggests, so it was refused.")


def _rows(sheet: Any) -> Iterator[tuple[Any, ...]]:
    for row in sheet.iter_rows(values_only=True):
        if row is not None and any(v is not None and v != "" for v in row):
            yield row


def _sheet_to_sqlite(conn: sqlite3.Connection, sheet: Any, table: str,
                     limits: Limits, progress: Progress | None = None,
                     ) -> tuple[int, tuple[str, ...]] | None:
    rows = _rows(sheet)
    header = next(rows, None)
    if header is None:
        return None  # an empty sheet is skipped, not an error
    width = len(header)
    while width and (header[width - 1] is None or header[width - 1] == ""):
        width -= 1  # trailing blank header cells
    if width == 0:
        return None
    if width > limits.max_columns:
        raise UploadError(f"Sheet {sheet.title!r} has {width} columns; the limit "
                          f"is {limits.max_columns}.")
    columns = unique_identifiers(header[:width])
    chunk: list[Sequence[Any]] = []
    total = 0
    first = True
    for row in rows:
        values = list(row[:width]) + [None] * (width - len(row))
        chunk.append([_iso(v) for v in values])
        total += 1
        if total > limits.max_rows:
            raise UploadError(f"Sheet {sheet.title!r} has more than "
                              f"{limits.max_rows:,} rows.")
        if len(chunk) >= CHUNK_ROWS:
            _write(conn, table, _normalise_frame(pd.DataFrame(chunk, columns=columns)),
                   first=first)
            first, chunk = False, []
            _say(progress, f"Converting sheet {sheet.title!r}: {total:,} rows so far")
    if chunk or first:
        _write(conn, table, _normalise_frame(pd.DataFrame(chunk, columns=columns)),
               first=first)
    return total, tuple(columns)


def excel_to_sqlite(data: bytes, dest: Path, *, label: str,
                    limits: Limits = DEFAULT_LIMITS,
                    progress: Progress | None = None) -> list[TableInfo]:
    """One table per non-empty sheet. Raises UploadError."""
    from openpyxl import load_workbook  # only needed for Excel uploads

    _say(progress, f"Opening {Path(label).name} ({len(data) / 1e6:.1f} MB)")
    _check_zip(data)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for a bad file
        raise UploadError("The file could not be read as an .xlsx workbook.") from exc
    try:
        sheets = list(workbook.worksheets)
        if len(sheets) > limits.max_tables:
            raise UploadError(f"The workbook has {len(sheets)} sheets; the limit is "
                              f"{limits.max_tables}.")
        names = unique_identifiers([s.title for s in sheets], fallback="sheet")
        tables: list[TableInfo] = []
        with sqlite3.connect(dest) as conn:
            for sheet, name in zip(sheets, names, strict=True):
                _say(progress, f"Converting sheet {sheet.title!r}")
                written = _sheet_to_sqlite(conn, sheet, name, limits, progress)
                if written is not None:
                    tables.append(TableInfo(name, f"{label} / {sheet.title}",
                                            written[0], written[1]))
    finally:
        workbook.close()
    if not tables:
        raise UploadError("The workbook has no sheet with data.")
    return tables
