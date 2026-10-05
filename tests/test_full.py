"""Architecture D (full): direct path = A, escalation with exploration,
compute tools, verification and budgets. Offline on scripted models."""

from __future__ import annotations

import json
from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.explore import MetricPlan
from agentcrew.archs.full import METRIC_GUIDANCE, driver_sentence, is_why_question
from agentcrew.compute.metrics import COLUMNS
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext

COUNT = "SELECT COUNT(*) AS n FROM customers"
BAD_AOV = ("SELECT o.channel, SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) "
           "AS total_revenue, COUNT(DISTINCT o.order_id) AS order_count, "
           "AVG(oi.quantity * oi.unit_price * (1 - oi.discount)) AS avg_order_value "
           "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
           "WHERE o.status = 'completed' GROUP BY o.channel")
GOOD_AOV = ("SELECT o.channel, SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) "
            "AS total_revenue, COUNT(DISTINCT o.order_id) AS order_count, "
            "SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) "
            "/ COUNT(DISTINCT o.order_id) AS avg_order_value "
            "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
            "WHERE o.status = 'completed' GROUP BY o.channel")
DECOMPOSE = {
    "action": "compute", "function": "decompose_change", "reason": "split it",
    "params": {"metric": "revenue", "comparison": {
        "group_dimension": "channel", "group_a": "Partner", "group_b": "Direct",
        "period": {"start": "2024-01-01", "end": "2024-12-31"}}},
}


METRIC_PLAN = {"metric": "revenue", "reason": "split revenue", "comparison": {
    "group_dimension": "channel", "groups": ["Direct", "Partner"],
    "period": {"start": "2024-01-01", "end": "2024-12-31"}}}


