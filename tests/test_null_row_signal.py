"""The null_row signal: rows with no value in any measure, among rows that have
values, trigger a repair. Offline: a scripted model and the real database."""

from __future__ import annotations

from datetime import date

import pytest

from agentcrew.archs import run_architecture
from agentcrew.archs.checks import classify
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.presentation import escalation_reason
from agentcrew.schemas import ExecutionResult
from agentcrew.tools import ToolContext

BY_CHANNEL = "SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel"
#: A second branch whose WHERE is false still returns one aggregate row.
WITH_EMPTY_BRANCH = (
    f"{BY_CHANNEL} UNION ALL SELECT 'all channels', "
    "SUM(CASE WHEN status = 'nope' THEN 1 END) FROM orders WHERE 1 = 0"
)


def result(columns, rows, sql="SELECT 1"):
    return ExecutionResult(ok=True, sql=sql, columns=columns, rows=rows,
                           row_count=len(rows))


def signal_of(res):
    return classify(res, None, max_rows=500)


class TestClassify:
    def test_a_row_without_any_measure_among_rows_that_have_them(self):
        res = result(["region", "rate"], [["APAC", 16.3], ["EMEA", 17.6], ["ALL", None]])
        found = signal_of(res)
        assert found is not None and found.kind == "null_row"
        assert "ALL" in found.detail

    def test_every_measure_of_the_row_must_be_null(self):
        res = result(["region", "a", "b"], [["APAC", 1, 2], ["EMEA", None, 3]])
        assert signal_of(res) is None

    def test_a_null_label_with_a_value_is_not_a_hollow_row(self):
        res = result(["churn_date", "n"], [["2025-01-01", 3], [None, 40]])
        assert signal_of(res) is None

    def test_text_only_results_have_no_measures(self):
        res = result(["name", "note"], [["a", None], ["b", "x"]])
        assert signal_of(res) is None

    def test_all_null_stays_the_sanity_signal(self):
        res = result(["region", "rate"], [[None, None], [None, None]])
        assert signal_of(res).kind == "sanity"

    def test_rows_with_labels_but_no_measure_at_all_have_no_numeric_column(self):
        res = result(["region", "rate"], [["APAC", None], ["EMEA", None]])
        assert signal_of(res) is None

    @pytest.mark.parametrize("function", ["LAG", "lead"])
    def test_a_window_offset_leaves_null_at_the_edge_by_design(self, function):
        sql = (f"SELECT month, n - {function}(n) OVER (ORDER BY month) AS growth "
               "FROM t")
        res = result(["month", "growth"], [["2025-01", None], ["2025-02", 4]], sql)
        assert signal_of(res) is None

    def test_a_query_error_still_comes_first(self):
        res = ExecutionResult(ok=False, sql="x", error_message="boom")
        assert signal_of(res).kind == "error"

    def test_the_row_label_comes_from_the_row_not_a_fixed_name(self):
        res = result(["region", "rate"], [["APAC", 1.0], ["whatever it is", None]])
        assert "whatever it is" in signal_of(res).detail


class Script:
    def __init__(self, repairs=()):
        self.repairs = list(repairs)
        self.calls: list[str] = []
        self.repair_prompt = ""

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": False, "reason": ""}
        if "You fix one SQLite query" in system:
            self.calls.append("repair")
            self.repair_prompt = user
            return {"sql": self.repairs.pop(0), "rationale": "x", "expected_shape": ""}
        if "You plan the next step" in system:
            self.calls.append("decide")
            return {"action": "answer"}
        if "final answer" in system:
            self.calls.append("answer")
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            self.calls.append("step0")
            return {"sql": WITH_EMPTY_BRANCH, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])


@pytest.fixture
def run(settings, engine):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    tools = ToolContext.create(engine)
    builder = ContextBuilder.from_database(engine, tools.catalog,
                                           today=date(2026, 10, 4))

    def go(arch, script):
        return run_architecture(arch, "How many orders per channel?", settings=pinned,
                                llm=FakeClient(responder=script), tools=tools,
                                context_builder=builder)
    return go


