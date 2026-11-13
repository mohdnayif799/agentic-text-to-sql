"""Table and column descriptions: drafted once by the model, edited by people.

For NorthStar the shared context carries a hand-written data dictionary
(context/data_dictionary.yaml). Any other database has none, and its column
names may say little (``c3``, ``amt2``, ``flg``). So when a dataset arrives
the model drafts one short description per table and per column, in one
structured call per batch of tables; the person who uploaded it can then
correct any of them. The result does two jobs:

* it becomes the data dictionary section of the shared context, beside the
  schema, so every SQL-writing call reads it;
* schema selection (db/selector.py) scores a table by the words of its
  descriptions as well as its names, so a question can find the right
  table when the column names are opaque.

What the drafting call is sent: the schema card (table and column names,
types, row counts, up to three distinct values of low-cardinality columns)
and up to three rows per table, each value cut to 40 characters. The app's
privacy note lists exactly this. Nothing else from the data is sent.

A draft is text from a model about text from a file. It is treated as data:
cut to a fixed length, stripped of newlines, and never executed.

What one row is
---------------
Each table also gets one line saying what a single row of it means ("One row
per student per course"). NorthStar's hand-written dictionary has this for
every table ("One row per order"); a drafted one did not, and the unseen-
schema test showed what that costs: given a summary table with one row per
student per course, the model answered a per-student question at that
table's grain (eval/results/college/college2.json, cm04), silently and on
both architectures. The row line is drafted with the descriptions, shown in
the same editor, and printed on the table's line of the data dictionary, so
every SQL-writing call reads it next to the table it describes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agentcrew.datasets.joins import Join
from agentcrew.db.introspect import Catalog
from agentcrew.db.selector import render_schema_card
from agentcrew.llm import LLMClient
from agentcrew.tools import ToolContext, get_sample_rows

MAX_DESCRIPTION = 200
TABLES_PER_CALL = 12
SAMPLE_ROWS = 3
SAMPLE_CELL = 40

DESCRIBE_SYSTEM = """\
You document a database for analysts who will ask questions about it. For \
every table and every column listed, write one short plain description (at \
most 20 words) of what it holds, based only on the names, types, values and \
sample rows shown. Say what a code or flag means only when the values make \
it clear; otherwise describe it as a code. Do not invent facts, units or \
meanings the data does not show. Say if a column looks like an identifier \
that refers to another table, and which one.

For every table also fill in `row`: what ONE row of the table is, written as \
"One row per ..." and naming every thing that together identifies a row, for \
example "One row per order", "One row per student per course" or "One row \
per region per month". Work it out from the row count and the columns that \
identify a row, not from the table's name. If the table is a summary of \
another table, say so in its description.
"""

DESCRIBE_USER = """\
## Tables
{schema}

## Sample rows
{samples}
"""


class ColumnNote(BaseModel):
    name: str
    description: str = Field(default="", description="At most 20 words.")


class TableNote(BaseModel):
    name: str
    description: str = Field(default="", description="At most 20 words.")
    row: str = Field(default="", description="What one row is: 'One row per ...'.")
    columns: list[ColumnNote] = Field(default_factory=list)


class DraftNotes(BaseModel):
    """The model's reply for one batch of tables."""

    tables: list[TableNote] = Field(default_factory=list)


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())[:MAX_DESCRIPTION]


#: The "column" of the editor row that holds a table's row line. Not a
#: possible identifier from a CSV or Excel upload (names.to_identifier).
ROW_LABEL = "(one row is)"


def _row_line(text: object) -> str:
    """A table's row line as one sentence starting "One row" (or "Each row"),
    whatever shape the draft or the editor gave it."""
    line = _clean(text).rstrip(".")
    if not line:
        return ""
    if not line.lower().startswith(("one row", "each row")):
        line = f"One row per {line[0].lower()}{line[1:]}"
    return line + "."


@dataclass(frozen=True)
class Notes:
    """Descriptions by table and by (table, column). Immutable: an edit makes
    a new Notes (with_rows)."""

    tables: Mapping[str, str] = field(default_factory=dict)
    columns: Mapping[tuple[str, str], str] = field(default_factory=dict)
    grains: Mapping[str, str] = field(default_factory=dict)
    """Per table, what one row of it is: "One row per student per course."."""

    def table_text(self, table: str) -> str:
        """Every description of `table` and its columns, for selection."""
        parts = [self.tables.get(table, ""), self.grains.get(table, "")]
        parts += [d for (t, _), d in self.columns.items() if t == table]
        return " ".join(p for p in parts if p)

    def rows(self, catalog: Catalog) -> list[dict[str, str]]:
        """One row per table, per table's row line and per column, in catalog
        order, for editing. A table's own row has an empty column; its row
        line has ROW_LABEL there."""
        out = []
        for name in sorted(catalog.tables):
            out.append({"table": name, "column": "",
                        "description": self.tables.get(name, "")})
            out.append({"table": name, "column": ROW_LABEL,
                        "description": self.grains.get(name, "")})
            for col in catalog.tables[name].columns:
                out.append({"table": name, "column": col.name,
                            "description": self.columns.get((name, col.name), "")})
        return out

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], catalog: Catalog) -> Notes:
        """Back from the editor. Rows naming an unknown table or column are
        dropped, so an edited table cannot add schema."""
        tables: dict[str, str] = {}
        columns: dict[tuple[str, str], str] = {}
        grains: dict[str, str] = {}
        for row in rows:
            table = str(row.get("table") or "")
            column = str(row.get("column") or "")
            text = _clean(row.get("description"))
            if table not in catalog.tables or not text:
                continue
            if not column:
                tables[table] = text
            elif column in catalog.tables[table].column_names:
                columns[(table, column)] = text
            elif column == ROW_LABEL:
                grains[table] = _row_line(text)
        return cls(tables, columns, grains)

    def to_yaml(self) -> str:
        data: dict[str, Any] = {}
        for table, text in sorted(self.tables.items()):
            data.setdefault(table, {"description": "", "columns": {}})
            data[table]["description"] = text
        for (table, column), text in sorted(self.columns.items()):
            data.setdefault(table, {"description": "", "columns": {}})
            data[table]["columns"][column] = text
        for table, text in sorted(self.grains.items()):
            data.setdefault(table, {"description": "", "columns": {}})
            data[table]["row"] = text
        return yaml.safe_dump({"tables": data}, sort_keys=True, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str, catalog: Catalog) -> Notes:
        data = (yaml.safe_load(text) or {}).get("tables") or {}
        rows: list[dict[str, Any]] = []
        for table, spec in data.items():
            spec = spec or {}
            rows.append({"table": table, "column": "",
                         "description": spec.get("description", "")})
            rows.append({"table": table, "column": ROW_LABEL,
                         "description": spec.get("row", "")})
            for column, text_ in (spec.get("columns") or {}).items():
                rows.append({"table": table, "column": column, "description": text_})
        return cls.from_rows(rows, catalog)


