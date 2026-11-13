"""The ask flow, shared by the Streamlit app and the REST API.

Why this module exists
----------------------
app.py used to hold the whole path from a question to an answer: which key
pays, the allowances and the daily cap, building the runtime, running the
architecture, turning every failure into a message, and the run-log line.
The API needs exactly that path, so it lives here and both are callers.
Nothing in it knows about Streamlit or HTTP: a caller passes plain values
and gets a `Turn` back.

What is the same for every caller
---------------------------------
* A request to change the data is refused before any model call.
* A run on the server's key uses fixed budgets (DEMO_MAX_ATTEMPTS and
  DEMO_MAX_STEPS), is checked against the daily cap before it starts, has
  every model call charged to it, and counts as a run only when it completed.
* Failures come back on the Turn, never as an exception, with the provider's
  text kept out of what a server-key caller sees.
* One run-log line per run (agentcrew/runlog.py), carrying the caller's
  surface ("app" or "api") and, for the API, a short hash of its key.

What differs is passed in: `state` (a browser session's allowance; None for
the API, which rate-limits per key instead) and `caller` (for the log).

The agent's entry points are looked up on their modules at call time
(`app_runner.build_runtime`, not an imported name), so a test that replaces
them on the module reaches every caller.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from agentcrew import llm as llm_module
from agentcrew import runlog
from agentcrew.archs import app_runner
from agentcrew.archs.app_runner import AppRun, CountingClient
from agentcrew.caps import DailyCap, DailyCapExceeded, daily_cap
from agentcrew.config import Settings
from agentcrew.datasets.describe import Notes, draft_notes
from agentcrew.datasets.source import DataSource
from agentcrew.datasets.store import DatasetStore, dataset_store
from agentcrew.db.engine import DatabaseNotFoundError, scrub_url
from agentcrew.graph import build as graph_build
from agentcrew.graph.build import RunOutcome
from agentcrew.llm import (
    PROVIDERS,
    LLMError,
    LLMTimeoutError,
    ProviderSpec,
    QuotaExhaustedError,
)
from agentcrew.refusal import WRITE_REFUSAL, is_write_request
from agentcrew.sql_editor import EditorRun, run_sql
from agentcrew.tools import ToolContext

# The app's logger: its handler (stderr, one line per event) is set up by the
# entry point (app.py or agentcrew/api/app.py).
log = logging.getLogger("agentcrew.app")

# ---- Server-key runs -----------------------------------------------------------
# A deployment can configure a server-side key (AGENTCREW_<PROVIDER>_API_KEY) so
# visitors can try the agent without one. That key is read only through
# Settings, only at the moment a run is built. Runs on it are capped per browser
# session; a visitor's own key is never capped.
DEMO_RUN_LIMIT = 5
DEMO_RUNS_USED = "demo_runs_used"  # session-state key holding the count

DEMO_LIMIT_MESSAGE = (
    f"You've used the {DEMO_RUN_LIMIT} free demo runs. Add your own Gemini key "
    "(free at aistudio.google.com/apikey) under 'Use your own API key' to keep "
    "going."
)
SHARED_QUOTA_MESSAGE = (
    "The shared demo quota is used up for now. Try again later or add your own key."
)
OWN_QUOTA_MESSAGE = (
    "Your API key hit its provider's quota or rate limit. Wait a moment and try "
    "again, or check the limits on your key."
)
DEMO_UNAVAILABLE_MESSAGE = (
    "The demo is temporarily unavailable. Try again later, or add your own key "
    "under 'Use your own API key'."
)
# A model call passed its request timeout (GEMINI_TIMEOUT_SECONDS). Shown on
# either key: it is the provider being slow, not the visitor's key or input.
SLOW_MODEL_MESSAGE = (
    "The model is responding slowly right now. Please try again in a minute."
)
# Runs on the server key use fixed budgets. Otherwise a caller could make one
# counted run spend several times as many model calls.
DEMO_MAX_ATTEMPTS = 3
DEMO_MAX_STEPS = 3
# The process-wide daily cap (agentcrew/caps.py) is the ceiling behind the
# per-session allowance, which a page reload resets. Same wording family as
# the other notices: not a failure, and not counted.
DAILY_LIMIT_MESSAGE = (
    "The demo has reached its daily limit. Try again tomorrow, or add your own "
    "key under 'Use your own API key'."
)
NO_MODEL_MESSAGE = (
    "Enter a custom model ID under 'Advanced settings', or pick one "
    "from the list."
)
NO_KEY_FOR_DESCRIPTIONS = (
    "No API key is available to draft descriptions, so they are empty. You can "
    "write them below, or add your own key and upload again."
)
DESCRIPTIONS_FAILED = (
    "The model could not draft descriptions right now, so they are empty. You "
    "can write them below; questions still work without them."
)
SQL_UNAVAILABLE = "The database is not available right now."


@dataclass(frozen=True)
class Caller:
    """Who a run is for, as the run log records it."""

    surface: str = "app"
    """app (Streamlit) or api."""
    client: str | None = None
    """The API caller: a short hash of its key, never the key. None in the app."""


APP = Caller()
SQL_EDITOR = Caller(surface="sql_editor")

Describer = Callable[[ToolContext], tuple[Notes | None, str | None]]
"""(tools) -> (drafted notes or None, a notice for the caller or None)."""

StageReporter = Callable[[int], Callable[[str], None]]
"""(the run's max_steps) -> a callback for each stage the run enters."""


# ---- Deployment settings ---------------------------------------------------------
def deployment_cap() -> DailyCap:
    """The deployment's cap, read from the environment once per process."""
    try:
        return daily_cap(Settings())
    except Exception:
        # Settings could not be built (a malformed variable); the run itself
        # will fail with a message. The cap still exists, at its defaults.
        fields = Settings.model_fields
        return daily_cap(Settings.model_construct(
            daily_run_cap=fields["daily_run_cap"].default,
            daily_llm_call_cap=fields["daily_llm_call_cap"].default,
            daily_sql_run_cap=fields["daily_sql_run_cap"].default,
        ))


def trace_content_enabled() -> bool:
    """AGENTCREW_TRACE_CONTENT: whether the run log line may carry the question
    and the final SQL. Off by default and off on the public demo."""
    try:
        return bool(Settings().trace_content)
    except Exception:
        return False


def server_key(provider: str) -> str:
    """The deployment's key for `provider`, or "". Never store or render this."""
    try:
        return getattr(Settings(), PROVIDERS[provider].key_field) or ""
    except Exception:
        return ""


def server_key_configured(provider: str) -> bool:
    return bool(server_key(provider))


def demo_runs_left(state: MutableMapping[str, Any]) -> int:
    return max(0, DEMO_RUN_LIMIT - state.get(DEMO_RUNS_USED, 0))


def record_demo_run(state: MutableMapping[str, Any]) -> None:
    state[DEMO_RUNS_USED] = state.get(DEMO_RUNS_USED, 0) + 1


def _database_url() -> str | None:
    """AGENTCREW_DATABASE_URL (self-hosted), for redaction only."""
    try:
        return Settings().database_url_value()
    except Exception:
        return None


def redact(text: str, secret: str) -> str:
    """Never echo the key back to a caller, even inside a provider error. The
    same goes for the self-hosted database URL and its password: every error
    text that is shown or logged passes through here."""
    if secret and len(secret) > 8:
        text = text.replace(secret, "***")
    return scrub_url(text, _database_url())


# ---- One question ----------------------------------------------------------------
@dataclass
class Turn:
    """One submitted question and whatever came back.

    Replaces the previous singleton `outcome` / `error` pair. Those could not
    represent "question 2 failed but question 1's answer is still valid": a new
    error overwrote `error` while leaving the old `outcome` on screen, so the
    failure appeared to belong to the previous answer.

    Binding the result (or the error) to the question that produced it makes
    that state unrepresentable.
    """

    question: str
    outcome: RunOutcome | None = None
    arch_run: AppRun | None = None
    """Set instead of `outcome` when a v2 architecture (A, C, D) answered."""
    error: str | None = None
    notice: str | None = None
    """Not a failure of the agent: demo limit reached, quota refused, no key."""
    source_id: str = "northstar"
    """Which data source answered (source_id()), so the SQL editor runs the
    answer's SQL only against that source."""
    code: str | None = None
    """Why there is no run, for a caller that maps it to its own wording (the
    API): write_refused, no_model, no_key, session_limit, daily_cap, quota,
    timeout, start_failed or error. None when the agent ran to an outcome."""


def execute(
    question: str,
    cfg: dict[str, Any],
    spec: ProviderSpec,
    *,
    own_key: str,
    stages: StageReporter | None = None,
    budget: DailyCap | None = None,
    source: DataSource | None = None,
    caller: Caller = APP,
) -> tuple[Turn, bool]:
    """Run one question. Failures are returned on the Turn, never raised.

    `cfg` carries the visitor's key when they gave one; otherwise it carries
    no key at all and Settings reads the server key from the environment, so
    the server key never passes through a caller. The returned bool says
    whether the run counts against the allowances: only a run that completed
    does. A quota refusal, a failure to start, or any exception during the
    run never uses up a visitor's runs.

    `budget` is the daily cap, given for runs on the server key only: each
    model call is charged against it, and a call that would cross the day's
    limit ends the run with a notice (not counted either).

    `source` is an uploaded dataset or the self-hosted database; None answers
    from NorthStar exactly as before.

    On the server key, failures show a generic message rather than provider
    error text; the redacted detail goes to the log instead.
    """
    turn = Turn(question=question)
    secret = own_key or server_key(spec.key)  # for redaction only
    started = time.perf_counter()
    arch = cfg.get("arch", "?")
    paid = {"own_key": bool(own_key), "question": question, "caller": caller}
    try:
        settings = Settings(**cfg)
        arch = settings.arch
        if arch == "v1" and source is None:
            deps = graph_build.build_deps(settings)
        else:
            if arch == "v1":  # the original agent only knows NorthStar
                settings = settings.model_copy(update={"arch": "full"})
                arch = "full"
            runtime = (app_runner.build_runtime(settings) if source is None
                       else app_runner.build_runtime(settings, source=source))
            client = runtime.new_client(budget=budget,
                                        trace=runtime.start_trace(question))
    except Exception as exc:
        log_run(cfg, "start_failed", started, 0, arch=arch, error=exc,
                secret=secret, **paid)
        turn.code = "start_failed"
        if not own_key:
            turn.error = DEMO_UNAVAILABLE_MESSAGE
        elif isinstance(exc, DatabaseNotFoundError):
            turn.error = str(exc)
        else:
            turn.error = (
                f"Could not start the agent: {redact(str(exc), secret)}\n\n"
                f"Check the API key in the sidebar. If the SDK is missing, run "
                f"`{spec.install}`."
            )
        return turn, False

    def calls_so_far() -> int:
        return _llm_calls_so_far(deps) if arch == "v1" else client.calls

    def usage_so_far() -> Any:
        return None if arch == "v1" else getattr(client, "usage", None)

    try:
        if arch == "v1":
            turn.outcome = graph_build.run_question(question, settings=settings,
                                                    deps=deps)
        else:
            on_stage = stages(settings.max_steps) if stages else None
            turn.arch_run = app_runner.run_arch_question(
                question, settings=settings, runtime=runtime, client=client,
                on_stage=on_stage)
    except QuotaExhaustedError as exc:
        # Stops the run outright: the provider refused, so there is nothing
        # to repair and retrying would only be refused again.
        log_run(cfg, "quota", started, calls_so_far(), arch=arch, error=exc,
                secret=secret, usage=usage_so_far(), **paid)
        turn.notice = OWN_QUOTA_MESSAGE if own_key else SHARED_QUOTA_MESSAGE
        turn.code = "quota"
        return turn, False
    except DailyCapExceeded as exc:
        # The day's model calls are used up: the call never reached the
        # provider. A notice like the per-session limit, not counted.
        log_run(cfg, "daily_cap", started, calls_so_far(), arch=arch, error=exc,
                secret=secret, usage=usage_so_far(), **paid)
        turn.notice = DAILY_LIMIT_MESSAGE
        turn.code = "daily_cap"
        return turn, False
    except LLMTimeoutError as exc:
        # The provider is slow, not broken: a notice rather than an error, the
        # same on either key, and (like every failed run) not counted.
        log_run(cfg, "timeout", started, calls_so_far(), arch=arch, error=exc,
                secret=secret, usage=usage_so_far(), **paid)
        turn.notice = SLOW_MODEL_MESSAGE
        turn.code = "timeout"
        return turn, False
    except Exception as exc:
        log_run(cfg, "error", started, calls_so_far(), arch=arch, error=exc,
                secret=secret, usage=usage_so_far(), **paid)
        turn.error = (
            f"Run failed: {type(exc).__name__}: {redact(str(exc), secret)}"
            if own_key
            else DEMO_UNAVAILABLE_MESSAGE
        )
        turn.code = "error"
        return turn, False

    if turn.arch_run is not None:
        run = turn.arch_run
        selected = run.outcome.selected
        log_run(cfg, run.outcome.status, started, run.llm_calls, arch=arch,
                path=run.outcome.path, tools=run.tools, usage=run,
                final_sql=selected.sql if selected is not None else None, **paid)
    else:
        counters = turn.outcome.trace["counters"]
        log_run(cfg, turn.outcome.status, started, counters["llm_calls"],
                arch=arch, path="v1",
                tools=[["run_sql"] for _ in turn.outcome.step_results],
                usage=_Tokens(counters.get("input_tokens", 0),
                              counters.get("output_tokens", 0)), **paid)
    return turn, True


@dataclass(frozen=True)
class _Tokens:
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0


def _llm_calls_so_far(deps: Any) -> int:
    """Model calls made before a run raised, read from its tracer."""
    tracer = getattr(deps, "tracer", None)
    return tracer.counters.get("llm_calls", 0) if tracer is not None else 0


def log_run(
    cfg: dict[str, Any],
    status: str,
    started: float,
    llm_calls: int,
    *,
    arch: str = "?",
    path: str = "-",
    tools: list[list[str]] | None = None,
    error: BaseException | None = None,
    secret: str = "",
    usage: Any = None,
    own_key: bool = False,
    question: str | None = None,
    final_sql: str | None = None,
    caller: Caller = APP,
) -> str:
    """One structured JSON line per run on stdout (agentcrew/runlog.py):
    which key paid, provider, model, architecture, status, path (direct or
    escalated), the tools of each step, model calls, tokens, seconds and an
    error's type name. Never the key, never an error's text (it can echo
    request data: the redacted text goes to a stderr detail line), and the
    question and final SQL only when AGENTCREW_TRACE_CONTENT is on."""
    content = trace_content_enabled()
    line = runlog.emit(runlog.RunRecord(
        surface=caller.surface,
        key="own" if own_key else "server",
        provider=str(cfg.get("provider", "?")),
        model=str(cfg.get("model", "?")),
        arch=arch,
        status=status,
        path=path,
        tools=tuple(tuple(step) for step in tools or ()),
        llm_calls=llm_calls,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        thoughts_tokens=int(getattr(usage, "thoughts_tokens", 0) or 0),
        seconds=time.perf_counter() - started,
        error=type(error).__name__ if error is not None else None,
        question=question if content else None,
        final_sql=final_sql if content else None,
        client=caller.client,
    ))
    if error is not None:
        # Collapsed to one line: provider errors often span several.
        message = " ".join(redact(str(error), secret).split())
        log.warning("run status=%s error=%s: %s", status, type(error).__name__,
                    message)
    return line


def answer(
    question: str,
    *,
    provider: str,
    model: str,
    own_key: str,
    max_attempts: int,
    max_steps: int,
    state: MutableMapping[str, Any] | None,
    stages: StageReporter | None = None,
    source: DataSource | None = None,
    caller: Caller = APP,
) -> Turn:
    """Choose the key for this run, enforce the allowances, and run. A request
    to change the data is refused first: no model call, no SQL, no run counted.

    `state` holds a browser session's allowance of server-key runs. None
    means the caller has no such allowance (the API, which rate-limits per
    key); the daily cap applies either way.
    """
    if is_write_request(question):
        log_run({"provider": provider, "model": model}, "refused",
                time.perf_counter(), 0, caller=caller)
        return Turn(question=question, notice=WRITE_REFUSAL, code="write_refused")
    spec = PROVIDERS[provider]
    own_key = own_key.strip()
    if not model:
        return Turn(question=question, notice=NO_MODEL_MESSAGE, code="no_model")
    cfg: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "max_attempts_per_step": max_attempts,
        "max_steps": max_steps,
    }
    if own_key:
        turn, _ = execute(
            question, {**cfg, spec.key_field: own_key}, spec, own_key=own_key,
            stages=stages, source=source, caller=caller,
        )
        return turn

    if not server_key_configured(provider):
        return Turn(
            question=question,
            notice=(
                f"Add your {spec.label} API key under 'Use your own API key' "
                "to run the agent."
            ),
            code="no_key",
        )
    if state is not None and demo_runs_left(state) == 0:
        return Turn(question=question, notice=DEMO_LIMIT_MESSAGE,
                    code="session_limit")
    cap = deployment_cap()
    if not cap.allows_run():
        return Turn(question=question, notice=DAILY_LIMIT_MESSAGE, code="daily_cap")

    # Enforced here, not by the caller: a browser's widget values and an API
    # request's fields both come from outside, so the server decides the
    # budgets for its own key.
    demo_cfg = {
        **cfg,
        "max_attempts_per_step": DEMO_MAX_ATTEMPTS,
        "max_steps": DEMO_MAX_STEPS,
    }
    turn, charged = execute(question, demo_cfg, spec, own_key="", stages=stages,
                            budget=cap, source=source, caller=caller)
    if charged:
        if state is not None:
            record_demo_run(state)
        cap.charge_run()
    return turn


