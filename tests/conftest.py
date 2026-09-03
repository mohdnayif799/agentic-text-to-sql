"""Shared test fixtures.

The `ScriptedAnalyst` is the important piece. It is a rule-based responder
plugged into `FakeClient`, so the *entire* graph - planning, authoring,
execution, triage, repair, verification, synthesis, budgets and loop detection
- runs end to end with no API key and no network, deterministically.

That means the control-flow logic (which is where agent bugs actually live) is
covered by CI, while the parts that genuinely need a model are covered by the
evaluation harness when a key is present.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.db.engine import read_only_engine  # noqa: E402
from agentcrew.graph.build import build_deps  # noqa: E402
from agentcrew.llm import FakeClient  # noqa: E402
from agentcrew.tracer import Tracer  # noqa: E402

DB_PATH = ROOT / "data" / "northstar.db"


@pytest.fixture(autouse=True)
def isolate_from_developer_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test hermetic with respect to configuration.

    `Settings` merges three sources: init kwargs > environment variables >
    `.env` file. Any field a test does not pass explicitly falls through to
    whatever the developer happens to have configured, so assertions about
    construction semantics silently become assertions about the machine.

    This shipped as a real bug: `test_setting_one_key_leaves_the_others_unset`
    passed on a clean checkout and failed on a machine with a populated
    `.env`, because the unrelated provider's key was loaded from disk. Worse,
    pytest printed the whole Settings object in the assertion diff - leaking a
    live API key into the terminal.

    Both sources have to be neutralised: clearing the environment variables is
    not enough, because pydantic-settings reads the `.env` file directly.
    """
    for name in list(os.environ):
        if name.startswith("AGENTCREW_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def clean_subprocess_env() -> dict[str, str]:
    """Environment for launching project scripts as subprocesses.

    Inherit the real environment and strip only what is actually under test.

    Do NOT hand-build this dict. Passing `env=` to subprocess *replaces* the
    environment rather than extending it, and a hand-built POSIX dict drops
    `SystemRoot` on Windows. Winsock then fails to initialise with
    `WinError 10106` (WSAEPROVIDERFAILEDINIT) the moment anything imports
    asyncio - which SQLAlchemy does at import time, even in the database seed
    script. On Linux asyncio loads `unix_events`, needs no Winsock, and the
    bug is invisible.

    Removing `PYTHONPATH` preserves the property the packaging tests exist to
    check: each entry point must bootstrap `sys.path` itself. `PYTHONNOUSERSITE`
    additionally keeps user site-packages from masking a broken bootstrap.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


@pytest.fixture(scope="session")
def db_path() -> Path:
    if not DB_PATH.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_database.py")],
            check=True,
            cwd=ROOT,
            env=clean_subprocess_env(),
        )
    return DB_PATH


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    return Settings(
        provider="fake",
        model="fake-model",
        database_path=db_path,
        trace_dir=tmp_path / "traces",
        max_attempts_per_step=3,
        max_steps=3,
        max_llm_calls=30,
        max_sql_executions=24,
    )


@pytest.fixture
def engine(db_path: Path):
    return read_only_engine(db_path)


class ScriptedAnalyst:
    """Deterministic stand-in for a competent SQL analyst.

    Dispatches on a marker phrase in the system prompt, so it stays in sync
    with the real prompts rather than with node names.
    """

    def __init__(
        self,
        *,
        sql_by_goal: dict[str, list[str]] | None = None,
        default_sql: str = "SELECT 1 AS answer",
        verdicts: list[str] | None = None,
        plan_steps: list[str] | None = None,
        ambiguous: bool = False,
    ) -> None:
        self.sql_by_goal = sql_by_goal or {}
        self.default_sql = default_sql
        self.verdicts = list(verdicts or [])
        self.plan_steps = plan_steps
        self.ambiguous = ambiguous
        self.calls: list[str] = []
        self._sql_cursor: dict[str, int] = {}

    def __call__(self, system: str, user: str) -> dict[str, Any] | str:
        if "planning stage" in system:
            self.calls.append("plan")
            return self._plan(user)
        if "SQL authoring stage" in system:
            self.calls.append("author")
            return self._sql(user)
        if "repair stage" in system:
            self.calls.append("repair")
            return self._sql(user, repair=True)
        if "verification stage" in system:
            self.calls.append("verify")
            return self._verify()
        if "final stage" in system:
            self.calls.append("synthesize")
            return self._final(user)
        raise AssertionError(f"ScriptedAnalyst got an unrecognised prompt:\n{system[:200]}")

    # -- handlers ----------------------------------------------------------
    def _plan(self, user: str) -> dict[str, Any]:
        if self.ambiguous:
            return {
                "intent": "unclear",
                "ambiguous": True,
                "clarifying_question": "Which time period do you mean?",
                "steps": [],
            }
        steps = self.plan_steps or ["answer the question"]
        return {
            "intent": "test intent",
            "ambiguous": False,
            "clarifying_question": None,
            "steps": steps,
        }

    def _match_goal(self, user: str) -> str | None:
        for goal in self.sql_by_goal:
            if goal in user:
                return goal
        return None

    def _sql(self, user: str, repair: bool = False) -> dict[str, Any]:
        goal = self._match_goal(user)
        if goal is None:
            sql = self.default_sql
        else:
            queue = self.sql_by_goal[goal]
            idx = self._sql_cursor.get(goal, 0)
            sql = queue[min(idx, len(queue) - 1)]
            self._sql_cursor[goal] = idx + 1
        return {
            "sql": sql,
            "rationale": "scripted",
            "expected_shape": "scripted shape",
        }

    def _verify(self) -> dict[str, Any]:
        verdict = self.verdicts.pop(0) if self.verdicts else "pass"
        return {
            "verdict": verdict,
            "reason": f"scripted {verdict}",
            "repair_hint": None if verdict == "pass" else "measure the right thing",
        }

    def _final(self, user: str) -> dict[str, Any]:
        return {
            "answer": "Scripted final answer.",
            "key_numbers": ["42"],
            "caveats": [],
        }


@pytest.fixture
def make_deps(settings: Settings, tmp_path: Path):
    """Factory producing Deps wired to a ScriptedAnalyst."""

    def _make(analyst: ScriptedAnalyst, **overrides: Any):
        cfg = settings.model_copy(update=overrides)
        client = FakeClient(responder=analyst)
        tracer = Tracer("test", trace_dir=cfg.trace_dir)
        return build_deps(cfg, llm=client, tracer=tracer)

    return _make


def load_trace(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
