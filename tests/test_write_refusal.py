"""A request to change the data is refused first, before any LLM call or SQL, and is
not counted as a run. Questions about such things are not requests. The read-only
guard stays as the second layer. Offline."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from ui_harness import OWN_KEY, FakeAgent, ask, sidebar_captions, start_app

from agentcrew.archs import run_architecture
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.refusal import WRITE_REFUSAL, is_write_request
from agentcrew.tools import ToolContext, execute_readonly_sql
from evalrun.sets import ALL_SET_FILES, load_questions

DELETE = "Delete all customers in the LATAM region."


class TestRefused:
    @pytest.mark.parametrize("request_text", [
        # Each verb the rule names.
        DELETE,
        "Remove customer 1133 from the database.",
        "Drop the orders table.",
        "Update order 5 to completed.",
        "Update the status of order 5.",
        "Insert a new customer called Ada.",
        "Truncate the support tickets table.",
        "Alter the orders table.",
        "Create a table called archive.",
        "Create a new customer called Ada.",
        # And similar ones.
        "Erase every refunded order.",
        "Wipe the customers table.",
        "Purge old tickets.",
        "Rename the segment column.",
        "Modify the price of product 5.",
        "Edit order 5.",
        "Overwrite the region names.",
        "Change the price of product 5 to 100.",
        "Set all discounts to 0.",
        "Replace Online with Web in the orders table.",
        "Add a new customer called Ada.",
        "Add 3 rows to orders.",
        "Clear the tickets table.",
        "Reset all prices.",
        "Cancel order 5.",
        "Mark order 5 as refunded.",
        "Get rid of all the cancelled orders.",
        # Politely, or as a wish.
        "Please delete all customers.",
        "Can you delete customer 5?",
        "Could you please remove order 12?",
        "Would you drop the regions table?",
        "I want to delete all orders.",
        "I'd like you to update the prices.",
        "We need to remove the duplicates.",
        "Let's truncate the orders table.",
        "Go ahead and delete them.",
        "Okay, delete customer 5.",
        "delete customer 5",
        # As SQL.
        "DROP TABLE customers",
        "DELETE FROM orders WHERE order_id = 5",
        "UPDATE orders SET status = 'refunded'",
        "INSERT INTO regions VALUES (9, 'x', 'y')",
        "Run this: DROP TABLE customers",
        "Execute DELETE FROM orders",
        "SELECT * FROM orders; DROP TABLE orders",
        # Beside a real question.
        "How many customers are there? Delete them all.",
        "Show me the refunded orders and delete them.",
        "Find the duplicate customers, then remove them.",
    ])
    def test_a_request_to_change_the_data(self, request_text):
        assert is_write_request(request_text), request_text


class TestNotRefused:
    @pytest.mark.parametrize("question", [
        # The rule's own examples.
        "how many orders were deleted?",
        "which customers were removed last year?",
        # The verbs as past tense, in a question.
        "How many tickets were updated in 2025?",
        "How many customers were created in 2025?",
        "When was order 5 updated?",
        "Who removed the customers?",
        "Count the orders that were dropped.",
        "Show me the deleted orders.",
        "List customers removed last year.",
        "Show orders that were updated and the customers who created them.",
        "Which customers were added and removed last year?",
        # Questions about what such a thing would do.
        "What happens if I delete a customer?",
        "Which customers should I delete?",
        "Can orders be deleted?",
        "How do I delete a customer?",
        "Did we drop the Partner channel?",
        "Give me a list of customers to delete.",
        "I want to see the deleted orders.",
        "Can you tell me how many orders were deleted?",
        # The words as nouns.
        "Why did revenue drop in Q3 2025?",
        "Drop in revenue by region last quarter?",
        "Drop-off rate by channel?",
        "Is the drop in revenue real?",
        "Why did revenue rise and drop the next month?",
        "Update frequency by customer?",
        "Delete counts by month?",
        "Edit history of order 5?",
        "Change in revenue from Q2 to Q3?",
        "Set of customers who bought twice?",
        "Clear winner among the regions?",
        "Cancel rate by channel?",
        "Removed customers by region?",
        "Create date of the newest order?",
        # Analysis, not a change to the data.
        "Create a chart of revenue by region.",
        "Create a table of revenue by region.",
        "Create a report of the deleted orders.",
        "Add up revenue by region.",
        "Add revenue and spend together.",
        "Remove refunded orders from the total and show revenue.",
        "Drop cancelled orders and compute the average order value.",
        "Remove the outliers and recompute the average.",
        "Update me on revenue in 2025.",
        # A bare word as a heading, then a real question.
        "Edit: I meant 2025, how many orders were placed?",
        "Update: how many orders were placed?",
        "",
    ])
    def test_a_question_about_the_data(self, question):
        assert not is_write_request(question), question

    @pytest.mark.parametrize("set_name", sorted(ALL_SET_FILES))
    def test_no_real_question_in_any_set_is_refused(self, set_name):
        """Only the legacy safety question asks for a change on purpose."""
        flagged = [q["question"] for q in load_questions(set_name)
                   if is_write_request(q["question"])]
        assert set(flagged) <= {DELETE}, flagged


class Script:
    """Fails the test if the model is called, unless given replies."""

    def __init__(self, sql=None):
        self.sql = sql
        self.calls: list[str] = []

    def __call__(self, system, user):
        if self.sql is None:
            raise AssertionError(f"the model was called: {system[:60]}")
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": False, "reason": ""}
        if "You fix one SQLite query" in system:
            self.calls.append("repair")
            return {"sql": self.sql + " ", "rationale": "x", "expected_shape": ""}
        if "You plan the next step" in system:
            self.calls.append("decide")
            return {"action": "answer"}
        if "final answer" in system:
            self.calls.append("answer")
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            self.calls.append("step0")
            return {"sql": self.sql, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(arch, question, script):
        return run_architecture(arch, question, settings=pinned,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


class TestInTheArchitectures:
    @pytest.mark.parametrize("arch", ["router", "full"])
    def test_c_and_d_refuse_with_no_call_and_no_table(self, run, arch):
        out = run(arch, DELETE, Script())  # the script fails on any call
        assert out.status == "refused" and out.answer == WRITE_REFUSAL
        assert out.selected is None and out.attempts == () and out.arch == arch

    def test_a_is_not_changed(self, run):
        """A is the baseline: it has its model call and meets the guard."""
        script = Script(sql="SELECT COUNT(*) AS n FROM customers")
        out = run("baseline", DELETE, script)
        assert script.calls == ["step0", "answer"] and out.status == "done"

    def test_a_question_about_it_runs_normally(self, run):
        script = Script(sql="SELECT COUNT(*) AS n FROM customers")
        out = run("full", "how many orders were deleted?", script)
        assert out.status == "done" and script.calls[0] == "step0"


class TestTheGuardIsStillTheSecondLayer:
    def test_a_request_the_first_layer_misses_cannot_write(self, run, engine):
        """Worded so the first layer lets it through, with a model that writes."""
        tools = ToolContext.create(engine)
        before = execute_readonly_sql(tools, "SELECT COUNT(*) FROM customers").rows
        question = "Is there any way you could get those customers out of the table?"
        assert not is_write_request(question)
        out = run("full", question, Script(sql="DELETE FROM customers"))
        assert out.selected is None or out.selected.rows
        assert all(not a.ok for a in out.attempts)
        assert execute_readonly_sql(tools, "SELECT COUNT(*) FROM customers").rows == before

    @pytest.mark.parametrize("sql", ["DELETE FROM customers", "DROP TABLE customers"])
    def test_the_guard_itself_still_rejects(self, engine, sql):
        assert not execute_readonly_sql(ToolContext.create(engine), sql).ok


def warnings(at: AppTest) -> list[str]:
    return [w.value for w in at.warning]


def test_the_app_shows_a_later_refusal_as_an_answer():
    # archs refuse with status "refused" when the model writes a write statement.
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text("utf-8")
    assert 'if outcome.status in ("done", "refused"):' in source


class TestInTheApp:
    def test_refused_with_no_run_no_call_and_no_table(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = ask(start_app(), DELETE)
        assert warnings(at) == [WRITE_REFUSAL]
        assert fake_agent.questions == []
        assert "Demo runs left: 5 of 5" in sidebar_captions(at)
        assert not at.dataframe and not at.success and not at.error

    def test_the_example_chip_is_refused_the_same_way(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        chip = next(b for b in at.button if b.label == DELETE)
        chip.click().run()
        assert warnings(at) == [WRITE_REFUSAL] and fake_agent.questions == []
        assert "Demo runs left: 5 of 5" in sidebar_captions(at)

    def test_a_question_about_deletions_is_a_normal_counted_run(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        question = "how many orders were deleted?"
        at = ask(start_app(), question)
        assert fake_agent.questions == [question]
        assert WRITE_REFUSAL not in warnings(at)
        assert "Demo runs left: 4 of 5" in sidebar_captions(at)

    def test_refused_even_with_no_runs_left(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        for _ in range(5):
            at = ask(at, "q")
        at = ask(at, DELETE)
        assert warnings(at)[-1] == WRITE_REFUSAL
        assert fake_agent.questions.count(DELETE) == 0

    def test_refused_on_a_visitors_own_key_too(
        self, demo_env: None, fake_agent: FakeAgent
    ) -> None:
        at = start_app()
        at.text_input(key="api_key_gemini").set_value(OWN_KEY).run()
        at = ask(at, DELETE)
        assert warnings(at) == [WRITE_REFUSAL] and fake_agent.questions == []

    def test_the_log_line_says_refused_and_not_the_question(
        self, demo_env: None, fake_agent: FakeAgent, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            ask(start_app(), DELETE)
        lines = [r.getMessage() for r in caplog.records if "status=refused" in r.getMessage()]
        assert len(lines) == 1 and "llm_calls=0" in lines[0]
        assert "LATAM" not in lines[0]
