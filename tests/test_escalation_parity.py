"""D escalates on exactly the questions C escalates on.

Protocol v2: D is identical to C until escalation. Two checks:

- scripted scenarios (always run): every way into escalation, through the
  shared rule Router.escalated;
- the real development sets, offline from the local eval cache: step 0 (A's
  cached call) and C's cached follow-up decisions and repairs decide each
  path. Calls after that point that are not cached are scripted, so the test
  never reaches the API; step 0 and the decision must come from the cache.
  Skipped when the cache is absent (CI).
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from agentcrew.archs import run_architecture
from agentcrew.context import ContextBuilder
from agentcrew.llm import FakeClient
from agentcrew.tools import ToolContext

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "eval" / ".llm_cache"

COUNT = "SELECT COUNT(*) AS n FROM customers"
EMPTY = "SELECT customer_id FROM customers WHERE customer_name = 'Nobody'"


def scripted(*, step0=COUNT, needs_followup=False, repair=COUNT):
    def respond(system: str, user: str):
        if "You check whether one query result" in system:
            return {"needs_followup": needs_followup, "reason": "scripted"}
        if "You fix one SQLite query" in system:
            return {"sql": repair, "rationale": "x", "expected_shape": ""}
        if "continuing an analysis" in system:
            return {"done": True, "sql": "", "reason": ""}
        if "You plan the next step" in system:
            return {"action": "answer"}
        if "You start the analysis of a question that asks" in system:
            return {}  # no valid plan: D continues with its ordinary loop
        if "Several queries were run" in system:
            return {"base_step": 1, "columns": []}
        if "final answer" in system:
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        if "SQL analyst" in system:
            return {"sql": step0, "rationale": "x", "expected_shape": ""}
        raise AssertionError(system[:80])
    return respond


@pytest.fixture
def builder(engine):
    tools = ToolContext.create(engine)
    return ContextBuilder.from_database(engine, tools.catalog, today=date(2026, 10, 4))


@pytest.mark.parametrize(("question", "script", "expected"), [
    ("How many customers?", {}, "direct"),
    ("How many customers?", {"needs_followup": True}, "escalated"),
    ("How many customers?", {"step0": "SELECT nope FROM customers"}, "escalated"),
    ("How many customers?", {"step0": EMPTY, "repair": EMPTY + " AND 1 = 1"},
     "escalated"),
    ("Forecast monthly signups for 2026.", {}, "escalated"),
    ("Why do we have so many customers?", {}, "direct"),
])
def test_scripted_paths_match(settings, engine, builder, question, script, expected):
    pinned = settings.model_copy(update={"today": date(2026, 10, 4)})
    paths = {}
    for arch in ("router", "full"):
        out = run_architecture(arch, question, settings=pinned,
                               llm=FakeClient(responder=scripted(**script)),
                               tools=ToolContext.create(engine),
                               context_builder=builder)
        paths[arch] = out.path
    assert paths == {"router": expected, "full": expected}


def _after_escalation(system: str, user: str):
    """Only for calls that come after the escalation decision."""
    if "SQL analyst" in system or "You check whether one query result" in system:
        raise AssertionError("step 0 and the follow-up decision must be cached")
    if "strict" in system and "checker" in system:
        return {"verdict": "no", "quote": "", "reason": "scripted"}
    return scripted()(system, user)


@pytest.mark.skipif(not CACHE.exists(), reason="needs the local eval cache")
@pytest.mark.parametrize("set_name", ["showcase", "dev2"])
def test_d_escalates_exactly_where_c_does_on_cached_runs(set_name, tmp_path):
    from evalrun.runner import RunConfig, Runner
    from evalrun.sets import load_questions

    cache = tmp_path / "cache"
    shutil.copytree(CACHE, cache)  # scripted calls never touch the real cache
    cfg = replace(RunConfig.load(), cache_dir=cache)
    runner = Runner(cfg, live_factory=lambda s: FakeClient(responder=_after_escalation),
                    log=lambda *_: None)
    payload = runner.run(set_name, ["router", "full"], tmp_path / "run")
    paths = {(i["id"], i["arch"]): i["path"] for i in payload["items"]}
    for q in load_questions(set_name):
        c, d = paths[(q["id"], "router")], paths[(q["id"], "full")]
        assert c in ("direct", "escalated"), (q["id"], c)
        assert d == c, (q["id"], c, d)
