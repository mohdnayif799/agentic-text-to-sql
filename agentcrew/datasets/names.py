"""Safe SQL identifiers for tables and columns that came from a file.

A CSV header or an Excel sheet name can hold anything: spaces, quotes,
semicolons, reserved words, duplicates, nothing at all. The model writes SQL
against these names, and the tools quote names in a few internal queries, so
every name is reduced to lower-case letters, digits and underscores, never
starts with a digit, is not a reserved word, and is unique within its scope.
The original label is kept so descriptions can mention it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MAX_LENGTH = 60

#: Words that would need quoting in SQLite or PostgreSQL. A column named
#: "order" or "group" is legal when quoted, but a model that forgets the quotes
#: gets a syntax error, so they get a suffix instead.
RESERVED = frozenset("""
    abort action add after all alter analyze and as asc attach autoincrement
    before begin between by cascade case cast check collate column commit
    conflict constraint create cross current current_date current_time
    current_timestamp database default deferrable deferred delete desc detach
    distinct do drop each else end escape except exclusive exists explain
    fail filter following for foreign from full glob group having if ignore
    immediate in index indexed initially inner insert instead intersect into
    is isnull join key left like limit match natural no not nothing notnull
    null of offset on or order outer over partition plan pragma preceding
    primary query raise range recursive references regexp reindex release
    rename replace restrict right rollback row rows savepoint select set
    table temp temporary then to transaction trigger unbounded union unique
    update user using vacuum values view virtual when where window with
    without analyse array asymmetric both leading only placing returning
    symmetric trailing verbose
""".split())


def to_identifier(label: object, *, fallback: str = "column") -> str:
    """One safe identifier for `label` (not yet made unique)."""
    text = unicodedata.normalize("NFKD", str(label if label is not None else ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"{fallback}_{text}"
    text = text[:MAX_LENGTH].rstrip("_") or fallback
    if text in RESERVED:
        text = f"{text}_"
    return text


def unique_identifiers(labels: Iterable[object], *, fallback: str = "column"
                       ) -> list[str]:
    """Identifiers for `labels` in order, made unique by a numeric suffix.
    Blank labels become `fallback_1`, `fallback_2`, ... by position."""
    out: list[str] = []
    seen: set[str] = set()
    for position, label in enumerate(labels, start=1):
        blank = label is None or str(label).strip() == "" or str(label).startswith(
            "Unnamed:")
        base = (f"{fallback}_{position}" if blank
                else to_identifier(label, fallback=fallback))
        name, n = base, 2
        while name in seen:
            suffix = f"_{n}"
            name = base[: MAX_LENGTH - len(suffix)] + suffix
            n += 1
        seen.add(name)
        out.append(name)
    return out
