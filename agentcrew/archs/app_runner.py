"""Run a v2 architecture (A, C or D) for the app.

The evaluation runner wires architectures to cached clients; the app needs
the plain version: a live client, the shared context for today's date, a
count of the model calls a run made, and the stages it went through (for a
progress indicator). Nothing here logs or stores the question, the answer or
any key.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from agentcrew.archs import ArchOutcome, progress, run_architecture
from agentcrew.config import Settings
from agentcrew.context import ContextBuilder
from agentcrew.db.engine import read_only_engine
from agentcrew.llm import LLMClient, LLMResponse, build_client
from agentcrew.tools import ToolContext


class CountingClient(LLMClient):
    """Delegates to a live client and counts its calls (one per request)."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.name = inner.name
        self.calls = 0

    def complete(self, *, system: str, user: str, max_tokens: int = 2048,
                 temperature: float = 0.0) -> LLMResponse:
        self.calls += 1
        return self.inner.complete(system=system, user=user, max_tokens=max_tokens,
                                   temperature=temperature)


@dataclass(frozen=True)
class Runtime:
    """What a run needs besides the question. Built once per settings."""

    settings: Settings
    tools: ToolContext
    builder: ContextBuilder
    client_factory: Callable[[Settings], LLMClient]

    def new_client(self) -> CountingClient:
        """A counted client for one run. Built before the run starts, so the
        caller still knows how many calls were made if the run raises."""
        return CountingClient(self.client_factory(self.settings))


@dataclass(frozen=True)
class AppRun:
    """One answered question: the outcome plus what the app shows and logs."""

    outcome: ArchOutcome
    llm_calls: int
    seconds: float

    @property
    def tools(self) -> list[list[str]]:
        """Tools by step, e.g. [["run_sql"], ["compute:decompose_change"]]."""
        return [list(s.get("tools", [])) for s in self.outcome.steps]


_CONTEXT: dict[tuple[str, date], tuple[ToolContext, ContextBuilder]] = {}


def build_runtime(settings: Settings,
                  client_factory: Callable[[Settings], LLMClient] = build_client
                  ) -> Runtime:
    """The read-only database, tools and shared context (profiled once per
    database and day, then reused), plus how to build the model client."""
    today = settings.today or date.today()
    key = (str(settings.database_path), today)
    if key not in _CONTEXT:
        engine = read_only_engine(settings.database_path)
        tools = ToolContext.create(engine, max_rows=settings.max_result_rows,
                                   timeout=settings.sql_timeout_seconds)
        builder = ContextBuilder.from_database(engine, tools.catalog, today=today)
        _CONTEXT[key] = (tools, builder)
    tools, builder = _CONTEXT[key]
    return Runtime(settings, tools, builder, client_factory)


def run_arch_question(
    question: str,
    *,
    settings: Settings,
    runtime: Runtime,
    client: CountingClient | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> AppRun:
    """Answer one question with settings.arch. `on_stage` receives each stage
    the architecture enters (agentcrew/archs/progress.py). Provider errors,
    quota refusals included, propagate to the caller."""
    client = client or runtime.new_client()
    started = time.perf_counter()
    with progress.listening(on_stage):
        outcome = run_architecture(
            settings.arch, question, settings=settings, llm=client,
            tools=runtime.tools, context_builder=runtime.builder,
        )
    return AppRun(outcome, client.calls, time.perf_counter() - started)
