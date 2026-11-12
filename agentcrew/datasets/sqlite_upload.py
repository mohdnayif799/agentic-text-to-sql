"""Check an uploaded SQLite file, then copy it into the store.

An uploaded database is untrusted input, not data we produced. Before the
agent sees it:

1. Its size is within the limit and its first 16 bytes are SQLite's header,
   so a renamed file of another kind is refused without being parsed.
2. It is opened read-only (``mode=ro``) with the agent's own hardening
   (db/engine.harden_sqlite): defensive mode, ``trusted_schema=OFF``,
   ``query_only``. Views and triggers in it may then call only functions
   SQLite marks innocuous.
3. ``PRAGMA integrity_check`` must report ok: a damaged file can make the
   library misbehave in ways a read-only flag does not prevent.
4. Virtual tables are refused (they run module code, such as full-text
   search or a CSV reader, when queried), and so is a file with no tables
   or too many.
5. It is copied with SQLite's backup API into a new file, which is then set
   to the plain rollback journal. The agent reads the copy; the visitor's
   bytes are deleted. A copy written by SQLite itself cannot carry tricks in
   unused pages or a write-ahead log the original may have had.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentcrew.datasets.errors import UploadError
from agentcrew.db.engine import harden_sqlite

HEADER = b"SQLite format 3\x00"


def _tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()


def check_and_copy(src: Path, dest: Path, *, max_bytes: int,
                   max_tables: int = 30) -> list[str]:
    """Validate `src` and copy it to `dest`. Returns the table names.
    Raises UploadError; `dest` does not exist after a failure."""
    size = src.stat().st_size
    if size > max_bytes:
        raise UploadError(f"The file is larger than {max_bytes / 1e6:.0f} MB.")
    with src.open("rb") as fh:
        if fh.read(len(HEADER)) != HEADER:
            raise UploadError("The file is not a SQLite database.")

    try:
        source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise UploadError("The SQLite file could not be opened.") from exc
    try:
        harden_sqlite(source)
        try:
            check = source.execute("PRAGMA integrity_check(1)").fetchone()
        except sqlite3.DatabaseError as exc:
            raise UploadError("The SQLite file is damaged.") from exc
        if not check or check[0] != "ok":
            raise UploadError("The SQLite file failed its integrity check.")
        tables = _tables(source)
        virtual = [n for n, sql in tables if sql.lstrip().upper().startswith(
            "CREATE VIRTUAL TABLE")]
        if virtual:
            raise UploadError("The file has virtual tables, which are not "
                              f"accepted: {', '.join(virtual[:5])}.")
        if not tables:
            raise UploadError("The SQLite file has no tables.")
        if len(tables) > max_tables:
            raise UploadError(f"The file has {len(tables)} tables; the limit is "
                              f"{max_tables}.")
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
        finally:
            target.close()
    except UploadError:
        dest.unlink(missing_ok=True)
        raise
    except sqlite3.Error as exc:
        dest.unlink(missing_ok=True)
        raise UploadError("The SQLite file could not be read.") from exc
    finally:
        source.close()
    return [name for name, _ in tables]
