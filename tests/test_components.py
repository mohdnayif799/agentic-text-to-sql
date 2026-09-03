"""Tests for the deterministic components: selection, budgets, tools."""

from __future__ import annotations

import time

import pytest

from agentcrew.db.introspect import build_catalog
from agentcrew.db.selector import (
    is_sampleable,
    render_schema_card,
    select_tables,
    tokenize,
)
from agentcrew.graph.control import Budget, LoopGuard, StopReason, explain_stop
from agentcrew.schemas import FailureKind
from agentcrew.tools import (
    ToolContext,
    describe_table,
    execute_readonly_sql,
    get_sample_rows,
    list_tables,
)


# ---------------------------------------------------------------------------
# Schema selection
# ---------------------------------------------------------------------------
class TestTokenize:
    def test_drops_stopwords(self) -> None:
        assert "the" not in tokenize("the customers")

    def test_normalises_plurals(self) -> None:
        assert tokenize("customers") == tokenize("customer")

    def test_splits_snake_case(self) -> None:
        assert "region" in tokenize("region_name")


class TestSelectTables:
    def test_picks_lexically_relevant_table(self, engine) -> None:
        catalog = build_catalog(engine)
        sel = select_tables("how many customers churned", catalog)
        assert "customers" in sel.tables
        assert "customers" in sel.seeded_by

    def test_expands_across_foreign_keys(self, engine) -> None:
        """'revenue by region' names neither orders nor order_items, but the
        query is impossible without them."""
        catalog = build_catalog(engine)
        sel = select_tables("total revenue by region", catalog)
        assert "regions" in sel.tables
        assert "orders" in sel.tables, "FK expansion should reach orders"

    def test_respects_the_cap(self, engine) -> None:
        catalog = build_catalog(engine)
        sel = select_tables("customers orders products regions", catalog, max_tables=3)
        assert len(sel.tables) <= 3

    def test_force_include_survives_the_cap(self, engine) -> None:
        catalog = build_catalog(engine)
        sel = select_tables(
            "customers", catalog, max_tables=2, force_include={"marketing_spend"}
        )
        assert "marketing_spend" in sel.tables

    def test_falls_back_when_nothing_matches(self, engine) -> None:
        catalog = build_catalog(engine)
        sel = select_tables("zzzz qqqq", catalog)
        assert sel.tables, "must never return an empty schema"


class TestSchemaCard:
    def test_includes_columns_and_joins(self, engine) -> None:
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["customers", "regions"], engine=engine)
        assert "TABLE customers" in card
        assert "region_id" in card
        assert "FOREIGN KEY JOINS AVAILABLE" in card

    def test_shows_values_for_low_cardinality_columns(self, engine) -> None:
        """The cure for the model inventing status='completed' vs 'complete'."""
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["orders"], engine=engine)
        assert "values include" in card
        assert "completed" in card

    def test_shows_values_for_low_cardinality_numeric_columns(self, engine) -> None:
        """Regression for the q05 evaluation failure.

        `order_items.discount` holds only 0.0/0.05/0.10/0.15 - obviously a
        rate. A type gate that admitted only text columns hid that, and the
        agent wrote `unit_price - discount` instead of `unit_price * (1 -
        discount)`. Sampling is about value semantics, not storage class.
        """
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["order_items"], engine=engine)
        line = next(
            ln for ln in card.splitlines() if ln.strip().startswith("discount")
        )
        assert "values include" in line, f"discount is not sampled: {line}"
        assert "0.05" in line

    def test_numeric_samples_are_not_quoted(self, engine) -> None:
        """Rendering 0.05 as '0.05' would imply a text column."""
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["order_items"], engine=engine)
        line = next(
            ln for ln in card.splitlines() if ln.strip().startswith("discount")
        )
        assert "'0.05'" not in line and '"0.05"' not in line

    def test_text_samples_are_still_quoted(self, engine) -> None:
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["orders"], engine=engine)
        line = next(ln for ln in card.splitlines() if ln.strip().startswith("status"))
        assert "'completed'" in line

    def test_primary_keys_are_not_sampled(self, engine) -> None:
        """Identifier values are arbitrary; sampling them wastes context."""
        catalog = build_catalog(engine)
        fks: set[str] = set()
        pk = next(c for c in catalog.tables["order_items"].columns if c.primary_key)
        assert not is_sampleable(pk, fks)

    def test_foreign_keys_are_not_sampled(self, engine) -> None:
        catalog = build_catalog(engine)
        fks = {
            c
            for fk in catalog.foreign_keys
            if fk.from_table == "order_items"
            for c in fk.from_columns
        }
        assert "product_id" in fks
        col = next(
            c for c in catalog.tables["order_items"].columns if c.name == "product_id"
        )
        assert not is_sampleable(col, fks)

    def test_foreign_key_columns_absent_from_rendered_card(self, engine) -> None:
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["order_items"], engine=engine)
        line = next(
            ln for ln in card.splitlines() if ln.strip().startswith("product_id")
        )
        assert "values include" not in line

    def test_high_cardinality_numeric_still_gated(self, engine) -> None:
        """The max_distinct gate must still apply to numerics."""
        catalog = build_catalog(engine)
        card = render_schema_card(
            catalog, ["marketing_spend"], engine=engine, max_distinct=40
        )
        line = next(ln for ln in card.splitlines() if ln.strip().startswith("amount"))
        assert "values include" not in line

    def test_omits_values_for_high_cardinality_columns(self, engine) -> None:
        catalog = build_catalog(engine)
        card = render_schema_card(catalog, ["customers"], engine=engine)
        name_line = next(
            line for line in card.splitlines() if line.strip().startswith("customer_name")
        )
        assert "values include" not in name_line


