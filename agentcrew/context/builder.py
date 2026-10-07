"""Assemble the shared context for one question.

Everything that does not depend on the question (schema, value lists, date
ranges, dictionary, example index) is computed once in `ContextBuilder`; per
question only the example retrieval runs. The full schema of every table is
always included, so no SQL step ever sees less schema than the baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

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
from agentcrew.context.values import MAX_DISTINCT, profile_columns
from agentcrew.db.introspect import Catalog
from agentcrew.db.selector import render_schema_card

EXAMPLES_PER_QUESTION = 3


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
