"""Shared context: data dictionary, value profile, example pool, builder."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from agentcrew.context import ContextBuilder
from agentcrew.context.dictionary import (
    DICTIONARY_PATH,
    load_dictionary,
    render_dictionary,
)
from agentcrew.context.examples import (
    EXAMPLES_PATH,
    ExampleIndex,
    load_examples,
    pairwise_similarity,
)
from agentcrew.context.values import profile_columns
from agentcrew.db.introspect import build_catalog
from agentcrew.db.safety import guard_sql
from evalkit.questions import GOLD_FILES, SET_FILES, load_set

ROOT = Path(__file__).resolve().parents[1]

#: User-language phrasings used by the term-matching questions. The dictionary
#: holds definitions and real values only, so none of these may appear.
FORBIDDEN_PHRASES = (
    "sev-1", "sev1", "sev 1", "web store", "webstore", "mid-sized", "mid sized",
    "european", "europe", "german", "reseller", "small business",
    "asia-pacific", "asia pacific", "cobolt",
)


def _load_pool_builder():
    spec = importlib.util.spec_from_file_location(
        "build_example_pool", ROOT / "scripts" / "build_example_pool.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDictionary:
    def test_allowed_values_match_the_database(self, engine):
        dictionary = load_dictionary()
        with engine.connect() as conn:
            for table, spec in dictionary["tables"].items():
                for column, col_spec in spec["columns"].items():
                    if not (isinstance(col_spec, dict) and "allowed_values" in col_spec):
                        continue
                    actual = sorted(conn.execute(text(
                        f'SELECT DISTINCT "{column}" FROM "{table}"'  # noqa: S608
                    )).scalars())
                    assert sorted(col_spec["allowed_values"]) == actual, column

    def test_every_table_and_column_is_documented(self, engine):
        catalog = build_catalog(engine)
        tables = load_dictionary()["tables"]
        assert set(tables) == set(catalog.tables)
        for name, table in catalog.tables.items():
            assert set(tables[name]["columns"]) == {c.name for c in table.columns}

    def test_no_user_language_synonyms(self):
        raw = DICTIONARY_PATH.read_text("utf-8").lower()
        rendered = render_dictionary(load_dictionary()).lower()
        found = [
            p for p in FORBIDDEN_PHRASES
            if any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", t) for t in (raw, rendered))
        ]
        assert found == []

    def test_no_gold_facts(self, engine):
        """No product or customer name and no gold number appears."""
        rendered = render_dictionary(load_dictionary())
        with engine.connect() as conn:
            names = set(conn.execute(text(
                "SELECT product_name FROM products UNION SELECT customer_name FROM customers"
            )).scalars())
        leaked = []
        for name in SET_FILES:
            for entry in json.loads(GOLD_FILES[name].read_text("utf-8"))["gold"].values():
                tables = [entry] + list(entry.get("interpretations", {}).values())
                for table in tables:
                    for row in table.get("rows", []):
                        for cell in row:
                            if isinstance(cell, str) and cell in names and cell in rendered:
                                leaked.append(cell)
                            if isinstance(cell, int | float) and not isinstance(cell, bool) \
                                    and abs(cell) >= 100 and f"{cell:g}" in rendered:
                                leaked.append(cell)
        assert leaked == []


class TestProfile:
    def test_lists_low_cardinality_text_and_summarises_dates(self, engine):
        profile = profile_columns(engine, build_catalog(engine))
        listed = {(v.table, v.column): v.values for v in profile.values}
        dates = {(d.table, d.column): d for d in profile.dates}
        assert listed[("support_tickets", "severity")] == (
            "critical", "high", "low", "medium")
        assert ("customers", "customer_name") not in listed  # 726 distinct
        assert ("customers", "churn_date") not in listed  # a date column
        month = dates[("marketing_spend", "month")]
        assert (month.fmt, month.minimum, month.maximum) == ("YYYY-MM", "2024-01", "2025-12")
        assert dates[("orders", "order_date")].maximum == "2025-12-31"


class TestBuilder:
    def test_render_has_every_section_and_the_pinned_date(self, engine):
        builder = ContextBuilder.from_database(
            engine, build_catalog(engine), today=date(2026, 10, 4)
        )
        ctx = builder.build("How many support tickets are still unresolved?")
        rendered = ctx.render()
        for section in ("## Database schema", "## Allowed values", "## Date columns",
                        "## Data dictionary", "## Verified examples", "## Today's date"):
            assert section in rendered
        assert "2026-10-04" in rendered
        assert 1 <= len(ctx.examples) <= 3
        # Full schema always: every table is present.
        for table in ("customers", "marketing_spend", "support_tickets"):
            assert f"TABLE {table}" in rendered


class TestExamplePool:
    def test_pool_holds_question_and_sql_only(self):
        data = yaml.safe_load(EXAMPLES_PATH.read_text("utf-8"))
        assert data["pool"]
        for item in data["pool"]:
            assert set(item) == {"id", "question", "sql"}
        pool_ids = {e["id"] for e in data["pool"]}
        assert pool_ids.isdisjoint({e["id"] for e in data["excluded"]})

    def test_pool_sql_is_read_only_and_runs(self, engine):
        with engine.connect() as conn:
            for example in load_examples():
                guarded = guard_sql(example.sql, max_rows=500, allow_write=False)
                conn.execute(text(guarded.sql)).fetchall()

    def test_no_example_is_close_to_an_eval_question(self):
        questions = [q["question"] for name in SET_FILES for q in load_set(name)]
        sims = pairwise_similarity([e.question for e in load_examples()], questions)
        assert max(max(row) for row in sims) < 0.5

    def test_pool_file_is_reproducible(self, db_path):
        builder = _load_pool_builder()
        from evalkit.gold import connect_read_only

        rebuilt = builder.build(connect_read_only(db_path))
        stored = yaml.safe_load(EXAMPLES_PATH.read_text("utf-8"))
        assert rebuilt == stored

    def test_retrieval_ranks_the_closest_example_first(self):
        index = ExampleIndex(load_examples())
        top = index.top_k("How many support tickets are unresolved right now?", k=3)
        assert top[0].id == "diag/d04"

    @pytest.mark.parametrize("k", [1, 2, 3])
    def test_retrieval_returns_at_most_k(self, k):
        assert len(ExampleIndex(load_examples()).top_k("revenue by region", k)) <= k
