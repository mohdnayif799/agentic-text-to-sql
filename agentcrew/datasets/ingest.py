"""One entry point for an upload: bytes from the browser to a stored dataset.

The order matters. Each file's kind comes from its extension and the sizes
are checked before anything parses them; the files are converted (or, for
SQLite, checked and copied) into a new folder; the result is opened only
through the agent's read-only engine to read its schema and propose joins;
only then is it registered, which is when the store's total limit is
enforced. Any failure deletes the folder, so a refused upload leaves nothing
on disk.

Several CSV files may be uploaded together: each becomes one table of the
same dataset (students.csv, marks.csv, ...), which is how related tables
arrive from a spreadsheet tool, and what join inference is for. An Excel
workbook (already one table per sheet) or a SQLite file comes alone.

Drafting descriptions needs a model call, so it is not done here: the
caller decides which key pays and charges the caps, then stores the notes.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentcrew.datasets.convert import (
    Limits,
    Progress,
    csv_to_sqlite,
    excel_to_sqlite,
)
from agentcrew.datasets.errors import UploadError
from agentcrew.datasets.joins import infer_joins
from agentcrew.datasets.names import unique_identifiers
from agentcrew.datasets.sqlite_upload import check_and_copy
from agentcrew.datasets.store import DB_NAME, Dataset, DatasetStore, StoreFull
from agentcrew.db.engine import read_only_engine
from agentcrew.db.introspect import build_catalog

KINDS = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".xlsx": "excel", ".xlsm": "excel",
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
}
#: Extensions for the upload widget.
EXTENSIONS = tuple(sorted({e.lstrip(".") for e in KINDS}))


def kind_of(filename: str) -> str:
    kind = KINDS.get(Path(filename).suffix.lower())
    if kind is None:
        raise UploadError("Upload CSV files (.csv), an Excel workbook (.xlsx) or a "
                          "SQLite database (.db, .sqlite). Older .xls files are "
                          "not supported; save them as .xlsx.")
    return kind


def size_limit(kind: str, settings: Any) -> int:
    mb = settings.max_excel_upload_mb if kind == "excel" else settings.max_upload_mb
    return int(mb * 1e6)


def label_of(names: Sequence[str]) -> str:
    first = Path(names[0]).name[:120]
    return first if len(names) == 1 else f"{first} and {len(names) - 1} more"


def _check(files: Sequence[tuple[str, bytes]], settings: Any) -> str:
    """The upload's kind, after the checks that need no parsing."""
    if not files:
        raise UploadError("Choose a file to upload.")
    kinds = {kind_of(name) for name, _ in files}
    if len(files) > 1 and kinds != {"csv"}:
        raise UploadError("Several files can be uploaded together only when all "
                          "are CSV. Upload an Excel workbook or a SQLite file on "
                          "its own.")
    kind = kinds.pop()
    if len(files) > settings.max_upload_tables:
        raise UploadError(f"Upload at most {settings.max_upload_tables} files at once.")
    limit = size_limit(kind, settings)
    total = sum(len(data) for _, data in files)
    if total > limit:
        what = "The files are" if len(files) > 1 else "The file is"
        raise UploadError(f"{what} {total / 1e6:.1f} MB; the limit for this kind "
                          f"of upload is {limit / 1e6:.0f} MB.")
    for name, data in files:
        if not data:
            raise UploadError(f"{Path(name).name} is empty.")
    return kind


def ingest(store: DatasetStore, *, owner: str, settings: Any,
           files: Sequence[tuple[str, bytes]] | None = None,
           filename: str | None = None, data: bytes | None = None,
           progress: Progress | None = None) -> Dataset:
    """Convert and register one upload: `files` as (name, bytes) pairs, or a
    single `filename` and `data`. `progress` receives a sentence per step
    (convert.Progress). Raises UploadError with a message that is safe to
    show."""

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    if files is None:
        files = [(filename or "", data or b"")]
    kind = _check(files, settings)
    limits = Limits(max_rows=settings.max_upload_rows,
                    max_columns=settings.max_upload_columns,
                    max_tables=settings.max_upload_tables)
    label = label_of([name for name, _ in files])
    dataset_id, folder = store.new_folder()
    dest = folder / DB_NAME
    try:
        if kind == "csv":
            tables = unique_identifiers([Path(n).stem for n, _ in files],
                                        fallback="data")
            for (name, content), table in zip(files, tables, strict=True):
                csv_to_sqlite(content, dest, label=name, limits=limits, table=table,
                              progress=progress)
        elif kind == "excel":
            excel_to_sqlite(files[0][1], dest, label=label, limits=limits,
                            progress=progress)
        else:
            say(f"Checking {label}")
            raw = folder / "upload.bin"
            raw.write_bytes(files[0][1])
            try:
                check_and_copy(raw, dest, max_bytes=size_limit(kind, settings),
                               max_tables=limits.max_tables)
            finally:
                raw.unlink(missing_ok=True)
        say("Reading the tables and looking for joins")
        engine = read_only_engine(dest, timeout=settings.sql_timeout_seconds)
        try:
            catalog = build_catalog(engine)
            joins = tuple(infer_joins(engine, catalog))
        finally:
            engine.dispose()
        dataset = Dataset(
            id=dataset_id, owner=owner, label=label, kind=kind, path=dest,
            size_bytes=dest.stat().st_size, tables=tuple(sorted(catalog.tables)),
            joins=joins)
    except UploadError:
        store.discard_folder(dataset_id)
        raise
    except Exception as exc:
        store.discard_folder(dataset_id)
        raise UploadError("The upload could not be converted.") from exc
    try:
        return store.add(dataset)
    except StoreFull as exc:
        raise UploadError("The demo is holding as much uploaded data as it can "
                          "right now. Try again in a few minutes.") from exc
