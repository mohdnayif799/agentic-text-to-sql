"""The stated assumption is built in code from the result that was graded and
shown, so it does not depend on the model following the answer instruction.
Offline: a scripted model and the real database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.assumption import assumption_sentence, open_terms
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext


def result(columns, rows, sql):
    return ExecutionResult(ok=True, sql=sql, columns=columns, rows=rows,
                           row_count=len(rows))


class TestOpenTerms:
    @pytest.mark.parametrize(("question", "metric_open", "window_open"), [
        ("Which customer segment is our most valuable?", True, True),
        ("What is our most popular product?", True, True),
        ("Which channel is the least profitable?", True, True),
        ("What was our best month in 2025?", True, False),
        ("Which was our worst region last year?", True, False),
        ("Who was our top customer in Q3 2025?", True, False),
        ("Which are the top 3 products by completed revenue?", False, True),
        ("Who are our best customers in terms of revenue?", False, True),
        ("Who are our best customers, broken down by region?", True, True),
    ])
    def test_vague_ranking_words(self, question, metric_open, window_open):
        terms = open_terms(question)
        assert terms is not None
        assert (terms.metric, terms.window) == (metric_open, window_open)

    @pytest.mark.parametrize("question", [
        "Who was our top customer by completed revenue in 2024?",
        "What were the top 3 products by completed revenue in 2024?",
        "Who are our best customers by revenue, all time?",
        "Which region has the most customers?",
        "Which product sold the most units in completed orders during 2025?",
        "How many customers signed up in 2025?",
        "What is our best-selling product in 2025?",
        "What sits on top of the revenue table in 2025?",
    ])
    def test_named_measures_and_other_questions(self, question):
        assert open_terms(question) is None


ALL_TIME = ("SELECT c.segment, SUM(o.amount) AS revenue FROM orders o "
            "JOIN customers c ON c.id = o.customer_id GROUP BY c.segment "
            "ORDER BY revenue DESC LIMIT 1")
UNITS = ("SELECT p.product_id, p.product_name, SUM(oi.quantity) AS units_sold, "
         "COUNT(*) AS lines FROM order_items oi JOIN products p "
         "ON p.product_id = oi.product_id GROUP BY 1, 2 ORDER BY units_sold DESC")


class TestSentence:
    def test_metric_and_window_both_come_from_the_query(self):
        sentence = assumption_sentence(
            "Which customer segment is our most valuable?",
            result(["segment", "revenue"], [["SMB", 9.0]], ALL_TIME))
        assert sentence == ('Assuming "most valuable" is measured by revenue, '
                            "across all dates in the data.")

    def test_the_metric_is_the_column_the_query_ranks_by(self):
        sentence = assumption_sentence(
            "What is our most popular product?",
            result(["product_id", "product_name", "units_sold", "lines"],
                   [[14, "Vertex Nimbus", 1926, 613]], UNITS))
        assert '"most popular" is measured by units sold,' in sentence

    def test_an_unnamed_order_by_expression_resolves_to_its_column(self):
        sql = ("SELECT p.name, SUM(oi.quantity) AS units FROM order_items oi "
               "JOIN products p ON p.id = oi.product_id GROUP BY p.name "
               "ORDER BY SUM(oi.quantity) DESC")
        sentence = assumption_sentence(
            "What is our most popular product?",
            result(["name", "units"], [["A", 5]], sql))
        assert "measured by units," in sentence

    def test_without_an_order_by_it_is_the_last_numeric_column(self):
        sql = "SELECT id, segment, n, revenue FROM t"
        sentence = assumption_sentence(
            "Which segment is our best?",
            result(["id", "segment", "n", "revenue"], [[1, "SMB", 3, 9.0]], sql))
        assert "measured by revenue," in sentence

    @pytest.mark.parametrize(("where", "window"), [
        ("o.day BETWEEN '2025-01-01' AND '2025-12-31'", "in 2025"),
        ("substr(o.day, 1, 4) = '2025'", "in 2025"),
        ("o.day LIKE '2025%'", "in 2025"),
        ("o.day LIKE '2025-%'", "in 2025"),
        ("o.day BETWEEN '2025-04-01' AND '2025-06-30'",
         "from 2025-04-01 to 2025-06-30"),
    ])
    def test_the_window_is_read_from_the_query(self, where, window):
        sql = f"SELECT o.region, SUM(o.x) AS total FROM o WHERE {where} GROUP BY 1"
        sentence = assumption_sentence(
            "Which region is our most valuable?",
            result(["region", "total"], [["APAC", 1.0]], sql))
        assert sentence.endswith(f"measured by total, {window}.")

    def test_a_status_filter_is_not_a_date_window(self):
        sql = ("SELECT region, SUM(x) AS total FROM o WHERE status = 'completed' "
               "GROUP BY 1")
        sentence = assumption_sentence(
            "Which region is our most valuable?",
            result(["region", "total"], [["APAC", 1.0]], sql))
        assert sentence.endswith("across all dates in the data.")

    def test_only_the_open_part_is_stated(self):
        sql = ("SELECT month, SUM(x) AS revenue FROM o WHERE month LIKE '2025%' "
               "GROUP BY 1")
        res = result(["month", "revenue"], [["2025-05", 1.0]], sql)
        assert assumption_sentence("What was our best month in 2025?", res) == (
            'Assuming "best" is measured by revenue.')
        assert assumption_sentence(
            "What is our top month by completed revenue?", res) == (
            "Assuming the period in 2025.")

    @pytest.mark.parametrize(("columns", "rows", "sql"), [
        (["segment"], [["SMB"]], "SELECT segment FROM t"),
        (["product_id", "name"], [[14, "A"]], "SELECT product_id, name FROM t"),
        (["month", "n"], [["2025-01", 1]], "-- compute: pct_change on step 1"),
    ])
    def test_no_sentence_without_a_measure_or_a_query(self, columns, rows, sql):
        assert assumption_sentence("Which segment is our best?",
                                   result(columns, rows, sql)) is None

    def test_no_sentence_for_a_question_that_names_everything(self):
        assert assumption_sentence(
            "Who was our top customer by completed revenue in 2024?",
            result(["segment", "revenue"], [["SMB", 9.0]], ALL_TIME)) is None


SEGMENTS = ("SELECT c.segment, COUNT(*) AS customers FROM customers c "
            "GROUP BY c.segment ORDER BY customers DESC LIMIT 1")
VAGUE = "Which customer segment is our most valuable?"


class Script:
    def __init__(self, answer="SMB is the most valuable, with 512 customers."):
        self.answer = answer
        self.calls: list[str] = []

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": False, "reason": ""}
        if "final answer" in system:
            self.calls.append("answer")
            return {"answer": self.answer, "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            self.calls.append("step0")
            return {"sql": SEGMENTS, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(arch, script, question=VAGUE):
        return run_architecture(arch, question, settings=pinned,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


@pytest.mark.parametrize("arch", ["router", "full"])
class TestInTheRun:
    def test_the_direct_answer_opens_with_the_reading(self, run, arch):
        script = Script()
        out = run(arch, script)
        assert out.path == "direct"
        assert script.calls == ["step0", "decision", "answer"]  # no extra call
        assert out.answer == ('Assuming "most valuable" is measured by customers, '
                              "across all dates in the data. SMB is the most "
                              "valuable, with 512 customers.")

    def test_a_reading_the_model_stated_is_not_repeated(self, run, arch):
        stated = "Assuming value means customers, SMB leads with 512."
        out = run(arch, Script(answer=stated))
        assert out.answer == stated

    def test_a_question_that_names_its_measure_is_left_alone(self, run, arch):
        plain = "Scripted."
        out = run(arch, Script(answer=plain),
                  question="How many customers does each segment have?")
        assert out.answer == plain


def test_a_is_not_changed(run):
    """A is the baseline: its answer stays the model's own words."""
    script = Script()
    out = run("baseline", script)
    assert out.answer == script.answer
    assert script.calls == ["step0", "answer"]
