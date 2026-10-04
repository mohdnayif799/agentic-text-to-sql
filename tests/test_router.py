"""Architecture C (router): direct path, repair discipline, follow-ups and
final-table assembly, offline on scripted models."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.assemble import AssemblyColumn, AssemblyPlan, assemble
from agentcrew.archs.checks import (
    classify,
    forecast_signal,
    schema_error,
    schema_of,
)
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext

TOP_2024 = ("SELECT c.customer_id, c.customer_name FROM customers c "
            "JOIN orders o ON o.customer_id = c.customer_id WHERE c.customer_id = 1052 "
            "GROUP BY c.customer_id")
REVENUE_2025 = ("SELECT SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS revenue_2025 "
                "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                "WHERE o.status = 'completed' AND o.customer_id = 1052 "
                "AND o.order_date BETWEEN '2025-01-01' AND '2025-12-31'")


class Script:
    """Scripted model for the router. Each list is consumed in order."""

    def __init__(self, *, step0, needs_followup=False, repairs=(), followups=(),
                 plan=None):
        self.step0 = step0
        self.needs_followup = needs_followup
        self.repairs = list(repairs)
        self.followups = list(followups)
        self.plan = plan
        self.calls: list[str] = []
        self.answer_prompt = ""

    def __call__(self, system: str, user: str):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": self.needs_followup,
                    "reason": "needs the customer first"}
        if "You fix one SQLite query" in system:
            self.calls.append("repair")
            return {"sql": self.repairs.pop(0), "rationale": "fix",
                    "expected_shape": ""}
        if "continuing an analysis" in system:
            self.calls.append("followup")
            if not self.followups:
                return {"done": True, "sql": "", "reason": "enough"}
            return {"done": False, "sql": self.followups.pop(0), "reason": "next hop"}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            return self.plan
        if "final answer" in system:
            self.calls.append("answer")
            self.answer_prompt = user
            return {"answer": "Scripted answer.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:  # step 0 is A's own call
            self.calls.append("step0")
            return {"sql": self.step0, "rationale": "scripted", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def pinned(settings):
    return settings.model_copy(update={"today": date(2026, 10, 4)})


@pytest.fixture
def builder(engine):
    tools = ToolContext.create(engine)
    return ContextBuilder.from_database(engine, tools.catalog, today=date(2026, 10, 4))


def run_c(script, pinned, engine, builder,
          question="Who was our top 2024 customer and their 2025 revenue?",
          **overrides):
    cfg = pinned.model_copy(update=overrides) if overrides else pinned
    return run_architecture(
        "router", question,
        settings=cfg, llm=FakeClient(responder=script),
        tools=ToolContext.create(engine), context_builder=builder,
    )


class TestDirectPath:
    def test_clean_step0_answers_directly_like_a(self, pinned, engine, builder):
        script = Script(step0="SELECT COUNT(*) AS n FROM customers")
        out = run_c(script, pinned, engine, builder)
        assert out.path == "direct" and out.status == "done"
        assert script.calls == ["step0", "decision", "answer"]
        assert out.selected.rows == [[1400]] and out.selected_step == 1
        assert out.repairs == ()


class TestRepair:
    def test_error_is_repaired_and_the_fix_accepted(self, pinned, engine, builder):
        script = Script(step0="SELECT nope FROM customers",
                        repairs=["SELECT COUNT(*) AS n FROM customers"])
        out = run_c(script, pinned, engine, builder)
        assert out.path == "escalated"
        (repair,) = out.repairs
        assert repair.signal == "error" and repair.accepted
        assert "no such column" in repair.detail
        assert not repair.before.ok and repair.after.rows == [[1400]]
        assert out.selected.rows == [[1400]]

    def test_schema_check_catches_a_silent_string_literal(self, pinned, engine, builder):
        # SQLite would treat "statuss" as a string and quietly return 0.
        script = Script(
            step0="SELECT COUNT(*) AS n FROM orders WHERE \"statuss\" = 'completed'",
            repairs=["SELECT COUNT(*) AS n FROM orders WHERE status = 'completed'"])
        out = run_c(script, pinned, engine, builder)
        (repair,) = out.repairs
        assert repair.signal == "schema" and repair.accepted
        assert out.selected.rows == [[7746]]

    def test_a_fix_that_does_not_resolve_the_signal_is_rejected(
        self, pinned, engine, builder
    ):
        script = Script(step0="SELECT * FROM customers WHERE segment = 'Nope'",
                        repairs=["SELECT * FROM customers WHERE segment = 'Still nope'",
                                 "SELECT COUNT(*) AS n FROM customers"])
        out = run_c(script, pinned, engine, builder)
        first, second = out.repairs
        assert first.signal == "empty" and not first.accepted
        assert second.accepted and out.selected.rows == [[1400]]

    def test_attempts_are_capped_and_the_original_result_is_kept(
        self, pinned, engine, builder
    ):
        script = Script(step0="SELECT * FROM customers WHERE segment = 'Nope'",
                        repairs=["SELECT * FROM customers WHERE segment = 'A'",
                                 "SELECT * FROM customers WHERE segment = 'B'"])
        out = run_c(script, pinned, engine, builder, max_attempts_per_step=3)
        assert len(out.repairs) == 2 and script.calls.count("repair") == 2
        assert all(not r.accepted for r in out.repairs)
        assert "segment = 'Nope'" in out.selected.sql

    def test_resubmitting_the_same_query_is_not_a_fix(self, pinned, engine, builder):
        bad = "SELECT nope FROM customers"
        script = Script(step0=bad, repairs=[bad, bad])
        out = run_c(script, pinned, engine, builder)
        assert all(r.after is None and not r.accepted for r in out.repairs)
        assert out.status == "failed"


class TestFollowupsAndAssembly:
    def test_multi_step_answer_is_written_from_one_assembled_table(
        self, pinned, engine, builder
    ):
        plan = {"base_step": 2, "columns": [{"step": 1, "column": "customer_id"},
                                            {"step": 1, "column": "customer_name"}]}
        script = Script(step0=TOP_2024, needs_followup=True,
                        followups=[REVENUE_2025], plan=plan)
        out = run_c(script, pinned, engine, builder)
        assert script.calls == ["step0", "decision", "followup", "followup", "assemble",
                                "answer"]
        assert out.path == "escalated" and out.selected_step == 2
        assert out.selected.columns == ["revenue_2025", "customer_id", "customer_name"]
        assert out.selected.rows[0][1:] == [1052, "Elena Dubois"]
        assert out.selected.rows[0][0] == pytest.approx(587.808)
        # The answer is written from the assembled table, not the steps.
        assert "Elena Dubois" in script.answer_prompt
        assert out.steps[-1]["tools"] == ["assemble"]

    def test_follow_ups_respect_max_steps(self, pinned, engine, builder):
        script = Script(step0="SELECT 1 AS a", needs_followup=True,
                        followups=["SELECT 2 AS b", "SELECT 3 AS c", "SELECT 4 AS d"],
                        plan={"base_step": 3, "columns": []})
        out = run_c(script, pinned, engine, builder, max_steps=3)
        assert script.calls.count("followup") == 2
        assert [s["step"] for s in out.steps if s["step"] != "final"] == [1, 2, 3]

    def test_invalid_assembly_plan_falls_back(self, pinned, engine, builder):
        script = Script(step0=TOP_2024, needs_followup=True,
                        followups=[REVENUE_2025],
                        plan={"base_step": 9, "columns": []})
        out = run_c(script, pinned, engine, builder)
        assert out.selected_step == 2
        assert "customer_name" in out.selected.columns


class TestAssembleUnit:
    def result(self, columns, rows, sql="SELECT 1"):
        return ExecutionResult(ok=True, sql=sql, columns=columns, rows=rows,
                               row_count=len(rows))

    def test_multi_row_values_become_one_column_per_label(self):
        base = self.result(["category", "revenue"], [["A", 1.0], ["B", 2.0]],
                           sql="SELECT category, revenue FROM t ORDER BY revenue DESC")
        aov = self.result(["segment", "aov"], [["Enterprise", 10.0], ["SMB", 9.0]])
        table = assemble(
            AssemblyPlan(base_step=2, columns=[AssemblyColumn(step=1, column="aov")]),
            {1: aov, 2: base})
        assert table.columns == ["category", "revenue", "Enterprise_aov", "SMB_aov"]
        assert table.rows == [["A", 1.0, 10.0, 9.0], ["B", 2.0, 10.0, 9.0]]
        assert table.sql.startswith("-- assembled: rows from step 2")
        assert "ORDER BY revenue DESC" in table.sql

    def test_unknown_columns_are_ignored(self):
        base = self.result(["x"], [[1]])
        table = assemble(AssemblyPlan(base_step=1, columns=[
            AssemblyColumn(step=2, column="nope")]), {1: base, 2: base})
        assert table.columns == ["x"]


class TestChecks:
    def test_schema_error_and_classification(self, engine):
        from agentcrew.db.introspect import build_catalog

        schema = schema_of(build_catalog(engine))
        assert schema_error("SELECT nope FROM orders", schema)
        assert schema_error("SELECT status FROM orders", schema) is None
        ok = ExecutionResult(ok=True, sql="", columns=["n"], rows=[[1]], row_count=1)
        assert classify(ok, None, 500) is None
        assert classify(ok, "bad column", 500).kind == "schema"
        empty = ExecutionResult(ok=True, sql="", columns=["n"], rows=[], row_count=0)
        assert classify(empty, None, 500).kind == "empty"
        nulls = ExecutionResult(ok=True, sql="", columns=["n"], rows=[[None]],
                                row_count=1)
        assert classify(nulls, None, 500).kind == "sanity"
        capped = ExecutionResult(ok=True, sql="", columns=["n"], rows=[[1]] * 5,
                                 row_count=5)
        assert classify(capped, None, 5).kind == "sanity"
        failed = ExecutionResult(ok=False, sql="", error_message="boom")
        assert classify(failed, "bad column", 500).kind == "error"


class TestStep0IsA:
    QUESTION = "How many customers do we have?"

    def test_step0_and_direct_answer_are_byte_identical_to_a(
        self, pinned, engine, builder
    ):
        from test_archs import Scripted

        a_client = FakeClient(responder=Scripted("SELECT COUNT(*) AS n FROM customers"))
        run_architecture("baseline", self.QUESTION, settings=pinned, llm=a_client,
                         tools=ToolContext.create(engine), context_builder=builder)
        step0 = FakeClient(responder=Script(step0="SELECT COUNT(*) AS n FROM customers"))
        main = FakeClient(responder=Script(step0="unused"))
        out = run_architecture("router", self.QUESTION, settings=pinned, llm=main,
                               tools=ToolContext.create(engine),
                               context_builder=builder, step0_llm=step0)
        assert out.path == "direct"
        # The step-0 client saw exactly A's two prompts (SQL, then answer).
        assert step0.prompts == a_client.prompts
        # The main client only made the follow-up decision.
        assert len(main.prompts) == 1
        assert "You check whether one query result" in main.prompts[0][0]

    def test_fanout_escalates_instead_of_answering_directly(
        self, pinned, engine, builder
    ):
        naive = ("SELECT SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS rev, "
                 "SUM(m.amount) AS spend FROM regions r "
                 "JOIN customers c ON c.region_id = r.region_id "
                 "JOIN orders o ON o.customer_id = c.customer_id "
                 "JOIN order_items oi ON oi.order_id = o.order_id "
                 "JOIN marketing_spend m ON m.region_id = r.region_id "
                 "WHERE r.region_name = 'LATAM'")
        fixed = ("SELECT (SELECT SUM(amount) FROM marketing_spend m "
                 "JOIN regions r ON r.region_id = m.region_id "
                 "WHERE r.region_name = 'LATAM') AS spend")
        script = Script(step0=naive, repairs=[fixed])
        out = run_c(script, pinned, engine, builder)
        (repair,) = out.repairs
        assert repair.signal == "fanout" and repair.accepted
        assert "not unique on either side" in repair.detail
        assert out.path == "escalated"


class TestEverySqlCallGetsTheFullContext:
    """Step 0, follow-ups and repairs all see exactly A's shared context."""

    def test_follow_up_and_repair_prompts_carry_a_context(
        self, pinned, engine, builder
    ):
        question = "Who was our top 2024 customer and their 2025 revenue?"
        plan = {"base_step": 2, "columns": []}
        script = Script(step0=TOP_2024, needs_followup=True,
                        followups=["SELECT nope FROM orders"],
                        repairs=[REVENUE_2025], plan=plan)
        client = FakeClient(responder=script)
        run_architecture("router", question, settings=pinned, llm=client,
                         tools=ToolContext.create(engine), context_builder=builder)
        context = builder.build(question).render()
        sql_prompts = [
            user for system, user in client.prompts
            if "SQL analyst" in system or "continuing an analysis" in system
            or "You fix one SQLite query" in system
        ]
        kinds = [s for s, _ in client.prompts]
        assert any("You fix one SQLite query" in s for s in kinds)  # a repair ran
        assert len(sql_prompts) >= 3  # step 0, a follow-up and a repair
        for user in sql_prompts:
            assert context in user  # schema, values, dictionary, date, examples
            assert f"## Question\n{question}\n" in user
            for section in ("## Database schema", "## Allowed values",
                            "## Data dictionary", "Metric definitions:",
                            "## Today's date\n2026-10-04"):
                assert section in user


