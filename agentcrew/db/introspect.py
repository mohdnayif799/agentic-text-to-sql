"""Deterministic schema discovery.

Architectural note
------------------
The original design had a "Schema Explorer **agent**". Reading table names,
column types and foreign keys is a `sqlalchemy.inspect()` call - it is fully
determined by the database and an LLM cannot do it better, only slower and
less reliably. So introspection is a plain function.

What genuinely needs judgement is *which* tables to put in front of the model,
and that lives in `selector.py`. Splitting the two is the difference between
an agent that discovers the database and an agent that pretends to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import Engine, inspect, text


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    primary_key: bool


@dataclass(frozen=True)
class ForeignKey:
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    row_count: int = 0

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Catalog:
    """Full, cheap, deterministic picture of the database."""

    tables: dict[str, Table] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def neighbours(self, table: str) -> set[str]:
        """Tables reachable from `table` in one foreign-key hop (either way)."""
        out: set[str] = set()
        for fk in self.foreign_keys:
            if fk.from_table == table:
                out.add(fk.to_table)
            elif fk.to_table == table:
                out.add(fk.from_table)
        return out

    def join_hints(self, tables: set[str]) -> list[str]:
        """Human-readable join conditions among the given tables."""
        hints = []
        for fk in self.foreign_keys:
            if fk.from_table in tables and fk.to_table in tables:
                pairs = " AND ".join(
                    f"{fk.from_table}.{a} = {fk.to_table}.{b}"
                    for a, b in zip(fk.from_columns, fk.to_columns, strict=False)
                )
                hints.append(pairs)
        return hints


def build_catalog(engine: Engine) -> Catalog:
    """Introspect the whole database. Cheap enough to do on every run."""
    insp = inspect(engine)
    catalog = Catalog()

    for name in sorted(insp.get_table_names()):
        if name.startswith("sqlite_"):
            continue
        cols = [
            Column(
                name=c["name"],
                type=str(c["type"]),
                nullable=bool(c.get("nullable", True)),
                primary_key=bool(c.get("primary_key", False)),
            )
            for c in insp.get_columns(name)
        ]
        catalog.tables[name] = Table(name=name, columns=cols)

        for fk in insp.get_foreign_keys(name):
            referred = fk.get("referred_table")
            if not referred:
                continue
            catalog.foreign_keys.append(
                ForeignKey(
                    from_table=name,
                    from_columns=tuple(fk.get("constrained_columns") or ()),
                    to_table=referred,
                    to_columns=tuple(fk.get("referred_columns") or ()),
                )
            )

    with engine.connect() as conn:
        for name, table in catalog.tables.items():
            try:
                # Table names come from introspection, not user input.
                stmt = text(f'SELECT count(*) FROM "{name}"')  # noqa: S608
                table.row_count = int(conn.execute(stmt).scalar() or 0)
            except Exception:
                table.row_count = -1

    return catalog


@lru_cache(maxsize=8)
def cached_catalog(engine_key: str, engine: Engine) -> Catalog:  # pragma: no cover
    """Catalog cache keyed by database path."""
    return build_catalog(engine)


def sample_values(
    engine: Engine,
    table: str,
    column: str,
    *,
    limit: int = 3,
    max_distinct: int = 40,
) -> list[str]:
    """Representative values for a low-cardinality text column.

    Skips high-cardinality columns: showing three of 50,000 customer names
    burns context and teaches the model nothing. Showing all six status values
    prevents it from inventing `status = 'completed'` when the data says
    `'complete'` - a very common and very annoying failure.
    """
    # noqa justification: table/column names come from SQLAlchemy introspection
    # of this database, never from user input. They cannot carry injection.
    q = text(f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"')  # noqa: S608
    try:
        with engine.connect() as conn:
            distinct = int(conn.execute(q).scalar() or 0)
            if distinct == 0 or distinct > max_distinct:
                return []
            rows = conn.execute(
                text(
                    f'SELECT DISTINCT "{column}" FROM "{table}" '  # noqa: S608
                    f'WHERE "{column}" IS NOT NULL LIMIT {int(limit)}'
                )
            ).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []
