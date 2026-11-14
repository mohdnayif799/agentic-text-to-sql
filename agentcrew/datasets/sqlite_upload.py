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
   or too many. What is virtual is read from ``PRAGMA table_list``, SQLite's
   own classification of each object. It is NOT read from the text of the
   CREATE statement in sqlite_master: that text is under the uploader's
   control (a comment between CREATE and VIRTUAL defeats a prefix match),
   while table_list reports what SQLite's parser actually built.
5. Every table and column name must be one the app can show and quote:
   no quote characters, no control characters, a sane length. Internal
   queries quote names through the dialect (introspect.quote_name), which
   is the real defence; this check keeps names that only exist to break
   out of quoting, or to smuggle text into a prompt, out of the store.
6. It is copied with SQLite's backup API into a new file, which is then set
   to the plain rollback journal. The agent reads the copy; the visitor's
   bytes are deleted. A copy written by SQLite itself cannot carry tricks in
   unused pages or a write-ahead log the original may have had.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agentcrew.datasets.errors import UploadError
from agentcrew.db.engine import harden_sqlite

HEADER = b"SQLite format 3\x00"
MAX_NAME_LENGTH = 128
#: Characters no table or column name may contain. Double quote and backtick
#: are the identifier quotes of SQLite, PostgreSQL and MySQL.
FORBIDDEN_IN_NAMES = frozenset('"`')


def name_problem(name: str) -> str | None:
    """Why a table or column name is refused, or None if it is acceptable."""
    if not name or not name.strip():
        return "is empty"
    if len(name) > MAX_NAME_LENGTH:
        return f"is longer than {MAX_NAME_LENGTH} characters"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "contains a control character"
    if FORBIDDEN_IN_NAMES & set(name):
        return "contains a quote character"
    return None


def _quoted(name: str) -> str:
    """For the PRAGMA calls below, before the name has been checked."""
    return '"' + name.replace('"', '""') + '"'


def _objects(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(name, type, column count) of every object in the main schema, as
    SQLite itself classifies them: table, view, virtual or shadow."""
    return [(row[1], row[2], row[3])
            for row in conn.execute("PRAGMA table_list").fetchall()
            if row[0] == "main" and not str(row[1]).startswith("sqlite_")]


def _check_names(conn: sqlite3.Connection, tables: list[str],
                 max_columns: int) -> None:
    for table in tables:
        problem = name_problem(table)
        if problem:
            raise UploadError(f"A table name {problem}, so the file was refused.")
        columns = conn.execute(f"PRAGMA table_xinfo({_quoted(table)})").fetchall()
        if len(columns) > max_columns:
            raise UploadError(f"Table {table} has {len(columns)} columns; the "
                              f"limit is {max_columns}.")
        for column in columns:
            problem = name_problem(str(column[1]))
            if problem:
                raise UploadError(f"A column name in table {table} {problem}, so "
                                  "the file was refused.")


def check_and_copy(src: Path, dest: Path, *, max_bytes: int,
                   max_tables: int = 30, max_columns: int = 200) -> list[str]:
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
        objects = _objects(source)
        virtual = sorted(name for name, kind, _ in objects
                         if kind in ("virtual", "shadow"))
        if virtual:
            shown = ", ".join(n for n in virtual[:5] if name_problem(n) is None)
            raise UploadError("The file has virtual tables, which are not "
                              f"accepted{': ' + shown if shown else ''}.")
        tables = sorted(name for name, kind, _ in objects if kind == "table")
        if not tables:
            raise UploadError("The SQLite file has no tables.")
        if len(tables) > max_tables:
            raise UploadError(f"The file has {len(tables)} tables; the limit is "
                              f"{max_tables}.")
        _check_names(source, tables, max_columns)
        with closing(sqlite3.connect(dest)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
    except UploadError:
        dest.unlink(missing_ok=True)
        raise
    except sqlite3.Error as exc:
        dest.unlink(missing_ok=True)
        raise UploadError("The SQLite file could not be read.") from exc
    finally:
        source.close()
    return tables
