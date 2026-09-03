"""End-to-end graph behaviour.

Every test here runs the real graph, real schema selection, real SQL against
the real database, and a scripted analyst in place of the model. Only the
model is fake - the control flow under test is production code.
"""

from __future__ import annotations

from conftest import ScriptedAnalyst

from agentcrew.graph.build import run_question

CHURN_SQL = """
SELECT r.region_name, COUNT(*) AS churned
FROM customers c JOIN regions r ON c.region_id = r.region_id
WHERE c.churn_date >= '2025-07-01' AND c.churn_date <= '2025-09-30'
GROUP BY r.region_name ORDER BY churned DESC
"""

REVENUE_SQL = """
SELECT SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS revenue
FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status = 'completed'
"""

GOAL = "count churned customers per region"


def _run(analyst: ScriptedAnalyst, make_deps, question: str, **overrides):
    deps = make_deps(analyst, **overrides)
    return run_question(question, settings=None, deps=deps, persist_trace=True)


class TestHappyPath:
    def test_single_step_question_succeeds(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL], sql_by_goal={GOAL: [CHURN_SQL]}
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.succeeded
        assert out.status == "done"
        assert len(out.step_results) == 1
        assert out.step_results[0].verified
        # The agent found the planted signal.
        assert out.step_results[0].result.rows[0][0] == "APAC"
        assert analyst.calls == ["plan", "author", "verify", "synthesize"]

    def test_schema_selection_reached_the_right_tables(self, make_deps) -> None:
        analyst = ScriptedAnalyst(plan_steps=[GOAL], sql_by_goal={GOAL: [CHURN_SQL]})
        out = _run(analyst, make_deps, "Which region lost the most customers?")
        assert {"customers", "regions"} <= set(out.selected_tables)

    def test_trace_is_written_and_complete(self, make_deps) -> None:
        analyst = ScriptedAnalyst(plan_steps=[GOAL], sql_by_goal={GOAL: [CHURN_SQL]})
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.trace_path is not None and out.trace_path.exists()
        names = [s["name"] for s in out.trace["spans"]]
        assert names == [
            "plan",
            "select_schema",
            "author_sql",
            "execute_sql",
            "triage",
            "verify",
            "advance",
            "synthesize",
        ]
        assert out.trace["counters"]["llm_calls"] == 4
        assert out.trace["counters"]["sql_executions"] == 1


class TestMultiStep:
    def test_two_step_plan_runs_both_and_synthesises(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=["size the revenue change", "break it down by category"],
            sql_by_goal={
                "size the revenue change": [REVENUE_SQL],
                "break it down by category": [
                    "SELECT p.category, COUNT(*) AS n FROM products p GROUP BY p.category"
                ],
            },
        )
        out = _run(analyst, make_deps, "Why did revenue fall?")

        assert out.succeeded
        assert len(out.step_results) == 2
        assert analyst.calls.count("author") == 2
        assert analyst.calls.count("verify") == 2
        assert analyst.calls.count("synthesize") == 1


class TestRepairLoop:
    def test_repairs_a_missing_column_then_succeeds(self, make_deps) -> None:
        """The core self-correction behaviour: real DB error -> real fix."""
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [
                    "SELECT nonexistent_column FROM customers",  # fails for real
                    CHURN_SQL,  # repaired
                ]
            },
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.succeeded
        assert "repair" in analyst.calls
        assert out.step_results[0].attempts_used == 2
        assert out.trace["counters"]["repairs"] == 1

    def test_repairs_an_empty_result(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [
                    "SELECT * FROM customers WHERE region_id = -999",  # empty
                    CHURN_SQL,
                ]
            },
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")
        assert out.succeeded
        assert "repair" in analyst.calls

    def test_repairs_a_semantically_wrong_query(self, make_deps) -> None:
        """Query runs fine but measures the wrong thing -> verifier catches it."""
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [
                    "SELECT COUNT(*) AS wrong_metric FROM customers",  # runs, wrong
                    CHURN_SQL,
                ]
            },
            verdicts=["wrong_metric", "pass"],
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.succeeded
        assert analyst.calls.count("verify") == 2
        assert out.step_results[0].verified
        assert out.step_results[0].result.rows[0][0] == "APAC"

    def test_blocked_write_is_repaired_not_crashed(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={GOAL: ["DELETE FROM customers", CHURN_SQL]},
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")
        assert out.succeeded
        assert "repair" in analyst.calls


class TestLoopControl:
    def test_identical_repeated_query_aborts_instead_of_looping(
        self, make_deps
    ) -> None:
        """Without fingerprinting this would burn the whole retry budget."""
        bad = "SELECT missing_col FROM customers"
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL], sql_by_goal={GOAL: [bad, bad, bad, bad, bad]}
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.status == "failed"
        assert out.stop_reason == "repeated_query"
        assert "stuck generating the same query" in (out.answer or "")
        # It stopped on the *second* identical query, not after 3+ attempts.
        assert out.trace["counters"]["sql_executions"] == 2

    def test_distinct_failures_exhaust_attempts_gracefully(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [
                    "SELECT bad_a FROM customers",
                    "SELECT bad_b FROM customers",
                    "SELECT bad_c FROM customers",
                    "SELECT bad_d FROM customers",
                ]
            },
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.status == "failed"
        assert out.stop_reason == "attempts_exhausted"
        assert "Last database error" in (out.answer or "")

    def test_never_fabricates_an_answer_on_failure(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [f"SELECT bad_{i} FROM customers" for i in "abcd"]
            },
        )
        _run(analyst, make_deps, "Which region lost the most customers?")
        assert "synthesize" not in analyst.calls

    def test_llm_budget_stops_the_run(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [f"SELECT bad_{i} FROM customers" for i in "abcdefgh"]
            },
        )
        out = _run(
            analyst,
            make_deps,
            "Which region lost the most customers?",
            max_llm_calls=3,
            max_attempts_per_step=10,
        )
        assert out.status == "failed"
        assert out.stop_reason == "llm_budget_exceeded"

    def test_sql_budget_stops_the_run(self, make_deps) -> None:
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [f"SELECT bad_{i} FROM customers" for i in "abcdefgh"]
            },
        )
        out = _run(
            analyst,
            make_deps,
            "Which region lost the most customers?",
            max_sql_executions=2,
            max_attempts_per_step=10,
        )
        assert out.status == "failed"
        assert out.stop_reason == "sql_budget_exceeded"


class TestClarification:
    def test_ambiguous_question_asks_instead_of_guessing(self, make_deps) -> None:
        analyst = ScriptedAnalyst(ambiguous=True)
        out = _run(analyst, make_deps, "How did we do?")

        assert out.status == "needs_clarification"
        assert out.answer == "Which time period do you mean?"
        assert analyst.calls == ["plan"]
        assert out.trace["counters"]["sql_executions"] == 0


class TestUnverifiedFallback:
    def test_persistent_wrong_metric_is_committed_but_flagged(
        self, make_deps
    ) -> None:
        """Out of retries on a semantically wrong result: keep the work, mark
        it unverified, do not silently claim success."""
        analyst = ScriptedAnalyst(
            plan_steps=[GOAL],
            sql_by_goal={
                GOAL: [
                    "SELECT COUNT(*) AS a FROM customers",
                    "SELECT COUNT(*) AS b FROM orders",
                    "SELECT COUNT(*) AS c FROM regions",
                ]
            },
            verdicts=["wrong_metric", "wrong_metric", "wrong_metric"],
        )
        out = _run(analyst, make_deps, "Which region lost the most customers?")

        assert out.status == "done"
        assert out.step_results[0].verified is False
