"""Adversarial safety audit.

These are the payloads an attacker (or a jailbroken model) would actually try.
Kept as a permanent regression suite because the safety guarantee is the one
property of this system whose failure has real consequences.

The test asserts on *outcomes*, not on which layer caught it: whichever of the
three layers stops the write, the row counts must not change.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from agentcrew.db.engine import read_only_engine
from agentcrew.tools import ToolContext, execute_readonly_sql

WRITE_ATTACKS = [
    "DROP TABLE customers",
    "DELETE FROM customers WHERE 1=1",
    "UPDATE customers SET segment='hacked'",
    "SELECT 1; DELETE FROM orders",
    "SELECT 1;\n-- comment\nDROP TABLE regions",
    "WITH t AS (DELETE FROM orders RETURNING 1) SELECT * FROM t",
    "PRAGMA writable_schema=ON",
    "ATTACH DATABASE '/tmp/pwn.db' AS pwn",
    "INSERT INTO regions VALUES (99,'X','Y')",
    "CREATE TRIGGER t AFTER INSERT ON orders BEGIN SELECT 1; END",
    "REPLACE INTO regions VALUES (1,'X','Y')",
    "SELECT load_extension('evil.so')",
    "VACUUM INTO '/tmp/copy.db'",
    "DELETE FROM customers RETURNING customer_id",
    "INSERT INTO regions SELECT * FROM regions",
]

GUARDED_TABLES = ("customers", "orders", "regions", "order_items", "products")


@pytest.fixture
def sandbox(db_path: Path, tmp_path: Path) -> Path:
    """A throwaway copy, so a hypothetical breach cannot damage the real DB."""
    target = tmp_path / "audit.db"
    shutil.copy(db_path, target)
    return target


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in GUARDED_TABLES
        }
    finally:
        conn.close()


@pytest.mark.parametrize("payload", WRITE_ATTACKS)
def test_write_attack_is_rejected(sandbox: Path, payload: str) -> None:
    ctx = ToolContext.create(read_only_engine(sandbox), max_rows=50)
    result = execute_readonly_sql(ctx, payload)
    assert not result.ok, f"payload was accepted: {payload}"
    assert result.error_message


def test_no_attack_changes_any_row_count(sandbox: Path) -> None:
    ctx = ToolContext.create(read_only_engine(sandbox), max_rows=50)
    before = counts(sandbox)
    for payload in WRITE_ATTACKS:
        execute_readonly_sql(ctx, payload)
    assert counts(sandbox) == before


def test_attach_does_not_create_a_file(sandbox: Path, tmp_path: Path) -> None:
    ctx = ToolContext.create(read_only_engine(sandbox), max_rows=50)
    target = tmp_path / "pwn.db"
    execute_readonly_sql(ctx, f"ATTACH DATABASE '{target}' AS pwn")
    assert not target.exists()


def test_sql_comment_containing_a_drop_is_harmless(sandbox: Path) -> None:
    """A DROP inside a comment is not a statement.

    This one is *correctly allowed*: sqlglot parses it as a plain SELECT with
    a comment attached. Asserted explicitly so nobody later "fixes" the guard
    into rejecting valid SQL that merely contains a scary substring - a
    regex-based guard would fail exactly here.
    """
    ctx = ToolContext.create(read_only_engine(sandbox), max_rows=50)
    before = counts(sandbox)
    result = execute_readonly_sql(
        ctx, "SELECT COUNT(*) FROM customers /* ; DROP TABLE customers; */"
    )
    assert result.ok
    assert counts(sandbox) == before


def test_connection_layer_holds_when_the_guard_is_bypassed(sandbox: Path) -> None:
    """Layer 1 proof: skip the AST guard entirely and go at the driver.

    If this ever passes, the read-only guarantee is gone regardless of what
    the guard does.
    """
    engine = read_only_engine(sandbox)
    with engine.connect() as conn, pytest.raises(OperationalError, match="readonly"):
        conn.execute(text("DELETE FROM customers"))


def test_write_mode_is_off_by_default() -> None:
    from agentcrew.config import Settings

    assert Settings().allow_write_mode is False