@pytest.mark.parametrize("arch", ["router", "full"])
class TestRepairOfAHollowRow:
    def test_the_repair_is_accepted_when_the_row_is_gone(self, run, arch):
        script = Script(repairs=[BY_CHANNEL])
        out = run(arch, script)
        assert out.path == "escalated"
        assert out.repairs[0].signal == "null_row" and out.repairs[0].accepted
        assert "all channels" in script.repair_prompt
        assert out.selected.row_count == 3
        assert all(row[1] is not None for row in out.selected.rows)

    def test_a_repair_that_keeps_the_row_is_not_accepted(self, run, arch):
        keeps = WITH_EMPTY_BRANCH + " LIMIT 400"
        script = Script(repairs=[keeps, keeps + " "])
        out = run(arch, script)
        assert not any(r.accepted for r in out.repairs)
        assert any(row[0] == "all channels" for row in out.selected.rows)


CLEAN = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
ANOTHER_CLEAN = "SELECT region_name, COUNT(*) AS n FROM regions GROUP BY region_name"


class Exploring(Script):
    """Repairs that keep the hollow row, then follow-up queries that run clean."""

    def __init__(self, followups, plan=None):
        super().__init__(repairs=[WITH_EMPTY_BRANCH + " LIMIT 400",
                                  WITH_EMPTY_BRANCH + " LIMIT 300"])
        self.followups = list(followups)
        self.plan = plan or {"base_step": 1, "columns": []}
        self.assembly_prompt = ""

    def __call__(self, system, user):
        if "You check whether one query result" in system:
            self.calls.append("decision")
            return {"needs_followup": True, "reason": "more is needed"}
        if "You plan the next step" in system:
            self.calls.append("decide")
            if self.followups:
                return {"action": "run_sql", "sql": self.followups.pop(0), "reason": "x"}
            return {"action": "answer"}
        if "continuing an analysis" in system:
            self.calls.append("followup")
            if self.followups:
                return {"done": False, "sql": self.followups.pop(0), "reason": "x"}
            return {"done": True, "sql": "", "reason": ""}
        if "Several queries were run" in system:
            self.calls.append("assemble")
            self.assembly_prompt = user
            return self.plan
        return super().__call__(system, user)


@pytest.mark.parametrize("arch", ["router", "full"])
class TestSelectionAvoidsAHollowTable:
    """Added after a repair kept the row and assembly then chose that table,
    although a later step had run clean."""

    def test_a_clean_table_wins_with_no_assembly_call(self, run, arch):
        script = Exploring([CLEAN])
        out = run(arch, script)
        assert "assemble" not in script.calls
        assert out.selected.columns == ["status", "n"]
        assert out.selected_step == 2

    def test_assembly_sees_only_the_clean_tables(self, run, arch):
        script = Exploring([CLEAN, ANOTHER_CLEAN], plan={"base_step": 1, "columns": []})
        out = run(arch, script)
        assert "assemble" in script.calls
        assert "all channels" not in script.assembly_prompt
        assert not any(row[0] == "all channels" for row in out.selected.rows)

    def test_the_hollow_table_stays_when_nothing_else_exists(self, run, arch):
        out = run(arch, Exploring([]))
        assert any(row[0] == "all channels" for row in out.selected.rows)


def test_a_is_not_changed_by_the_signal(run):
    """A has no repair path: its table keeps the row, which is the baseline."""
    script = Script()
    out = run("baseline", script)
    assert script.calls == ["step0", "answer"] and out.repairs == ()
    assert any(row[0] == "all channels" for row in out.selected.rows)


def test_the_app_can_say_why_it_escalated(run):
    out = run("full", Script(repairs=[BY_CHANNEL]))
    assert "no value" in escalation_reason(out)