# ---- Describing a dataset ----------------------------------------------------------
def describer(*, provider: str, model: str, own_key: str,
              caller: Caller = APP) -> Describer:
    """The model call that drafts a dataset's descriptions, paid for the way a
    run is: the visitor's own key uncapped, or the server key with every call
    charged to the daily cap. One run-log line records it (arch "describe")."""

    def describe(tools: ToolContext) -> tuple[Notes | None, str | None]:
        spec = PROVIDERS[provider]
        cfg: dict[str, Any] = {"provider": provider, "model": model}
        budget = None
        if own_key:
            cfg[spec.key_field] = own_key
        elif server_key_configured(provider):
            budget = deployment_cap()
        else:
            return None, NO_KEY_FOR_DESCRIPTIONS
        started = time.perf_counter()
        client: CountingClient | None = None
        paid = {"own_key": bool(own_key), "caller": caller}
        try:
            settings = Settings(**cfg)
            client = CountingClient(llm_module.build_client(settings),
                                    budget=budget)
            notes = draft_notes(client, tools,
                                max_tokens=settings.max_output_tokens,
                                temperature=settings.temperature)
        except DailyCapExceeded as exc:
            log_run(cfg, "daily_cap", started, client.calls if client else 0,
                    arch="describe", error=exc, **paid)
            return None, DAILY_LIMIT_MESSAGE
        except (QuotaExhaustedError, LLMTimeoutError, LLMError) as exc:
            log_run(cfg, "error", started, client.calls if client else 0,
                    arch="describe", error=exc, secret=own_key,
                    usage=client.usage if client else None, **paid)
            return None, DESCRIPTIONS_FAILED
        log_run(cfg, "done", started, client.calls, arch="describe",
                usage=client.usage, **paid)
        return notes, None

    return describe


