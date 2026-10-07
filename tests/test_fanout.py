"""Zero-LLM join fan-out check (agentcrew/archs/fanout.py)."""

from __future__ import annotations

import pytest
import yaml

from agentcrew.archs.fanout import KeyIndex, fanout_problem
from agentcrew.db.introspect import build_catalog
from evalkit.questions import SET_FILES, load_set


@pytest.fixture(scope="module")
def keys(request):
    from agentcrew.db.engine import read_only_engine

    db_path = request.getfixturevalue("db_path")
    engine = read_only_engine(db_path)
    return KeyIndex(engine, build_catalog(engine))


FANOUTS = {
    "order lines x marketing spend through region": (
        "SELECT SUM(oi.quantity * oi.unit_price), SUM(m.amount) FROM regions r "
        "JOIN customers c ON c.region_id = r.region_id "
        "JOIN orders o ON o.customer_id = c.customer_id "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN marketing_spend m ON m.region_id = r.region_id"),
    "orders x tickets on customer": (
        "SELECT c.customer_id, COUNT(*) FROM orders o "
        "JOIN support_tickets t ON t.customer_id = o.customer_id "
        "JOIN customers c ON c.customer_id = o.customer_id GROUP BY 1"),
    "expression key": (
        "SELECT m.month, SUM(m.amount) FROM marketing_spend m "
        "JOIN orders o ON substr(o.order_date, 1, 7) = m.month GROUP BY 1"),
    "composite key that is still not unique": (
        "SELECT SUM(m.amount) FROM orders o "
        "JOIN customers c ON c.customer_id = o.customer_id "
        "JOIN marketing_spend m ON m.region_id = c.region_id "
        "AND m.month = substr(o.order_date, 1, 7)"),
}

SAFE = {
    "lookup joins": (
        "SELECT p.category, SUM(oi.quantity) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN products p ON p.product_id = oi.product_id GROUP BY 1"),
    "count distinct": (
        "SELECT COUNT(DISTINCT o.order_id) FROM orders o "
        "JOIN support_tickets t ON t.customer_id = o.customer_id"),
    "no aggregate": (
        "SELECT o.order_id, t.ticket_id FROM orders o "
        "JOIN support_tickets t ON t.customer_id = o.customer_id"),
    "aggregate per table, then join": (
        "SELECT r.region_name, rev.x, mkt.y FROM regions r "
        "JOIN (SELECT c.region_id, SUM(oi.quantity) AS x FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN customers c ON c.customer_id = o.customer_id GROUP BY 1) rev "
        "ON rev.region_id = r.region_id "
        "JOIN (SELECT region_id, SUM(amount) AS y FROM marketing_spend GROUP BY 1) mkt "
        "ON mkt.region_id = r.region_id"),
    "join filter on a constant": (
        "WITH t AS (SELECT customer_id FROM customers LIMIT 1) "
        "SELECT t.customer_id, SUM(oi.quantity) FROM t "
        "LEFT JOIN orders o ON o.customer_id = t.customer_id AND o.status = 'completed' "
        "LEFT JOIN order_items oi ON oi.order_id = o.order_id GROUP BY 1"),
    "min and max are safe": (
        "SELECT MAX(o.order_date) FROM orders o "
        "JOIN support_tickets t ON t.customer_id = o.customer_id"),
}


@pytest.mark.parametrize("name", FANOUTS)
def test_fanouts_are_flagged(keys, name):
    assert fanout_problem(FANOUTS[name], keys)


@pytest.mark.parametrize("name", SAFE)
def test_safe_queries_are_not_flagged(keys, name):
    assert fanout_problem(SAFE[name], keys) is None


def test_no_gold_evidence_or_reference_query_is_flagged(keys):
    sqls = []
    for name in SET_FILES:
        for q in load_set(name):
            for spec in [q.get("gold")] + [i["gold"] for i in q.get("interpretations", [])]:
                if spec and "sql" in spec:
                    sqls.append(spec["sql"])
            sqls += list((q.get("evidence") or {}).values())
    for path in ("eval/questions.yaml", "eval/questions_diagnostic.yaml",
                 "eval/questions_holdout.yaml", "eval/heldout.yaml"):
        with open(path, encoding="utf-8") as handle:
            sqls += [q["reference_sql"] for q in yaml.safe_load(handle)
                     if q.get("reference_sql")]
    flagged = [s for s in sqls if fanout_problem(s, keys)]
    assert flagged == []


def test_unparseable_sql_is_not_flagged(keys):
    assert fanout_problem("SELEC nonsense ((", keys) is None