FORECAST = ("Project monthly signups for the next three months from a linear "
            "trend over 2024.")


class TestForecastSignal:
    """A deterministic, zero-LLM escalation signal for questions that ask for
    a forecast, a projection, a trend line or a regression fit."""

    @pytest.mark.parametrize("question", [
        FORECAST,
        "Forecast completed revenue for next quarter.",
        "What is the projected number of orders in March 2026?",
        "Extrapolate the 2025 ticket counts to 2026.",
        "Fit a trend line to monthly spend and give its slope.",
        "Fit a straight-line trend to monthly units.",
        "Run a linear regression of revenue on marketing spend.",
        "Using least-squares, what is the line of best fit for orders by month?",
    ])
    def test_forecast_questions_signal(self, question):
        signal = forecast_signal(question)
        assert signal is not None and signal.kind == "forecast"

    @pytest.mark.parametrize("question", [
        "What is the Pearson correlation between monthly spend and revenue?",
        "Which months were more than 2 standard deviations from the monthly mean?",
        "How did revenue trend during 2024?",
        "What was the median completed order value in 2025?",
        "Who was our top customer in 2024?",
    ])
    def test_other_questions_do_not(self, question):
        assert forecast_signal(question) is None

    def test_forecast_escalates_without_a_decision_call(self, pinned, engine, builder):
        script = Script(step0="SELECT COUNT(*) AS n FROM customers")
        out = run_c(script, pinned, engine, builder, question=FORECAST)
        assert out.path == "escalated"
        assert "decision" not in script.calls
        assert script.calls[:2] == ["step0", "followup"]
        assert out.plan and "forecast" in out.plan[0]
        assert out.steps[0]["escalation_signal"] == "forecast"
