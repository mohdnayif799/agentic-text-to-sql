"""Where an architecture run currently is, for error reports and progress.

Each architecture names its stage before doing the work (an LLM call, a
query, a tool), for example "step 2: decide_next_action". If an exception
escapes the run, the evaluation runner reports the stage it happened in, so
an error is never mistaken for an ordinary failure or a direct answer. The
app listens to the same stages to show a progress indicator.

ContextVars keep runs in different threads (Streamlit sessions) apart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

START = "start"

_STAGE: ContextVar[str] = ContextVar("agentcrew_stage", default=START)
_LISTENER: ContextVar[Callable[[str], None] | None] = ContextVar(
    "agentcrew_stage_listener", default=None)


def enter(stage: str) -> None:
    _STAGE.set(stage)
    listener = _LISTENER.get()
    if listener is not None:
        listener(stage)


def current() -> str:
    return _STAGE.get()


def reset() -> None:
    _STAGE.set(START)


@contextmanager
def listening(callback: Callable[[str], None] | None) -> Iterator[None]:
    """Call `callback(stage)` on every stage entered inside the block."""
    token = _LISTENER.set(callback)
    try:
        yield
    finally:
        _LISTENER.reset(token)
