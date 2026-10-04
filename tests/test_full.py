"""Architecture D (full): direct path = A, escalation with exploration,
compute tools, verification and budgets. Offline on scripted models."""

from __future__ import annotations

import json
from datetime import date

import pytest

from agentcrew.archs import run_architecture
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


class Script:
    def __init__(self, *, step0=COUNT, needs_followup=False, actions=(),
                 repairs=(), plan=None):
        self.step0, self.needs_followup = step0, needs_followup
        self.actions, self.repairs = list(actions), list(repairs)
        self.plan = plan or {"base_step": 1, "columns": []}
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    def __call__(self, system: str, user: str):
        def note(kind):
            self.calls.append(kind)
            self.prompts[kind] = user

        if "You check whether one query result" in system:
            note("decision")
            return {"needs_followup": self.needs_followup, "reason": "needs a breakdown"}
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
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
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


QUESTION = "Why does Direct bring in more revenue than Partner in 2024?"


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
        assert "component" in out.selected.columns
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
