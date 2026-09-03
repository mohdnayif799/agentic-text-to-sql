"""SQL safety guard.

Design note
-----------
Read-only behaviour is enforced at three independent layers, because any single
layer can be defeated:

1. **Connection layer** (`agentcrew/db/engine.py`) - SQLite is opened with the
   ``file:...?mode=ro`` URI and ``PRAGMA query_only=ON``. Even a perfect
   jailbreak cannot write, because the handle physically cannot.
2. **AST layer** (this module) - the statement is parsed with sqlglot and the
   whole tree is walked. Anything that is not a single read-only SELECT/CTE is
   rejected *before* it reaches the driver.
3. **Resource layer** (this module + engine) - a LIMIT is injected when the
   model omits one, and execution runs under a timeout.

Layer 1 is the real guarantee. Layer 2 exists to give the *agent* a clean,
explainable error it can learn from, and to keep write-mode (opt-in) honest.
We never rely on the model choosing to behave.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"

#: Expression types that mutate data or schema. Rejected unconditionally in
#: read-only mode. Kept explicit rather than "anything not SELECT" so that the
#: error message can name the offending construct.
_MUTATING = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
)

#: Statements sqlglot parses as opaque commands (PRAGMA, ATTACH, VACUUM, ...).
#: These can escape the sandbox (ATTACH can open a writable file), so the whole
#: category is denied.
_DENIED_COMMAND_PREFIXES = (
    "pragma",
    "attach",
    "detach",
    "vacuum",
    "reindex",
    "analyze",
    "replace",
)


class UnsafeSQLError(Exception):
    """Raised when a statement fails the guard."""


@dataclass(frozen=True)
class GuardResult:
    """Outcome of guarding a statement."""

    sql: str
    """The statement to actually execute (may have had a LIMIT injected)."""
    fingerprint: str
    limit_injected: bool
    referenced_tables: tuple[str, ...]


def fingerprint(sql: str) -> str:
    """Stable hash of a *normalised* statement.

    Used for loop detection: two attempts that differ only in whitespace,
    casing or alias naming produce the same fingerprint, so the orchestrator
    can tell "the model retried the identical query" from "the model tried
    something new". Falls back to raw-text hashing for unparseable SQL.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=DIALECT)
        canonical = tree.sql(dialect=DIALECT, normalize=True, pretty=False).lower()
    except Exception:
        canonical = " ".join(sql.lower().split())
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _is_read_only_root(tree: exp.Expression) -> bool:
    return isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect)) or (
        isinstance(tree, exp.Subquery) and _is_read_only_root(tree.this)
    )


def _has_aggregate_or_limit(tree: exp.Expression) -> bool:
    """True when injecting a LIMIT would be pointless or harmful."""
    if tree.find(exp.Limit):
        return True
    if tree.find(exp.Group):
        return False  # grouped queries can still be huge; cap them
    # A bare aggregate with no GROUP BY returns exactly one row.
    selects = tree.find(exp.Select)
    if selects is not None:
        exprs = selects.expressions
        if exprs and all(
            e.find(exp.AggFunc) is not None for e in exprs
        ):
            return True
    return False


def guard_sql(
    sql: str,
    *,
    max_rows: int,
    allow_write: bool = False,
) -> GuardResult:
    """Validate and normalise a statement.

    Raises:
        UnsafeSQLError: if the statement is empty, multi-statement, unparseable,
            or performs any operation other than reading.
    """
    if not sql or not sql.strip():
        raise UnsafeSQLError("Empty statement.")

    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:  # sqlglot raises several subclasses
        raise UnsafeSQLError(f"Could not parse SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if not statements:
        raise UnsafeSQLError("No parseable statement found.")
    if len(statements) > 1:
        raise UnsafeSQLError(
            f"Expected exactly one statement, found {len(statements)}. "
            "Statement batching is not permitted."
        )

    tree = statements[0]

    # Opaque commands (PRAGMA / ATTACH / ...) parse as exp.Command.
    if isinstance(tree, exp.Command):
        name = (tree.this or "").strip().lower()
        raise UnsafeSQLError(f"Statement type '{name or 'command'}' is not permitted.")

    if not allow_write:
        for node_type in _MUTATING:
            found = tree.find(node_type)
            if found is not None:
                raise UnsafeSQLError(
                    f"{node_type.__name__.upper()} is not permitted: this agent "
                    "is read-only. Rewrite the request as a SELECT."
                )
        # Catch anything mutating that slipped past the explicit list.
        if not _is_read_only_root(tree):
            raise UnsafeSQLError(
                f"Only SELECT statements are permitted (got "
                f"{type(tree).__name__.upper()})."
            )
        for cmd in tree.find_all(exp.Command):
            name = str(cmd.this or "").strip().lower()
            if name.startswith(_DENIED_COMMAND_PREFIXES):
                raise UnsafeSQLError(f"Embedded '{name}' command is not permitted.")

    tables = tuple(
        sorted(
            {
                t.name.lower()
                for t in tree.find_all(exp.Table)
                if t.name
            }
        )
    )

    limit_injected = False
    if not allow_write and not _has_aggregate_or_limit(tree):
        tree = tree.limit(max_rows)
        limit_injected = True

    final_sql = tree.sql(dialect=DIALECT, pretty=False)
    return GuardResult(
        sql=final_sql,
        fingerprint=fingerprint(final_sql),
        limit_injected=limit_injected,
        referenced_tables=tables,
    )
