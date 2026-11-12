"""Run a v2 architecture (A, C or D) for the app.

The evaluation runner wires architectures to cached clients; the app needs
the plain version: a live client, the shared context for today's date, a
count of the model calls a run made, and the stages it went through (for a
progress indicator). Nothing here logs or stores the question, the answer or
any key. Each model call is logged with its role (the first line of its
system prompt, which is fixed text), duration and token counts only.

Two optional collaborators ride on the counting client:

* the process-wide daily cap (agentcrew/caps.py), which refuses a model call
  that would cross the day's limit before it reaches the provider;
* a Langfuse run trace (agentcrew/tracer.py), which receives the same facts
  the log lines do. Tracing is an extra: every call into it is guarded, and
  nothing in it waits on the network.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from agentcrew.archs import ArchOutcome, progress, run_architecture
from agentcrew.caps import DailyCap
from agentcrew.config import Settings
from agentcrew.context import ContextBuilder
from agentcrew.db.engine import read_only_engine
from agentcrew.llm import LLMClient, LLMResponse, Usage, build_client
from agentcrew.tools import ToolContext
from agentcrew.tracer import LangfuseRunSink, RunTrace, build_run_sink

# A child of app.py's "agentcrew.app" logger, which sets INFO and the stderr
# handler (Cloud Run sends stderr to Cloud Logging). A logger outside that
# tree would fall back to the root logger's WARNING and drop these lines.
log = logging.getLogger("agentcrew.app.llm")


def _role(system: str) -> str:
    """The first line of a system prompt: fixed text naming the call's job,
    never the question (the same label evalrun/cache.py records)."""
    first = system.strip().splitlines()[0] if system.strip() else ""
    return first[:80].replace('"', "'")


class CountingClient(LLMClient):
    """Delegates to a live client, counts its calls (one per request) and
    their tokens, and logs each one, so a slow run shows which call was slow.

    `budget` is the daily cap: each call is charged before it is made, and a
    call that would cross the day's limit raises DailyCapExceeded instead of
    reaching the provider. `trace` is the run's Langfuse trace, when tracing
    is on; it gets the same facts the log line gets and nothing more.
    """

    def __init__(self, inner: LLMClient, *, budget: DailyCap | None = None,
                 trace: RunTrace | None = None) -> None:
        self.inner = inner
        self.name = inner.name
        self.calls = 0
        self.usage = Usage()
        """Tokens over every call of this run, for the run log line."""
        self.budget = budget
        self.trace = trace

    def complete(self, *, system: str, user: str, max_tokens: int = 2048,
                 temperature: float = 0.0) -> LLMResponse:
        if self.budget is not None:
            self.budget.charge_call()  # raises DailyCapExceeded; not counted
        self.calls += 1
        started = time.perf_counter()
        role = _role(system)
        fields = f'llm_call n={self.calls} role="{role}"'
        handle = (self.trace.call_started(role=role, model=self.model_name())
                  if self.trace is not None else None)
        try:
            response = self.inner.complete(system=system, user=user,
                                           max_tokens=max_tokens,
                                           temperature=temperature)
        except Exception as exc:
            # Only the exception type: provider messages can echo request data.
            seconds = time.perf_counter() - started
            log.info("%s seconds=%.2f status=%s", fields, seconds,
                     type(exc).__name__)
            if self.trace is not None:
                self.trace.call_ended(handle, seconds=seconds,
                                      status=type(exc).__name__)
            raise
        seconds = time.perf_counter() - started
        self.usage.add(response.usage)
        # thoughts_tokens: hidden thinking, counted apart from output_tokens,
        # so a slow call shows whether the model was thinking or waiting.
        log.info("%s seconds=%.2f status=ok input_tokens=%d output_tokens=%d "
                 "thoughts_tokens=%d", fields, seconds,
                 response.usage.input_tokens, response.usage.output_tokens,
                 response.usage.thoughts_tokens)
        if self.trace is not None:
            self.trace.call_ended(handle, seconds=seconds, status="ok",
                                  usage=response.usage)
        return response

    def model_name(self) -> str:
        """The wrapped client's model, when it has one (provider clients keep
        it as `_model`); "" otherwise."""
        return str(getattr(self.inner, "_model", "") or "")


@dataclass(frozen=True)
class Runtime:
    """What a run needs besides the question. Built once per settings."""

    settings: Settings
    tools: ToolContext
    builder: ContextBuilder
    client_factory: Callable[[Settings], LLMClient]
    sink: LangfuseRunSink | None = None
    """Langfuse run tracing, when its keys are configured; else None."""

    def new_client(self, *, budget: DailyCap | None = None,
                   trace: RunTrace | None = None) -> CountingClient:
        """A counted client for one run. Built before the run starts, so the
        caller still knows how many calls were made if the run raises."""
        return CountingClient(self.client_factory(self.settings), budget=budget,
                              trace=trace)

    def start_trace(self, question: str) -> RunTrace | None:
        """The run's Langfuse trace, or None when tracing is off."""
        if self.sink is None:
            return None
        return self.sink.start(arch=self.settings.arch,
                               provider=self.settings.provider,
                               model=self.settings.resolved_model(),
                               question=question)


@dataclass(frozen=True)
class AppRun:
    """One answered question: the outcome plus what the app shows and logs."""

    outcome: ArchOutcome
    llm_calls: int
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0

    @property
    def tools(self) -> list[list[str]]:
        """Tools by step, e.g. [["run_sql"], ["compute:decompose_change"]]."""
        return [list(s.get("tools", [])) for s in self.outcome.steps]


_CONTEXT: dict[tuple[str, date], tuple[ToolContext, ContextBuilder]] = {}
#: The Langfuse sink is built once per process from the first settings seen:
#: one client, one credential check, one log line.
_SINK: list[LangfuseRunSink | None] = []


def run_sink(settings: Settings) -> LangfuseRunSink | None:
    if not _SINK:
        _SINK.append(build_run_sink(settings))
    return _SINK[0]


def reset_run_sink() -> None:
    """Test helper: forget the process-wide sink."""
    _SINK.clear()


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
    return Runtime(settings, tools, builder, client_factory, sink=run_sink(settings))


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
    quota refusals and a daily-cap refusal propagate to the caller. The
    client's trace, if it has one, is closed here either way."""
    client = client or runtime.new_client()
    trace = client.trace
    started = time.perf_counter()
    try:
        with progress.listening(on_stage):
            outcome = run_architecture(
                settings.arch, question, settings=settings, llm=client,
                tools=runtime.tools, context_builder=runtime.builder,
            )
    except Exception as exc:
        if trace is not None:
            trace.finish(status="error", path="-", tools=[],
                         llm_calls=client.calls,
                         seconds=time.perf_counter() - started,
                         error=type(exc).__name__)
        raise
    run = AppRun(outcome, client.calls, time.perf_counter() - started,
                 input_tokens=client.usage.input_tokens,
                 output_tokens=client.usage.output_tokens,
                 thoughts_tokens=client.usage.thoughts_tokens)
    if trace is not None:
        selected = outcome.selected
        trace.finish(status=outcome.status, path=outcome.path, tools=run.tools,
                     llm_calls=run.llm_calls, seconds=run.seconds,
                     final_sql=selected.sql if selected is not None else None)
    return run