# ---------------------------------------------------------------------------
# Budgets and loop control
# ---------------------------------------------------------------------------
class TestBudget:
    def test_starts_clean(self) -> None:
        assert Budget().check() is StopReason.NONE

    def test_trips_on_llm_calls(self) -> None:
        b = Budget(max_llm_calls=2)
        b.note_llm_call()
        assert b.check() is StopReason.NONE
        b.note_llm_call()
        assert b.check() is StopReason.LLM_BUDGET

    def test_trips_on_sql_executions(self) -> None:
        b = Budget(max_sql_executions=1)
        b.note_sql()
        assert b.check() is StopReason.SQL_BUDGET

    def test_trips_on_wall_clock(self) -> None:
        b = Budget(wall_clock_seconds=0.01)
        time.sleep(0.02)
        assert b.check() is StopReason.TIME_BUDGET

    def test_exhausted_matches_check(self) -> None:
        b = Budget(max_llm_calls=1)
        assert not b.exhausted()
        b.note_llm_call()
        assert b.exhausted()


class TestLoopGuard:
    def test_first_sighting_is_not_a_repeat(self) -> None:
        g = LoopGuard()
        is_repeat, count = g.observe("abc")
        assert not is_repeat and count == 1

    def test_second_sighting_is_a_repeat(self) -> None:
        g = LoopGuard()
        g.observe("abc")
        is_repeat, count = g.observe("abc")
        assert is_repeat and count == 2

    def test_aborts_at_the_repeat_limit(self) -> None:
        g = LoopGuard(repeat_limit=2)
        g.observe("abc")
        assert not g.should_abort("abc")
        g.observe("abc")
        assert g.should_abort("abc")

    def test_reset_clears_between_steps(self) -> None:
        g = LoopGuard()
        g.observe("abc")
        g.reset()
        assert not g.should_abort("abc")


@pytest.mark.parametrize("reason", list(StopReason))
def test_every_stop_reason_has_wording(reason: StopReason) -> None:
    text = explain_stop(reason)
    if reason is StopReason.NONE:
        assert text == ""
    else:
        assert len(text) > 20, f"{reason} needs a user-facing explanation"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@pytest.fixture
def ctx(engine) -> ToolContext:
    return ToolContext.create(engine, max_rows=100)


class TestTools:
    def test_list_tables(self, ctx: ToolContext) -> None:
        names = {t["table"] for t in list_tables(ctx)}
        assert {"customers", "orders", "order_items", "regions"} <= names

    def test_describe_table(self, ctx: ToolContext) -> None:
        info = describe_table(ctx, "customers")
        assert info["rows"] > 0
        assert any(c["name"] == "churn_date" for c in info["columns"])
        assert info["foreign_keys"]

    def test_describe_unknown_table_lists_alternatives(self, ctx: ToolContext) -> None:
        info = describe_table(ctx, "nope")
        assert "error" in info and "available" in info

    def test_sample_rows(self, ctx: ToolContext) -> None:
        got = get_sample_rows(ctx, "regions", limit=2)
        assert len(got["rows"]) == 2

    def test_sample_rows_rejects_unknown_table(self, ctx: ToolContext) -> None:
        assert "error" in get_sample_rows(ctx, "'; DROP TABLE x; --")

    def test_execute_success(self, ctx: ToolContext) -> None:
        r = execute_readonly_sql(ctx, "SELECT count(*) AS n FROM regions")
        assert r.ok and r.rows[0][0] == 4

    def test_execute_blocks_writes_and_returns_it_as_data(
        self, ctx: ToolContext
    ) -> None:
        """The agent must be able to *read* the rejection, not crash on it."""
        r = execute_readonly_sql(ctx, "DELETE FROM customers")
        assert not r.ok
        assert r.failure is FailureKind.BLOCKED
        assert "read-only" in (r.error_message or "")

    def test_execute_reports_missing_object(self, ctx: ToolContext) -> None:
        r = execute_readonly_sql(ctx, "SELECT * FROM no_such_table")
        assert not r.ok and r.failure is FailureKind.MISSING_OBJECT

    def test_execute_reports_syntax_error(self, ctx: ToolContext) -> None:
        r = execute_readonly_sql(ctx, "SELECT * FROM WHERE")
        assert not r.ok and r.failure in {FailureKind.SYNTAX, FailureKind.BLOCKED}

    def test_execute_flags_empty_result(self, ctx: ToolContext) -> None:
        r = execute_readonly_sql(
            ctx, "SELECT * FROM customers WHERE region_id = -1"
        )
        assert r.ok and r.failure is FailureKind.EMPTY

    def test_row_cap_is_enforced(self, engine) -> None:
        small = ToolContext.create(engine, max_rows=5)
        r = execute_readonly_sql(small, "SELECT * FROM customers")
        assert r.row_count <= 5

    def test_engine_is_physically_read_only(self, engine) -> None:
        """Bypass the guard entirely: the connection itself must refuse."""
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError

        with engine.connect() as conn, pytest.raises(OperationalError, match="readonly"):
            conn.execute(text("DELETE FROM customers"))