def load_notes(path: Path, catalog: Catalog) -> Notes | None:
    """Saved descriptions (self-hosted), or None if the file does not exist."""
    if not path.exists():
        return None
    return Notes.from_yaml(path.read_text("utf-8"), catalog)


def save_notes(path: Path, notes: Notes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notes.to_yaml(), encoding="utf-8")


def _samples(tools: ToolContext, tables: list[str]) -> str:
    blocks = []
    for name in tables:
        sample = get_sample_rows(tools, name, limit=SAMPLE_ROWS)
        if "error" in sample:
            continue
        lines = [" | ".join(sample["columns"])]
        lines += [" | ".join("NULL" if v is None else str(v)[:SAMPLE_CELL]
                             for v in row) for row in sample["rows"]]
        blocks.append(f"{name}:\n" + "\n".join(lines))
    return "\n\n".join(blocks) or "(none)"


def draft_notes(llm: LLMClient, tools: ToolContext, *, max_tokens: int = 2048,
                temperature: float = 0.0) -> Notes:
    """Ask the model for descriptions of every table, TABLES_PER_CALL tables a
    call. A batch whose reply is invalid is skipped (its descriptions stay
    empty for the person to fill in) rather than failing the upload."""
    from agentcrew.llm import StructuredOutputError

    catalog = tools.catalog
    names = sorted(catalog.tables)
    tables: dict[str, str] = {}
    columns: dict[tuple[str, str], str] = {}
    grains: dict[str, str] = {}
    for start in range(0, len(names), TABLES_PER_CALL):
        batch = names[start:start + TABLES_PER_CALL]
        user = DESCRIBE_USER.format(
            schema=render_schema_card(catalog, batch, engine=tools.engine),
            samples=_samples(tools, batch))
        try:
            draft, _ = llm.complete_structured(
                system=DESCRIBE_SYSTEM, user=user, schema=DraftNotes,
                max_tokens=max_tokens, temperature=temperature)
        except StructuredOutputError:
            continue
        for note in draft.tables:
            if note.name not in batch:
                continue  # a table the model made up
            if note.description:
                tables[note.name] = _clean(note.description)
            if note.row:
                grains[note.name] = _row_line(note.row)
            known = set(catalog.tables[note.name].column_names)
            for col in note.columns:
                if col.name in known and col.description:
                    columns[(note.name, col.name)] = _clean(col.description)
    return Notes(tables, columns, grains)


def render_dictionary(notes: Notes, joins: Iterable[Join], catalog: Catalog,
                      tables: Iterable[str] | None = None) -> str:
    """The data dictionary section for a dataset, in the shape NorthStar's
    has (context/dictionary.render_dictionary), for `tables` (default all)."""
    chosen = sorted(tables) if tables is not None else sorted(catalog.tables)
    lines = ["Conventions:",
             "- Dates are TEXT in 'YYYY-MM-DD' (or 'YYYY-MM-DD HH:MM:SS') format "
             "unless the schema shows another type.",
             "- Text values are case-sensitive; use the allowed values exactly "
             "as written.",
             "- Descriptions were drafted by a model and may have been edited "
             "by the data's owner; the schema and values are authoritative.",
             "- Each table's line says what one row of it is. When a question "
             "is about one kind of thing and a table has several rows for each, "
             "group by that thing alone before filtering or comparing on it.",
             "", "Tables:"]
    for name in chosen:
        described = notes.tables.get(name) or "(no description)"
        if not described.endswith((".", ")")):
            described += "."
        grain = notes.grains.get(name)
        lines.append(f"{name}: {described}" + (f" {grain}" if grain else ""))
        for col in catalog.tables[name].columns:
            text = notes.columns.get((name, col.name))
            if text:
                lines.append(f"  {col.name}: {text}")
    picked = set(chosen)
    joined = [j for j in joins if j.from_table in picked and j.to_table in picked]
    lines += ["", "Join paths:"]
    lines += [f"- {j.sql()}" for j in joined] or ["- (none known)"]
    return "\n".join(lines)


def empty_notes() -> Notes:
    return Notes({}, {}, {})


