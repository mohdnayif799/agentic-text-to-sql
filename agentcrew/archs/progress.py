"""Where an architecture run currently is, for error reports.

Each architecture names its stage before doing the work (an LLM call, a
query, a tool), for example "step 2: decide_next_action". If an exception
escapes the run, the evaluation runner reports the stage it happened in, so
an error is never mistaken for an ordinary failure or a direct answer. A
ContextVar keeps runs in different threads (Streamlit sessions) apart.
"""

from __future__ import annotations

from contextvars import ContextVar

START = "start"

_STAGE: ContextVar[str] = ContextVar("agentcrew_stage", default=START)


def enter(stage: str) -> None:
    _STAGE.set(stage)


def current() -> str:
    return _STAGE.get()


def reset() -> None:
    _STAGE.set(START)
