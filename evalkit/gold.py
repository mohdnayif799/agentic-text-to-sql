"""Gold result files and the database fingerprint they were built against."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def database_fingerprint(conn: sqlite3.Connection) -> dict[str, str]:
    """SHA-256 of every user table's full contents, in rowid order.

    Content rather than file bytes, so a database rebuilt from the same seed
    by a different SQLite version still matches.
    """
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    prints = {}
    for name in names:
        digest = hashlib.sha256()
        # Name comes from sqlite_master of this database, not from input.
        for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):  # noqa: S608
            digest.update(repr(tuple(row)).encode())
        prints[name] = digest.hexdigest()
    return prints


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_gold(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))