class Script:
    def __init__(self, *, step0=COUNT, needs_followup=False, actions=(),
                 repairs=(), plan=None, metric_plans=(METRIC_PLAN,),
                 answer="Scripted.", trend_plans=()):
        self.step0, self.needs_followup = step0, needs_followup
        self.answer = answer
        self.trend_plans = list(trend_plans)
        self.actions, self.repairs = list(actions), list(repairs)
        self.plan = plan or {"base_step": 1, "columns": []}
        self.metric_plans = list(metric_plans)
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    def __call__(self, system: str, user: str):
        def note(kind):
            self.calls.append(kind)
            self.prompts[kind] = user

        if "You check whether one query result" in system:
            note("decision")
            return {"needs_followup": self.needs_followup, "reason": "needs a breakdown"}
        if "asks for a forecast, a projection or a fitted trend" in system:
            note("trend_plan")
            return self.trend_plans.pop(0) if self.trend_plans else {}
        if "You start the analysis of a question that asks why" in system:
            note("metric_plan")
            return self.metric_plans.pop(0) if self.metric_plans else {}
        if "strict" in system and "checker" in system:
            note("judge")
            return {"verdict": "no", "quote": "", "reason": "scripted"}
        if "You plan the next step" in system:
            note("decide")
            return self.actions.pop(0) if self.actions else {"action": "answer"}
        if "You fix one SQLite query" in system:
            note("repair")
            return {"sql": self.repairs.pop(0), "rationale": "x", "expected_shape": ""}
        if "Several queries were run" in system:
            note("assemble")
            return self.plan
        if "final answer" in system:
            note("answer")
            return {"answer": self.answer, "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            note("step0")
            return {"sql": self.step0, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def pinned(settings):
    return settings.model_copy(update={"today": date(2026, 10, 4)})


@pytest.fixture
def builder(engine):
    tools = ToolContext.create(engine)
    return ContextBuilder.from_database(engine, tools.catalog, today=date(2026, 10, 4))


QUESTION = "How did Direct revenue compare with Partner revenue in 2024?"
WHY = "Why does Direct bring in more revenue than Partner in 2024?"


def run_d(script, pinned, engine, builder, question=QUESTION, **overrides):
    cfg = pinned.model_copy(update=overrides) if overrides else pinned
    client = FakeClient(responder=script)
    out = run_architecture("full", question, settings=cfg, llm=client,
                           tools=ToolContext.create(engine), context_builder=builder)
    return out, client


class TestDirectPath:
    def test_direct_path_is_a(self, pinned, engine, builder):
        script = Script()
        out, _ = run_d(script, pinned, engine, builder)
        assert out.path == "direct" and out.arch == "full"
        assert script.calls == ["step0", "decision", "answer"]
        assert out.selected.rows == [[1400]]

    def test_no_verification_on_the_direct_path(self, pinned, engine, builder):
        script = Script(step0=BAD_AOV)
        out, _ = run_d(script, pinned, engine, builder)
        assert out.path == "direct" and out.repairs == ()

    def test_c_identical_calls_use_the_shared_client(self, pinned, engine, builder):
        own = FakeClient(responder=Script(needs_followup=True))
        shared = FakeClient(responder=Script(needs_followup=True))
        step0 = FakeClient(responder=Script())
        run_architecture("full", QUESTION, settings=pinned, llm=own,
                         tools=ToolContext.create(engine), context_builder=builder,
                         step0_llm=step0, shared_llm=shared)
        assert len(step0.prompts) == 1  # A's step 0 only (escalated answer is D's)
        assert ["You check whether one query result" in s for s, _ in shared.prompts] \
            == [True]
        assert all("You check whether" not in s for s, _ in own.prompts)


class TestEscalation:
    def test_decompose_then_answer_from_the_assembled_table(
        self, pinned, engine, builder
    ):
        script = Script(needs_followup=True, actions=[DECOMPOSE],
                        plan={"base_step": 2, "columns": []})
        out, _ = run_d(script, pinned, engine, builder)
        assert out.path == "escalated"
        assert script.calls == ["step0", "decision", "decide", "decide", "assemble",
                                "answer"]
        assert [s["tools"] for s in out.steps] == [
            ["run_sql"], ["compute:decompose_change"], ["assemble"]]
        assert "item" in out.selected.columns
        assert out.selected_step == 2

    def test_decide_sees_the_full_context_question_and_history(
        self, pinned, engine, builder
    ):
        script = Script(needs_followup=True, actions=[DECOMPOSE])
        run_d(script, pinned, engine, builder)
        prompt = script.prompts["decide"]
        assert builder.build(QUESTION).render() in prompt
        assert f"## Question\n{QUESTION}\n" in prompt
        assert "Step 1 [run_sql]" in prompt and "Step 2 [compute:decompose_change]" in prompt

    def test_max_steps_is_respected(self, pinned, engine, builder):
        sample = {"action": "sample_rows", "table": "orders"}
        script = Script(needs_followup=True, actions=[sample] * 5)
        out, _ = run_d(script, pinned, engine, builder, max_steps=3)
        assert script.calls.count("decide") == 2
        assert len([s for s in out.steps if s["step"] != "final"]) == 3

    def test_reconciliation_failure_on_step0_is_repaired(self, pinned, engine, builder):
        script = Script(step0=BAD_AOV, needs_followup=True, repairs=[GOOD_AOV])
        out, _ = run_d(script, pinned, engine, builder)
        (repair,) = out.repairs
        assert repair.signal == "reconcile" and repair.accepted
        assert "should equal" in repair.detail

    def test_compute_errors_are_observations(self, pinned, engine, builder):
        bad = {"action": "compute", "function": "share_of_total",
               "params": {"step": 9, "value": "n", "label": "x"}}
        script = Script(needs_followup=True, actions=[bad])
        out, _ = run_d(script, pinned, engine, builder)
        assert out.status == "done"
        assert out.selected.rows == [[1400]]  # the failed compute is not selected

    def test_search_values_finds_a_misspelled_name(self, pinned, engine, builder):
        search = {"action": "search_values", "term": "Cobolt Nimbus"}
        script = Script(needs_followup=True, actions=[search])
        run_d(script, pinned, engine, builder)
        assert "Cobalt Nimbus" in script.prompts["decide"]

    def test_invalid_actions_end_the_loop_gracefully(self, pinned, engine, builder):
        script = Script(needs_followup=True,
                        actions=[{"action": "run_sql"}, {"action": "run_sql"}])
        out, _ = run_d(script, pinned, engine, builder)
        assert out.status == "done" and out.selected.rows == [[1400]]


COMPONENTS = ("number of orders", "average order value", "units sold",
              "average unit price", "1 - average discount")


class TestWhyQuestions:
    """A why-question's escalated path starts with a metric decomposition, and
    the answer is written from the stacked metric tables."""

    @pytest.mark.parametrize("question", [
        WHY, "What explains the drop in tickets?", "what drove Q2 sales",
        "Reasons for the lower refund rate?", "How come APAC fell?",
        "What caused churn to rise in 2025?",
    ])
    def test_why_questions_are_recognised(self, question):
        assert is_why_question(question)

    @pytest.mark.parametrize("question", [
        QUESTION, "Which region had the most orders?",
        "What is the difference between Direct and Partner revenue?",
    ])
    def test_other_questions_are_not(self, question):
        assert not is_why_question(question)

    def test_why_starts_with_a_decomposition_and_stacks(self, pinned, engine, builder):
        script = Script(needs_followup=True)
        out, _ = run_d(script, pinned, engine, builder, question=WHY)
        assert script.calls == ["step0", "decision", "metric_plan", "decide", "answer"]
        assert [s["tools"] for s in out.steps] == [
            ["run_sql"], ["compute:decompose_change"], ["stack"]]
        assert tuple(out.selected.columns) == COLUMNS
        assert out.selected_step == 2

    def test_answer_reports_every_component(self, pinned, engine, builder):
        script = Script(needs_followup=True)
        run_d(script, pinned, engine, builder, question=WHY)
        prompt = script.prompts["answer"]
        assert METRIC_GUIDANCE in prompt
        for item in COMPONENTS:
            assert item in prompt

    def test_metric_and_contribution_tables_are_stacked(self, pinned, engine, builder):
        contribution = {
            "action": "compute", "function": "contribution_by_dimension",
            "params": {"metric": "revenue", "dimension": "segment",
                       "comparison": METRIC_PLAN["comparison"]}}
        script = Script(needs_followup=True, actions=[contribution])
        out, _ = run_d(script, pinned, engine, builder, question=WHY)
        assert "assemble" not in script.calls
        assert [s["tools"] for s in out.steps][-1] == ["stack"]
        analyses = {row[COLUMNS.index("analysis")] for row in out.selected.rows}
        assert "revenue by segment" in analyses
        assert "orders x average order value" in analyses

    def test_failed_metric_plan_falls_back_to_the_loop(self, pinned, engine, builder):
        script = Script(needs_followup=True, actions=[DECOMPOSE],
                        metric_plans=[{"metric": "profit"}, {"metric": "profit"}],
                        plan={"base_step": 2, "columns": []})
        out, _ = run_d(script, pinned, engine, builder, question=WHY)
        assert out.status == "done"
        assert script.calls.count("metric_plan") == 2  # one schema repair
        assert [s["tools"] for s in out.steps][1] == ["compute:decompose_change"]

    def test_failed_metric_step_uses_ordinary_selection(self, pinned, engine, builder):
        bad = {**METRIC_PLAN, "comparison": {
            "group_dimension": "channel", "groups": ["Nowhere", "Direct"]}}
        script = Script(needs_followup=True, metric_plans=[bad])
        out, _ = run_d(script, pinned, engine, builder, question=WHY)
        assert out.status == "done" and out.selected.rows == [[1400]]
        assert METRIC_GUIDANCE not in script.prompts["answer"]

    def test_metric_sql_goes_through_the_read_only_guard(
        self, pinned, engine, builder, monkeypatch
    ):
        import agentcrew.tools as tools_module

        seen: list[str] = []
        real = tools_module.guard_sql

        def spy(sql, **kwargs):
            seen.append(sql)
            return real(sql, **kwargs)

        monkeypatch.setattr(tools_module, "guard_sql", spy)
        out, _ = run_d(Script(needs_followup=True), pinned, engine, builder,
                       question=WHY)
        metric_sql = [line for line in out.selected.sql.splitlines()
                      if line.startswith("SELECT")]
        assert metric_sql and all(sql in seen for sql in metric_sql)


class TestMetricPlan:
    def test_metric_names_are_allowlisted(self):
        with pytest.raises(ValueError, match="unknown metric"):
            MetricPlan(metric="profit", comparison=METRIC_PLAN["comparison"])
        with pytest.raises(ValueError, match="unknown metric"):
            MetricPlan(metric="revenue); DROP TABLE orders; --",
                       comparison=METRIC_PLAN["comparison"])

    def test_decomposable_metrics_use_decompose_change(self):
        action = MetricPlan(**METRIC_PLAN).to_action()
        assert (action.action, action.function) == ("compute", "decompose_change")
        assert action.params["comparison"]["groups"] == ["Direct", "Partner"]

    def test_metrics_without_components_need_a_dimension(self):
        with pytest.raises(ValueError, match="dimension"):
            MetricPlan(metric="tickets", comparison=METRIC_PLAN["comparison"])
        action = MetricPlan(metric="tickets", dimension="severity",
                            comparison=METRIC_PLAN["comparison"]).to_action()
        assert action.function == "contribution_by_dimension"
        assert action.params["dimension"] == "severity"


def test_runner_reports_tools_and_counts_all_clients(tmp_path, db_path):
    from dataclasses import replace

    from evalrun.runner import RunConfig, Runner

    script = Script(needs_followup=True, actions=[DECOMPOSE])
    cfg = replace(RunConfig.load(), cache_dir=tmp_path / "cache")
    runner = Runner(cfg, live_factory=lambda s: FakeClient(responder=script),
                    log=lambda *_: None)
    payload = runner.run("showcase", ["full"], tmp_path / "run")
    item = payload["items"][0]
    assert item["tools"][1] == ["compute:decompose_change"]
    trace = json.loads((tmp_path / "run" / item["trace"]).read_text("utf-8"))
    names = {c["cache_namespace"] for c in trace["llm_calls"]}
    assert names == {"full", "baseline (step 0)", "router (shared with C)"}
    assert item["llm_calls"] == len(trace["llm_calls"])


def test_answer_prompt_without_guidance_is_unchanged():
    """A's and C's answer prompts (and so their cached responses) must not
    change because D can append reading guidance."""
    from agentcrew.archs.answer import ANSWER_ROWS, ANSWER_USER, write_answer
    from agentcrew.schemas import ExecutionResult

    client = FakeClient(responses=[{"answer": "x", "key_numbers": [], "caveats": []}])
    result = ExecutionResult(ok=True, sql="SELECT 1", columns=["n"], rows=[[1]],
                             row_count=1)
    write_answer(client, "Q?", result, max_tokens=100, temperature=0.0)
    ((_, user),) = client.prompts
    assert user == ANSWER_USER.format(question="Q?", sql="SELECT 1", row_count=1,
                                      truncated="",
                                      preview=result.preview(limit=ANSWER_ROWS))


SERIES = ("SELECT substr(signup_date, 1, 7) AS month, COUNT(*) AS signups "
          "FROM customers WHERE signup_date BETWEEN '2023-01-01' AND '2024-12-31' "
          "GROUP BY month")
FORECAST_Q = "Project monthly signups for the first two months of 2025 from a linear trend."


def test_without_a_trend_plan_the_loop_can_still_reach_the_tools(
    pinned, engine, builder
):
    forecast = {"action": "compute", "function": "linear_trend_forecast",
                "params": {"step": 2, "value": "signups", "order": "month",
                           "periods_ahead": 2}}
    script = Script(actions=[{"action": "run_sql", "sql": SERIES}, forecast],
                    plan={"base_step": 3, "columns": []})
    out, _ = run_d(script, pinned, engine, builder, question=FORECAST_Q)
    assert out.path == "escalated" and "decision" not in script.calls
    assert out.repairs == ()  # the 2023-2024 fit window is not a date-range error
    assert [s["tools"] for s in out.steps][:3] == [
        ["run_sql"], ["run_sql"], ["compute:linear_trend_forecast"]]
    assert out.selected.columns[0] == "month"
    assert [r[0] for r in out.selected.rows] == ["2025-01", "2025-02"]


def _stacked(rows):
    from agentcrew.schemas import ExecutionResult

    return ExecutionResult(ok=True, sql="-- stacked", columns=list(COLUMNS),
                           rows=rows, row_count=len(rows))


class TestDriverSentence:
    """A why-answer always states the main driver in plain words."""

    ROWS = [
        ["B", "A", "metric", "revenue", 610000.0, 900000.0, 290000.0, 47.5, None,
         "higher", False],
        ["B", "A", "orders x average order value", "number of orders", 200.0, 300.0,
         100.0, 50.0, 104.2, "higher", True],
        ["B", "A", "orders x average order value", "average order value", 3050.0,
         3000.0, -50.0, -1.64, -4.2, "about the same", False],
        ["C", "A", "orders x average order value", "average order value", 2000.5,
         3000.0, 999.5, 49.96, 98.0, "higher", True],
        ["B", "A", "revenue by region", "APAC", 1.0, 2.0, 1.0, 100.0, 90.0,
         "higher", True],
    ]

    def test_sentence_names_the_first_analysis_driver_per_pair(self):
        assert driver_sentence(_stacked(self.ROWS)) == (
            "Main driver (largest contributor): number of orders, A 300 vs B 200 "
            "(+50.0%); average order value, A 3,000 vs C 2,000.50 (+50.0%).")

    def test_breakdown_driver_names_its_dimension(self):
        rows = [r for r in self.ROWS if r[2] in ("metric", "revenue by region")]
        assert driver_sentence(_stacked(rows)) == (
            "Main driver (largest contributor): APAC (revenue by region), "
            "A 2 vs B 1 (+100.0%).")

    def test_added_when_the_answer_leaves_out_the_driver(self, pinned, engine, builder):
        out, _ = run_d(Script(needs_followup=True), pinned, engine, builder,
                       question=WHY)
        assert out.answer.startswith("Main driver (largest contributor): ")
        assert out.answer.endswith(" Scripted.")
        assert out.steps[-1]["driver_sentence_added"] is True

    def test_not_added_when_the_answer_names_it(self, pinned, engine, builder):
        out, _ = run_d(Script(needs_followup=True), pinned, engine, builder,
                       question=WHY)
        named = out.answer.split(": ", 1)[1].split(",", 1)[0]
        script = Script(needs_followup=True,
                        answer=f"The {named.upper()} explains it.")
        again, _ = run_d(script, pinned, engine, builder, question=WHY)
        assert again.answer == f"The {named.upper()} explains it."
        assert again.steps[-1]["driver_sentence_added"] is False

    def test_guidance_asks_for_plain_words_and_the_driver_first(self):
        assert "plain words" in METRIC_GUIDANCE
        assert "First sentence: the main driver" in METRIC_GUIDANCE



def test_forced_escalation_escalates_a_clean_question(pinned, engine, builder):
    from agentcrew.archs.full import FORCED_REASON

    script = Script()  # clean step 0, follow-up decision says no
    client = FakeClient(responder=script)
    out = run_architecture("full", QUESTION, settings=pinned, llm=client,
                           tools=ToolContext.create(engine), context_builder=builder,
                           force_escalation=True)
    assert out.path == "escalated"
    assert script.calls == ["step0", "decision", "decide", "answer"]
    assert out.plan == (FORCED_REASON,)
    assert FORCED_REASON in script.prompts["decide"]
    normal, _ = run_d(Script(), pinned, engine, builder)
    assert normal.path == "direct"  # the switch is off by default



TREND_PLAN = {"series_sql": SERIES, "period": "month", "value": "signups",
              "function": "linear_trend_forecast", "periods": ["2025-01", "2025-02"],
              "reason": "fit the 2023-2024 series"}


class TestTrendPlan:
    """A forecast / trend-fit question opens with a TrendPlan: the series
    query, then the deterministic fit, whose table is the final result."""

    def test_forecast_opens_with_the_series_and_the_fit(self, pinned, engine, builder):
        script = Script(trend_plans=[TREND_PLAN])
        out, _ = run_d(script, pinned, engine, builder, question=FORECAST_Q)
        assert script.calls == ["step0", "trend_plan", "answer"]
        assert [s["tools"] for s in out.steps] == [
            ["run_sql"], ["run_sql"], ["compute:linear_trend_forecast"]]
        assert out.selected_step == 3
        cols = out.selected.columns
        assert cols[0] == "month" and "x_convention" in cols
        rows = out.selected.rows
        assert [r[0] for r in rows] == ["2025-01", "2025-02"]
        assert rows[0][cols.index("x_convention")] == "x = month number, 1 = 2023-01"
        assert rows[0][cols.index("x")] == 25

    def test_model_style_plan_with_wrong_case_and_extra_fields(
        self, pinned, engine, builder
    ):
        plan = {**TREND_PLAN, "function": "Linear_Trend_Forecast", "value": "SIGNUPS",
                "periods": "2025-03", "confidence": "high", "threshold": None}
        out, _ = run_d(Script(trend_plans=[plan]), pinned, engine, builder,
                       question=FORECAST_Q)
        assert [r[0] for r in out.selected.rows] == ["2025-03"]

    def test_trend_anomalies_against_the_line(self, pinned, engine, builder):
        plan = {"series_sql": SERIES, "period": "month", "value": "signups",
                "function": "zscore_anomalies", "threshold": 1.5,
                "direction": "Both"}
        out, _ = run_d(Script(trend_plans=[plan]), pinned, engine, builder,
                       question="Which months sit far from the trend line of signups?")
        assert [s["tools"] for s in out.steps][-1] == ["compute:zscore_anomalies"]
        assert "x_convention" in out.selected.columns

    def test_a_failing_series_query_falls_back_to_the_loop(
        self, pinned, engine, builder
    ):
        bad = {**TREND_PLAN, "series_sql": "SELECT nope FROM customers"}
        script = Script(trend_plans=[bad], repairs=["SELECT nope2 FROM customers"] * 2)
        out, _ = run_d(script, pinned, engine, builder, question=FORECAST_Q)
        assert out.status == "done" and out.selected.rows == [[1400]]
        assert "decide" in script.calls  # the ordinary loop took the last step

    def test_a_plan_without_periods_is_repaired_or_dropped(self):
        from agentcrew.archs.explore import TrendPlan

        with pytest.raises(ValueError, match="periods"):
            TrendPlan(series_sql=SERIES, period="month", value="signups")
        with pytest.raises(ValueError):
            TrendPlan(series_sql=SERIES, period="month", value="signups",
                      function="moving_average", periods_ahead=1)
