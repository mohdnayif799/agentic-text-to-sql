"""Schema selection: choose what the model sees.

Why this is not an LLM call
---------------------------
Dumping a whole schema into the prompt is the default in most text-to-SQL
demos. It works on a 7-table toy database and falls apart on a real one -
context cost grows linearly and precision drops as the model picks plausible
but wrong tables.

The selection itself is a ranking problem over a small candidate set, and a
lexical scorer plus foreign-key closure solves it deterministically, for free,
in microseconds. Spending an LLM call here would add latency, cost and a new
failure mode in exchange for nothing.

Selection strategy
------------------
1. Score every table by token overlap between the question and the table name
   (weighted heavily) and its column names (weighted lightly).
2. Keep tables that score above zero, plus the highest-scoring table always.
3. Expand by one foreign-key hop, because joins are the whole point - a
   question about "revenue by region" mentions neither `orders` nor
   `order_items`, but you cannot answer it without them.
4. Force-include any table named in a previous execution error, so a repair
   attempt can actually see the object it got wrong.
5. Cap the result, preferring high scorers and hub tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Engine

from agentcrew.db.introspect import Catalog, Column, sample_values

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Words that carry no signal about which table is relevant.
_STOPWORDS = frozenset(
    """
    a an and are as at be by did do does for from had has have how i in is it
    its me my of on or our show tell that the their there they this to us was
    we were what when where which who why will with you your give list find
    most least top bottom many much more less than compare between during over
    each per across total number count average avg sum
    """.split()
)

# Light-touch morphology: enough to match "customers" to "customer"
# without pulling in a stemming dependency.
def _normalise(token: str) -> str:
    for suffix in ("ies", "es", "s"):
        if len(token) > 3 and token.endswith(suffix):
            if suffix == "ies":
                return token[:-3] + "y"
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> set[str]:
    """Content tokens from a question or an identifier."""
    raw = _TOKEN_RE.findall(text.lower().replace("_", " "))
    return {_normalise(t) for t in raw if t not in _STOPWORDS and len(t) > 1}


@dataclass
class Selection:
    tables: list[str]
    scores: dict[str, float]
    seeded_by: list[str]
    expanded_by_fk: list[str]
    forced: list[str]


TABLE_NAME_WEIGHT = 3.0
COLUMN_NAME_WEIGHT = 1.0
HUB_BONUS = 0.25


def score_tables(question: str, catalog: Catalog) -> dict[str, float]:
    """Lexical relevance of each table to the question."""
    q_tokens = tokenize(question)
    scores: dict[str, float] = {}
    for name, table in catalog.tables.items():
        name_tokens = tokenize(name)
        col_tokens: set[str] = set()
        for col in table.columns:
            col_tokens |= tokenize(col.name)

        score = TABLE_NAME_WEIGHT * len(q_tokens & name_tokens)
        score += COLUMN_NAME_WEIGHT * len(q_tokens & col_tokens)
        if score > 0:
            score += HUB_BONUS * len(catalog.neighbours(name))
        scores[name] = score
    return scores


def select_tables(
    question: str,
    catalog: Catalog,
    *,
    max_tables: int = 8,
    force_include: set[str] | None = None,
) -> Selection:
    """Pick the tables to show the model."""
    force_include = {t.lower() for t in (force_include or set())}
    scores = score_tables(question, catalog)

    seeds = sorted(
        (t for t, s in scores.items() if s > 0),
        key=lambda t: (-scores[t], t),
    )
    if not seeds and catalog.tables:
        # Nothing matched lexically: fall back to the largest hub tables rather
        # than returning an empty schema, which would guarantee failure.
        seeds = sorted(
            catalog.tables,
            key=lambda t: (
                -len(catalog.neighbours(t)),
                -catalog.tables[t].row_count,
                t,
            ),
        )[:2]

    chosen: list[str] = []
    for t in seeds:
        if t not in chosen:
            chosen.append(t)

    expanded: list[str] = []
    for seed in list(chosen):
        for neighbour in sorted(catalog.neighbours(seed)):
            if neighbour not in chosen and neighbour in catalog.tables:
                chosen.append(neighbour)
                expanded.append(neighbour)

    forced: list[str] = []
    for t in sorted(force_include):
        if t in catalog.tables and t not in chosen:
            chosen.append(t)
            forced.append(t)

    if len(chosen) > max_tables:
        keep = set(forced)
        ranked = sorted(chosen, key=lambda t: (-scores.get(t, 0.0), t))
        for t in ranked:
            if len(keep) >= max_tables:
                break
            keep.add(t)
        chosen = [t for t in ranked if t in keep][:max_tables]

    return Selection(
        tables=chosen,
        scores=scores,
        seeded_by=seeds,
        expanded_by_fk=[t for t in expanded if t in chosen],
        forced=[t for t in forced if t in chosen],
    )


_TEXTUAL = ("char", "text", "clob", "string")
_NUMERIC = ("int", "real", "float", "double", "decimal", "numeric")


def is_sampleable(column: Column, fk_columns: set[str]) -> bool:
    """Whether showing this column's distinct values tells the model anything.

    Sampling exists to reveal value *semantics* that the declared type does
    not carry. That applies to two kinds of column:

    * text categoricals - stops the model writing ``status = 'complete'``
      when the data says ``'completed'``;
    * numeric codes, rates and flags - a column whose only values are
      0.0 / 0.05 / 0.10 / 0.15 is obviously a **rate**, and a model that can
      see that will not write ``unit_price - discount``.

    The second case was previously excluded by a type gate that only admitted
    text, which is how a real evaluation failure got through: the agent read
    ``discount REAL`` as a currency amount and subtracted it. See
    docs/DECISIONS.md (D14).

    Identifiers are excluded because their values are arbitrary. A primary key
    or a foreign key carries no semantics worth spending context on, and its
    values are join plumbing rather than meaning.
    """
    if column.primary_key or column.name in fk_columns:
        return False
    kind = column.type.lower()
    return any(k in kind for k in _TEXTUAL) or any(k in kind for k in _NUMERIC)


def _is_numeric(column: Column) -> bool:
    return any(k in column.type.lower() for k in _NUMERIC)


def render_schema_card(
    catalog: Catalog,
    tables: list[str],
    *,
    engine: Engine | None = None,
    sample_limit: int = 3,
    max_distinct: int = 40,
) -> str:
    """Render the selected tables as a compact, model-friendly schema card."""
    lines: list[str] = []
    selected = set(tables)

    for name in tables:
        table = catalog.tables.get(name)
        if table is None:
            continue
        fk_columns = {
            c
            for fk in catalog.foreign_keys
            if fk.from_table == name
            for c in fk.from_columns
        }
        count = f"{table.row_count:,} rows" if table.row_count >= 0 else "unknown size"
        lines.append(f"TABLE {name}  -- {count}")
        for col in table.columns:
            bits = [f"  {col.name} {col.type}"]
            if col.primary_key:
                bits.append("PRIMARY KEY")
            if not col.nullable:
                bits.append("NOT NULL")
            line = " ".join(bits)

            if engine is not None and is_sampleable(col, fk_columns):
                vals = sample_values(
                    engine,
                    name,
                    col.name,
                    limit=sample_limit,
                    max_distinct=max_distinct,
                )
                if vals:
                    # Numeric values are shown unquoted: rendering 0.05 as
                    # '0.05' would suggest a text column and defeat the point.
                    shown = ", ".join(
                        v if _is_numeric(col) else repr(v) for v in vals
                    )
                    line += f"   -- values include: {shown}"
            lines.append(line)
        lines.append("")

    hints = catalog.join_hints(selected)
    if hints:
        lines.append("FOREIGN KEY JOINS AVAILABLE:")
        lines.extend(f"  {h}" for h in hints)

    return "\n".join(lines).strip()
