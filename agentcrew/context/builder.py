"""Assemble the shared context for one question.

Everything that does not depend on the question (schema, value lists, date
ranges, dictionary, example index) is computed once in `ContextBuilder`; per
question only the example retrieval runs. The full schema of every table is
always included, so no SQL step ever sees less schema than the baseline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from agentcrew.context.dictionary import (
    DICTIONARY_PATH,
    load_dictionary,
    render_dictionary,
)
from agentcrew.context.examples import (
    EXAMPLES_PATH,
    Example,
    ExampleIndex,
    load_examples,
)
from agentcrew.context.values import MAX_DISTINCT, Profile, profile_columns
from agentcrew.db.introspect import Catalog
from agentcrew.db.selector import render_schema_card, select_tables

if TYPE_CHECKING:
    from agentcrew.datasets.describe import Notes
    from agentcrew.datasets.joins import Join

EXAMPLES_PER_QUESTION = 3


def render_notes(notes: Notes, joins: Sequence[Join], catalog: Catalog,
                 tables: Sequence[str] | None = None) -> str:
    # Imported here: datasets/ imports the tools layer, which context/ must
    # not pull in for NorthStar's path.
    from agentcrew.datasets.describe import render_dictionary as render

    return render(notes, joins, catalog, tables)


@dataclass(frozen=True)
class SharedContext:
    schema: str
    values: str
    dates: str
    dictionary: str
    examples: tuple[Example, ...]
    today: date

    def render(self) -> str:
        parts = [
            "## Database schema (SQLite)\n" + self.schema,
            "## Allowed values of low-cardinality text columns\n" + self.values,
            "## Date columns: format and range of the data\n" + self.dates,
            "## Data dictionary\n" + self.dictionary,
        ]
        if self.examples:
            parts.append(
                "## Verified examples of similar questions\n" + "\n\n".join(
                    f"Q: {e.question}\nSQL: {e.sql}" for e in self.examples
                )
            )
        parts.append(f"## Today's date\n{self.today.isoformat()}")
        return "\n\n".join(parts)


class ContextBuilder:
    """Built once per process; `build(question)` is cheap."""

    def __init__(
        self,
        *,
        schema: str,
        values: str,
        dates: str,
        dictionary: str,
        examples: ExampleIndex,
        today: date,
    ) -> None:
        self._static = (schema, values, dates, dictionary)
        self._examples = examples
        self.today = today

    @classmethod
    def from_database(
        cls,
        engine: Engine,
        catalog: Catalog,
        *,
        today: date,
        dictionary_path: Path = DICTIONARY_PATH,
        examples_path: Path = EXAMPLES_PATH,
        max_distinct: int = MAX_DISTINCT,
    ) -> ContextBuilder:
        profile = profile_columns(engine, catalog, max_distinct=max_distinct)
        return cls(
            schema=render_schema_card(catalog, sorted(catalog.tables)),
            values=profile.render_values(),
            dates=profile.render_dates(),
            dictionary=render_dictionary(load_dictionary(dictionary_path)),
            examples=ExampleIndex(load_examples(examples_path)),
            today=today,
        )

    def build(self, question: str, k: int = EXAMPLES_PER_QUESTION) -> SharedContext:
        schema, values, dates, dictionary = self._static
        return SharedContext(
            schema=schema,
            values=values,
            dates=dates,
            dictionary=dictionary,
            examples=self._examples.top_k(question, k),
            today=self.today,
        )

    @classmethod
    def from_source(
        cls,
        engine: Engine,
        catalog: Catalog,
        *,
        today: date,
        notes: Notes,
        joins: Sequence[Join],
        max_tables: int,
        max_distinct: int = MAX_DISTINCT,
    ) -> ContextBuilder:
        """The shared context for a database that is not NorthStar: an upload
        or AGENTCREW_DATABASE_URL. Same sections, same rendering; what differs:

        * the data dictionary is the dataset's descriptions and confirmed joins
          (datasets/describe.py), not NorthStar's YAML;
        * there are no verified examples: NorthStar's would only mislead;
        * with more than `max_tables` tables, each question gets the tables
          schema selection picks (names, descriptions, join closure), with
          their values and dictionary entries, instead of the whole schema.
        """
        profile = profile_columns(engine, catalog, max_distinct=max_distinct)
        if len(catalog.tables) <= max_tables:
            return cls(
                schema=render_schema_card(catalog, sorted(catalog.tables)),
                values=profile.render_values(),
                dates=profile.render_dates(),
                dictionary=render_notes(notes, joins, catalog),
                examples=ExampleIndex(()),
                today=today,
            )
        return SelectingContextBuilder(catalog=catalog, profile=profile,
                                       notes=notes, joins=tuple(joins),
                                       max_tables=max_tables, today=today)


class SelectingContextBuilder(ContextBuilder):
    """Per-question schema for a database too large to show whole."""

    def __init__(self, *, catalog: Catalog, profile: Profile, notes: Notes,
                 joins: tuple[Join, ...], max_tables: int, today: date) -> None:
        super().__init__(schema="", values="", dates="", dictionary="",
                         examples=ExampleIndex(()), today=today)
        self._catalog = catalog
        self._profile = profile
        self._notes = notes
        self._joins = joins
        self._max_tables = max_tables
        self._table_notes = {t: notes.table_text(t) for t in catalog.tables}

    def tables_for(self, question: str) -> list[str]:
        return select_tables(question, self._catalog, max_tables=self._max_tables,
                             notes=self._table_notes).tables

    def build(self, question: str, k: int = EXAMPLES_PER_QUESTION) -> SharedContext:
        tables = self.tables_for(question)
        chosen = set(tables)
        profile = Profile(
            values=tuple(v for v in self._profile.values if v.table in chosen),
            dates=tuple(d for d in self._profile.dates if d.table in chosen),
        )
        return SharedContext(
            schema=render_schema_card(self._catalog, tables),
            values=profile.render_values(),
            dates=profile.render_dates(),
            dictionary=render_notes(self._notes, self._joins, self._catalog,
                                    tables=tables),
            examples=(),
            today=self.today,
        )