# ---- Running SQL directly ----------------------------------------------------------
def run_sql_on(sql: str, source: DataSource | None, *,
               state: MutableMapping[str, Any], session_limit: int | None = None,
               caller: Caller = SQL_EDITOR,
               ) -> tuple[EditorRun, int]:
    """Run a caller's own SQL on the agent's ToolContext for `source`
    (build_runtime caches it, so it is the very object the agent's runs use;
    None is NorthStar). Returns the run and the row cap it ran under. No model
    call is made. `session_limit` None means the configured per-session cap;
    0 means none (the API, which rate-limits per key)."""
    try:
        settings = Settings()
        runtime = (app_runner.build_runtime(settings) if source is None
                   else app_runner.build_runtime(settings, source=source))
        tools = runtime.tools
    except Exception as exc:
        log.warning("sql editor could not start: %s", type(exc).__name__)
        runlog.emit(runlog.RunRecord(surface=caller.surface, provider="-",
                                     model="-", arch="-", status="start_failed",
                                     key="-", error=type(exc).__name__,
                                     client=caller.client))
        return EditorRun(message=SQL_UNAVAILABLE, status="start_failed"), 0
    limit = settings.session_sql_run_cap if session_limit is None else session_limit
    run = run_sql(sql, tools=tools, state=state, session_limit=limit,
                  cap=deployment_cap(), scrub=lambda text: redact(text, ""),
                  trace_content=trace_content_enabled(), surface=caller.surface,
                  client=caller.client)
    return run, tools.max_rows


# ---- The dataset store ---------------------------------------------------------------
_hooked: set[int] = set()
_hook_lock = threading.Lock()


def store(settings: Any) -> DatasetStore:
    """The process-wide dataset store, with the cache cleanup hook registered
    once: removing a dataset closes its engine and forgets its context."""
    found = dataset_store(settings)
    with _hook_lock:
        if id(found) not in _hooked:
            found.on_remove(app_runner.forget_dataset)
            _hooked.add(id(found))
    return found
