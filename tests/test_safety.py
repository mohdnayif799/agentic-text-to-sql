"""Safety guard tests.

This is the highest-value test file in the repo: it is the component whose
failure has real consequences, and it is fully deterministic.
"""

from __future__ import annotations

import pytest

from agentcrew.db.safety import UnsafeSQLError, fingerprint, guard_sql

MAX = 500


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from customers",
        "SELECT count(*) FROM orders",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "SELECT a FROM t UNION SELECT b FROM u",
        "SELECT r.region_name, sum(o.order_id) FROM orders o "
        "JOIN regions r ON r.region_id = o.order_id GROUP BY 1",
        "SELECT * FROM customers WHERE churn_date IS NOT NULL LIMIT 10",
    ],
)
def test_read_only_statements_pass(sql: str) -> None:
    assert guard_sql(sql, max_rows=MAX).sql


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM customers",
        "UPDATE customers SET segment = 'x'",
        "INSERT INTO customers (customer_id) VALUES (1)",
        "ALTER TABLE customers ADD COLUMN evil TEXT",
        "CREATE TABLE evil (a INT)",
        "CREATE VIEW v AS SELECT 1",
        "PRAGMA table_info(customers)",
        "ATTACH DATABASE '/tmp/evil.db' AS evil",
        "DETACH DATABASE evil",
        "VACUUM",
    ],
)
def test_mutating_and_command_statements_are_blocked(sql: str) -> None:
    with pytest.raises(UnsafeSQLError):
        guard_sql(sql, max_rows=MAX)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE customers",
        "SELECT 1;DELETE FROM orders;",
        "SELECT 1; SELECT 2",
    ],
)
def test_statement_batching_is_blocked(sql: str) -> None:
    with pytest.raises(UnsafeSQLError, match="one statement"):
        guard_sql(sql, max_rows=MAX)


@pytest.mark.parametrize("sql", ["", "   ", "\n"])
def test_empty_is_blocked(sql: str) -> None:
    with pytest.raises(UnsafeSQLError, match="Empty"):
        guard_sql(sql, max_rows=MAX)


def test_unparseable_is_blocked() -> None:
    with pytest.raises(UnsafeSQLError, match="parse"):
        guard_sql("SELECT FROM WHERE (((", max_rows=MAX)


def test_cte_hiding_a_write_is_blocked() -> None:
    with pytest.raises(UnsafeSQLError):
        guard_sql(
            "WITH x AS (DELETE FROM customers RETURNING 1) SELECT * FROM x",
            max_rows=MAX,
        )


def test_limit_injected_when_absent() -> None:
    result = guard_sql("SELECT * FROM customers", max_rows=25)
    assert result.limit_injected
    assert "LIMIT 25" in result.sql.upper()


def test_existing_limit_is_respected() -> None:
    result = guard_sql("SELECT * FROM customers LIMIT 3", max_rows=500)
    assert not result.limit_injected
    assert "LIMIT 3" in result.sql.upper()


def test_bare_aggregate_needs_no_limit() -> None:
    result = guard_sql("SELECT count(*) FROM orders", max_rows=500)
    assert not result.limit_injected


def test_grouped_aggregate_still_gets_a_limit() -> None:
    """GROUP BY can return unbounded rows, so the cap must still apply."""
    result = guard_sql(
        "SELECT region_id, count(*) FROM customers GROUP BY region_id", max_rows=50
    )
    assert result.limit_injected


def test_referenced_tables_are_reported() -> None:
    result = guard_sql(
        "SELECT * FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
        max_rows=MAX,
    )
    assert result.referenced_tables == ("customers", "orders")


def test_write_mode_permits_mutation_when_explicitly_enabled() -> None:
    """Opt-in write mode exists but is off by default everywhere."""
    assert guard_sql(
        "UPDATE customers SET segment='x'", max_rows=MAX, allow_write=True
    ).sql


class TestFingerprint:
    def test_ignores_whitespace_and_case(self) -> None:
        assert fingerprint("select  A   from  T") == fingerprint("SELECT a FROM t")

    def test_distinguishes_different_queries(self) -> None:
        assert fingerprint("SELECT a FROM t") != fingerprint("SELECT b FROM t")

    def test_handles_unparseable_input(self) -> None:
        assert fingerprint("not sql at all (((") == fingerprint("not sql at all (((")
