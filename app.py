"""AgentCrew Streamlit interface.

Kept deliberately plain. The interesting thing to demonstrate is the agent's
behaviour - whether one query was enough or the question escalated, which
tools each step used, the queries it wrote - so the UI's job is to expose
that clearly, not to be a dashboard.

The architecture comes from Settings.arch (AGENTCREW_ARCH). The default is D
(`full`): one query first, escalation only on a signal, then tools. `v1`
runs the original multi-step agent.

Run:  streamlit run app.py
"""

from __future__ import annotations

import html
import logging
import re
import sys
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentcrew.archs.app_runner import (  # noqa: E402
    AppRun,
    build_runtime,
    run_arch_question,
)
from agentcrew.config import Settings  # noqa: E402
from agentcrew.db.engine import DatabaseNotFoundError  # noqa: E402
from agentcrew.graph.build import RunOutcome, build_deps, run_question  # noqa: E402
from agentcrew.llm import (  # noqa: E402
    PROVIDERS,
    SELECTABLE_PROVIDERS,
    ProviderSpec,
    QuotaExhaustedError,
)
from agentcrew.schemas import ExecutionResult  # noqa: E402

# One line per run on stderr (Cloud Run sends stderr to Cloud Logging).
# Streamlit re-executes this file on every rerun but loggers are process-wide,
# so the guard stops each rerun from adding another handler (duplicate lines).
log = logging.getLogger("agentcrew.app")
if not log.handlers:
    _stderr = logging.StreamHandler()  # defaults to sys.stderr
    _stderr.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    log.addHandler(_stderr)
    log.setLevel(logging.INFO)
    log.propagate = False

# Must match primaryColor in .streamlit/config.toml (a test checks this), so
# charts use the same blue as buttons, links and the question bubble.
CHART_COLOR = "#2563EB"

PAGE_CSS = """
<style>
/* User message bubble (same as the RAG project).
   Right-aligned, no avatar. The row is a flex container and the bubble is a
   flex item, so the bubble is sized by its CONTENT and only grows to the cap -
   a three-word question stays three words wide instead of becoming a
   full-width block. max-width uses min() of a percentage and an absolute cap
   so it is responsive in both directions. */
.user-msg-row {
    display: flex;
    justify-content: flex-end;
    margin: 1.75rem 0 0.55rem 0;
}
.user-msg {
    max-width: min(56%, 36rem);
    background: rgba(59, 130, 246, 0.10);
    border: 1px solid rgba(59, 130, 246, 0.20);
    border-radius: 1.15rem 1.15rem 0.35rem 1.15rem;
    padding: 0.7rem 1.05rem;
    font-size: 0.95rem;
    line-height: 1.55;
    text-align: left;
    white-space: pre-wrap;      /* keep newlines in multi-line questions */
    overflow-wrap: anywhere;    /* never let a long URL widen the bubble */
}
@media (max-width: 640px) {
    .user-msg { max-width: 86%; }
}
/* Example questions as chips. Scoped through the container's key class
   (st-key-...), a documented hook, not Streamlit's internal test ids. */
.st-key-example_chips button {
    border-radius: 999px;
    font-size: 0.9rem;
}
</style>
"""

EXAMPLES = [
    "Which region lost the most customers in Q3 2025?",
    "Why did revenue decrease in Q3 2025 compared with Q2?",
    "Which product category generated the most revenue in 2025?",
    "Which customer segment has the highest churn rate?",
    "Which region had the most high or critical support tickets in 2025?",
    "Delete all customers in the LATAM region.",
]


def to_frame(result: ExecutionResult) -> pd.DataFrame:
    if not result.columns:
        return pd.DataFrame()
    return pd.DataFrame(result.rows, columns=result.columns)


def maybe_chart(df: pd.DataFrame) -> None:
    chart = build_chart(df)
    if chart is not None:
        st.altair_chart(chart, width="stretch")


def build_chart(df: pd.DataFrame) -> alt.Chart | None:
    """Chart only when it genuinely helps: one label column, one number
    column, and few enough rows to read.

    Note: pandas 3.0 gives string columns the `str` dtype rather than
    `object`, so `dtype == object` silently stops matching. `is_string_dtype`
    is correct on both 2.x and 3.x.

    Built with Altair rather than `st.bar_chart` because the built-in chart
    gives no control over plot padding, and the left-most y-axis tick was
    being clipped ("100" rendering as "00"). Ticks are still chosen by Vega
    from the data - nothing about the scale is hardcoded.
    """
    if df.empty or len(df) > 25 or df.shape[1] < 2:
        return None
    labels = [
        c
        for c in df.columns
        if pd.api.types.is_string_dtype(df[c])
        and not pd.api.types.is_numeric_dtype(df[c])
    ]
    numbers = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not (labels and numbers):
        return None

    label, value = labels[0], numbers[0]
    return (
        alt.Chart(df)
        .mark_bar(color=CHART_COLOR)
        .encode(
            x=alt.X(
                f"{label}:N",
                sort="-y",
                title=None,
                axis=alt.Axis(labelAngle=0, labelLimit=180, labelOverlap=False),
            ),
            y=alt.Y(
                f"{value}:Q",
                title=value.replace("_", " "),
                axis=alt.Axis(tickCount=5, labelPadding=6, titlePadding=8),
            ),
            tooltip=list(df.columns),
        )
        .properties(
            height=340,
            # The actual fix. `autosize` with contains="padding" makes Vega fit
            # the whole drawing - axis labels included - inside the container
            # instead of letting the widest tick label run off the left edge,
            # and the explicit padding reserves room for it.
            padding={"left": 16, "right": 16, "top": 10, "bottom": 10},
            autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        )
        .configure_view(strokeWidth=0)
        .configure_axis(labelFontSize=12, titleFontSize=12)
    )


CUSTOM_MODEL = "Custom..."

# ---- Demo mode ---------------------------------------------------------------
# A deployment can configure a server-side key (AGENTCREW_<PROVIDER>_API_KEY) so
# visitors can try the agent without one. That key is read only through
# Settings, only at the moment a run is built, and never placed in a widget or
# in st.session_state: Streamlit sends every widget value to the browser, and a
# password field has a show/hide toggle. Runs on it are capped per browser
# session; a visitor's own key is never capped.
DEMO_RUN_LIMIT = 5
DEMO_RUNS_USED = "demo_runs_used"  # st.session_state key holding the count

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
# Runs on the server key use fixed budgets. Otherwise the sliders would let one
# counted run make several times as many model calls.
DEMO_MAX_ATTEMPTS = 3
DEMO_MAX_STEPS = 3
DEMO_FIXED_CAPTION = "Fixed in demo mode. Add your own key to change."


def configured_defaults() -> tuple[str, str]:
    """(provider, model) the UI starts on: AGENTCREW_PROVIDER / AGENTCREW_MODEL.

    Falls back to the first selectable provider when configuration is missing,
    invalid, or names the test-only `fake` provider.
    """
    try:
        settings = Settings()
    except Exception:
        return SELECTABLE_PROVIDERS[0], ""
    if settings.provider not in SELECTABLE_PROVIDERS:
        return SELECTABLE_PROVIDERS[0], ""
    return settings.provider, settings.model


def model_options(provider: str, configured_model: str) -> list[str]:
    """Models offered for `provider`, keeping a configured one that is not listed."""
    models = list(PROVIDERS[provider].models)
    if configured_model and configured_model not in models:
        models = [configured_model, *models]
    return [*models, CUSTOM_MODEL]


def _server_key(provider: str) -> str:
    """The deployment's key for `provider`, or "". Never store or render this."""
    try:
        return getattr(Settings(), PROVIDERS[provider].key_field) or ""
    except Exception:
        return ""


def server_key_configured(provider: str) -> bool:
    return bool(_server_key(provider))


def demo_runs_left(state: MutableMapping[str, Any]) -> int:
    return max(0, DEMO_RUN_LIMIT - state.get(DEMO_RUNS_USED, 0))


def record_demo_run(state: MutableMapping[str, Any]) -> None:
    state[DEMO_RUNS_USED] = state.get(DEMO_RUNS_USED, 0) + 1


def redact(text: str, secret: str) -> str:
    """Never echo the key back into the UI, even inside a provider error."""
    if secret and len(secret) > 8:
        return text.replace(secret, "***")
    return text


# ---- Architecture runs (A, C, D) ---------------------------------------------
_STAGE = re.compile(r"step (\d+): (.+)")
_STAGE_WORDS = {
    "decide_next_action": "choosing the next step",
    "metric plan": "planning the metric analysis",
    "trend plan": "planning the trend fit",
    "query": "running a query",
    "follow-up SQL": "writing a follow-up query",
}


def stage_label(stage: str, max_steps: int) -> tuple[str, float, bool] | None:
    """(progress text, fraction done, escalating) for a stage the run entered,
    or None for a stage of the direct path (step 1 and the decision)."""
    if stage == "final: answer":
        return "Writing the answer", 1.0, False
    if stage.startswith("final: answer"):
        return "Writing the answer from the first query's result", 1.0, True
    if stage == "final: assembly":
        return "Escalated: assembling the final table", 0.95, True
    found = _STAGE.fullmatch(stage)
    if not found:
        return None
    number, what = int(found[1]), found[2]
    fraction = min(number / (max_steps + 1), 0.9)
    repairing = what.startswith(("repair", "A's retry"))
    if number == 1:
        if repairing:
            return "Escalated: repairing the first query", fraction, True
        if what == "verification":
            return "Escalated: checking the first result", fraction, True
        return None
    if repairing:
        doing = "repairing a query"
    elif what.startswith("compute:"):
        doing = what.split(":", 1)[1].replace("_", " ")
    else:
        doing = _STAGE_WORDS.get(what, what.replace("_", " "))
    return f"Escalated: step {number} of {max_steps}, {doing}", fraction, True


def stage_reporter(
    show: Callable[[str, float], None], max_steps: int
) -> Callable[[str], None]:
    """on_stage callback: shows progress once the question has escalated
    (a direct answer takes a few seconds; an escalated one 5 to 20)."""
    escalated = False

    def on_stage(stage: str) -> None:
        nonlocal escalated
        label = stage_label(stage, max_steps)
        if label is None:
            return
        text, fraction, escalating = label
        escalated = escalated or escalating
        if escalated:
            show(text, fraction)
    return on_stage


def path_summary(run: AppRun) -> str:
    outcome = run.outcome
    if outcome.path != "escalated":
        return "**Answered directly:** one query was enough, so nothing escalated."
    if outcome.plan:
        reason = outcome.plan[0]
    elif outcome.repairs:
        reason = f"the first query needed a repair ({outcome.repairs[0].signal})."
    else:
        reason = "a check on the first result asked for more work."
    return f"**Escalated:** {reason}"


def tools_text(tools: list[str]) -> str:
    return " + ".join(tools) or "none"


# How the final table was made, when it is not one step's own result.
FINAL_TABLE = {
    "assemble": "assembled from the steps below",
    "stack": "the metric analyses below, stacked",
    "fallback": "the first query's result (answering the escalated table failed)",
}


def render_arch_run(run: AppRun) -> None:
    outcome = run.outcome
    if outcome.status == "done":
        st.success(outcome.answer or "")
    else:
        st.error(outcome.answer or "The agent stopped without an answer.")
    st.markdown(path_summary(run))
    for caveat in outcome.caveats:
        st.warning(caveat)

    steps = [s for s in outcome.steps if s.get("step") != "final"]
    final = [s for s in outcome.steps if s.get("step") == "final"]
    a, b, c, d, e = st.columns(5)
    a.metric("Path", "Escalated" if outcome.path == "escalated" else "Direct")
    b.metric("Steps", len(steps))
    c.metric("Repairs", len(outcome.repairs))
    d.metric("LLM calls", run.llm_calls)
    e.metric("Time", f"{run.seconds:.1f}s")

    if outcome.selected is not None:
        df = to_frame(outcome.selected)
        st.markdown("**Final result**")
        if df.empty:
            st.caption("No rows returned.")
        else:
            st.dataframe(df, width="stretch", hide_index=True)
            maybe_chart(df)
        if final:
            made = final[0].get("tools", ["assemble"])[0]
            st.caption(f"Final table: {FINAL_TABLE.get(made, made)}.")

    for step in steps:
        with st.expander(f"Step {step['step']}: {tools_text(step.get('tools', []))}",
                         expanded=False):
            if step.get("reason"):
                st.caption(step["reason"])
            if step.get("sql"):
                st.code(step["sql"], language="sql", wrap_lines=True)


def render_run(outcome: RunOutcome) -> None:
    if outcome.status == "done":
        st.success(outcome.answer or "")
    elif outcome.status == "needs_clarification":
        st.info(f"**I need one clarification:** {outcome.answer}")
    else:
        st.error(outcome.answer or "The agent stopped without an answer.")
        st.caption(f"stop reason: `{outcome.stop_reason}`")

    if outcome.key_numbers:
        # These are free-form strings from the model ("North America: 84 lost
        # customers"). st.metric renders them at display size and clips them
        # ("North America: 8..."), so they are rendered as wrapping text.
        st.markdown("**Key figures**")
        for item in outcome.key_numbers:
            st.markdown(f"- {item}")

    for caveat in outcome.caveats:
        st.warning(caveat)

    counters = outcome.trace["counters"]
    a, b, c, d, e = st.columns(5)
    a.metric("Steps", len(outcome.step_results))
    b.metric("SQL runs", counters["sql_executions"])
    c.metric("Repairs", counters["repairs"])
    d.metric("LLM calls", counters["llm_calls"])
    e.metric("Time", f"{outcome.trace['elapsed_seconds']:.1f}s")

    for i, step in enumerate(outcome.step_results, start=1):
        badge = "verified" if step.verified else "UNVERIFIED"
        with st.expander(
            f"Step {i}: {step.goal}  -  {step.attempts_used} attempt(s), {badge}",
            expanded=(i == 1),
        ):
            st.code(step.sql, language="sql", wrap_lines=True)
            df = to_frame(step.result)
            if df.empty:
                st.caption("No rows returned.")
            else:
                st.dataframe(df, width="stretch", hide_index=True)
                maybe_chart(df)
            st.caption(
                f"{step.result.row_count} row(s) in {step.result.duration_ms:.0f} ms"
                + ("  -  truncated at the row cap" if step.result.truncated else "")
            )

    with st.expander("Agent trace"):
        st.caption(
            "Every node the agent entered, in order. This is the same data "
            "written to data/traces/ and forwarded to Langfuse when enabled."
        )
        spans = pd.DataFrame(
            [
                {
                    "node": s["name"],
                    "kind": s["kind"],
                    "ms": round(s["duration_ms"] or 0, 2),
                    "ok": "ok" if s["ok"] else "failed",
                    "detail": ", ".join(
                        f"{k}={v}" for k, v in list(s["data"].items())[:4]
                    ),
                }
                for s in outcome.trace["spans"]
            ]
        )
        # st.table rather than st.dataframe: the detail column holds long goal
        # strings, and a dataframe grid clips them to the column width with no
        # way to read the rest. A static table wraps.
        st.table(spans.set_index("node"))

    with st.expander("Schema selection"):
        st.write("**Tables shown to the model:**", ", ".join(outcome.selected_tables))
        dbg = outcome.schema_debug
        if dbg.get("seeded_by"):
            st.write("Lexically matched:", ", ".join(dbg["seeded_by"]))
        if dbg.get("expanded_by_fk"):
            st.write("Added via foreign keys:", ", ".join(dbg["expanded_by_fk"]))
        st.caption(
            "Selection is deterministic: token overlap plus one-hop foreign-key "
            "closure. No LLM call is spent here."
        )


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


def execute(
    question: str,
    cfg: dict[str, Any],
    spec: ProviderSpec,
    *,
    own_key: str,
    show_progress: Callable[[str, float], None] | None = None,
) -> tuple[Turn, bool]:
    """Run one question. Failures are returned on the Turn, never raised.

    `cfg` carries the visitor's key when they gave one; otherwise it carries
    no key at all and Settings reads the server key from the environment, so
    the server key never passes through the UI. The returned bool says whether
    the run counts against the demo allowance: only a run that completed does.
    A quota refusal, a failure to start, or any exception during the run never
    uses up a visitor's runs.

    On the server key, failures show a generic message rather than provider
    error text; the redacted detail goes to the log instead.
    """
    turn = Turn(question=question)
    secret = own_key or _server_key(spec.key)  # for redaction only
    started = time.perf_counter()
    arch = cfg.get("arch", "?")
    try:
        settings = Settings(**cfg)
        arch = settings.arch
        if arch == "v1":
            deps = build_deps(settings)
        else:
            runtime = build_runtime(settings)
            client = runtime.new_client()
    except Exception as exc:
        _log_run(cfg, "start_failed", started, 0, arch=arch, error=exc,
                 secret=secret)
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

    try:
        if arch == "v1":
            turn.outcome = run_question(question, settings=settings, deps=deps)
        else:
            on_stage = (stage_reporter(show_progress, settings.max_steps)
                        if show_progress else None)
            turn.arch_run = run_arch_question(
                question, settings=settings, runtime=runtime, client=client,
                on_stage=on_stage)
    except QuotaExhaustedError as exc:
        # Stops the run outright: the provider refused, so there is nothing
        # to repair and retrying would only be refused again.
        _log_run(cfg, "quota", started, calls_so_far(), arch=arch, error=exc,
                 secret=secret)
        turn.notice = OWN_QUOTA_MESSAGE if own_key else SHARED_QUOTA_MESSAGE
        return turn, False
    except Exception as exc:
        _log_run(cfg, "error", started, calls_so_far(), arch=arch, error=exc,
                 secret=secret)
        turn.error = (
            f"Run failed: {type(exc).__name__}: {redact(str(exc), secret)}"
            if own_key
            else DEMO_UNAVAILABLE_MESSAGE
        )
        return turn, False

    if turn.arch_run is not None:
        run = turn.arch_run
        _log_run(cfg, run.outcome.status, started, run.llm_calls, arch=arch,
                 path=run.outcome.path, tools=run.tools)
    else:
        _log_run(cfg, turn.outcome.status, started,
                 turn.outcome.trace["counters"]["llm_calls"], arch=arch, path="v1",
                 tools=[["run_sql"] for _ in turn.outcome.step_results])
    return turn, True


def _llm_calls_so_far(deps: Any) -> int:
    """Model calls made before a run raised, read from its tracer."""
    tracer = getattr(deps, "tracer", None)
    return tracer.counters.get("llm_calls", 0) if tracer is not None else 0


def _log_run(
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
) -> None:
    """One log line per run: provider, model, architecture, status, path
    (direct or escalated), the tools of each step and the LLM calls. Never
    the key and never the question text."""
    used = ">".join("+".join(step) for step in tools or []) or "-"
    fields = (
        f"provider={cfg['provider']} model={cfg['model']} arch={arch} "
        f"status={status} path={path} tools={used} "
        f"seconds={time.perf_counter() - started:.1f} llm_calls={llm_calls}"
    )
    if error is None:
        log.info("run %s", fields)
        return
    # Collapsed to one line: provider errors often span several.
    message = " ".join(redact(str(error), secret).split())
    log.warning("run %s error=%s: %s", fields, type(error).__name__, message)


def answer(
    question: str,
    *,
    provider: str,
    model: str,
    own_key: str,
    max_attempts: int,
    max_steps: int,
    state: MutableMapping[str, Any],
    show_progress: Callable[[str, float], None] | None = None,
) -> Turn:
    """Choose the key for this run, enforce the demo allowance, and run."""
    spec = PROVIDERS[provider]
    own_key = own_key.strip()
    if not model:
        return Turn(
            question=question,
            notice=(
                "Enter a custom model ID under 'Advanced settings', or pick one "
                "from the list."
            ),
        )
    cfg: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "max_attempts_per_step": max_attempts,
        "max_steps": max_steps,
    }
    if own_key:
        turn, _ = execute(
            question, {**cfg, spec.key_field: own_key}, spec, own_key=own_key,
            show_progress=show_progress,
        )
        return turn

    if not server_key_configured(provider):
        return Turn(
            question=question,
            notice=(
                f"Add your {spec.label} API key under 'Use your own API key' "
                "to run the agent."
            ),
        )
    if demo_runs_left(state) == 0:
        return Turn(question=question, notice=DEMO_LIMIT_MESSAGE)

    # Enforced here, not only by the disabled sliders: widget values come from
    # the browser, so the server decides the budgets for its own key.
    demo_cfg = {
        **cfg,
        "max_attempts_per_step": DEMO_MAX_ATTEMPTS,
        "max_steps": DEMO_MAX_STEPS,
    }
    turn, charged = execute(question, demo_cfg, spec, own_key="",
                            show_progress=show_progress)
    if charged:
        record_demo_run(state)
    return turn


ABOUT = (
    "AgentCrew answers plain-English questions about a sample sales database. "
    "It writes one read-only SQL query first and answers directly when that is "
    "enough. On a concrete signal (an error, an empty or suspicious result, a "
    "why or forecast question) it escalates: it repairs the query, explores "
    "with lookups and deterministic tools, and checks the result. Try the "
    "delete example to see it refuse to write."
)


@dataclass(frozen=True)
class RunChoice:
    """What the sidebar decided for the next run.

    `own_key` is the visitor's key, read from its widget on every script run.
    The server key is never part of this: Settings reads it at run time.
    """

    provider: str
    model: str
    own_key: str
    max_attempts: int
    max_steps: int


def _render_user_message(text: str) -> None:
    """Draw a user turn as a right-aligned bubble with no avatar.

    Text is HTML-escaped rather than passed through st.markdown. Beyond the
    obvious injection reason, it means a question containing __init__.py or
    *asterisks* is shown exactly as typed instead of being reinterpreted as
    formatting.
    """
    st.markdown(
        '<div class="user-msg-row"><div class="user-msg">'
        f"{html.escape(text)}</div></div>",
        unsafe_allow_html=True,
    )


def render_reply(turn: Turn) -> None:
    if turn.notice:
        st.warning(turn.notice)
    if turn.error:
        st.error(turn.error)
    if turn.outcome is not None:
        render_run(turn.outcome)
    if turn.arch_run is not None:
        render_arch_run(turn.arch_run)


def render_turn(turn: Turn) -> None:
    _render_user_message(turn.question)
    # Plain container, not st.chat_message: chat_message always renders an
    # avatar and there is no supported way to suppress it.
    with st.container():
        render_reply(turn)


def _queue_question(question: str) -> None:
    """Example-chip callback: the rerun this click triggers runs the question."""
    st.session_state.pending = question


def _new_chat() -> None:
    """Clear the conversation. The demo run count deliberately survives."""
    st.session_state.turns = []
    st.session_state.pending = None


def render_examples() -> None:
    st.caption("Try an example:")
    with st.container(key="example_chips", horizontal=True, gap="small"):
        for i, example in enumerate(EXAMPLES):
            st.button(
                example, key=f"example_{i}", on_click=_queue_question, args=(example,)
            )


def render_sidebar(
    default_provider: str, default_model: str
) -> tuple[RunChoice, DeltaGenerator]:
    """Build the sidebar. Returns the run settings and the counter's slot."""
    with st.sidebar:
        st.subheader("About")
        st.markdown(ABOUT)
        # Filled by show_demo_counter(), which runs again after a run so the
        # count shown is never one behind.
        counter = st.empty()
        st.button("New chat", key="new_chat", on_click=_new_chat, width="stretch")

        with st.expander("Use your own API key"):
            provider = st.selectbox(
                "Provider",
                SELECTABLE_PROVIDERS,
                index=SELECTABLE_PROVIDERS.index(default_provider),
                format_func=lambda p: PROVIDERS[p].label,
                key="provider",
            )
            spec = PROVIDERS[provider]
            # Always starts empty. The server key is never a widget value.
            own_key = st.text_input(
                f"{spec.label} API key",
                type="password",
                key=f"api_key_{provider}",
                placeholder=spec.key_hint,
                help=(
                    "Used only for this session. It is never written to disk, "
                    "logged, or included in traces."
                ),
            )

        with st.expander("Advanced settings"):
            # Widget keys include the provider, so each provider keeps its own
            # selection. Switching provider cannot leave another provider's
            # model selected - the incompatible combination is unrepresentable
            # rather than merely validated against.
            configured = default_model if provider == default_provider else ""
            options = model_options(provider, configured)
            choice = st.selectbox(
                "Model",
                options,
                index=options.index(configured) if configured in options else 0,
                key=f"model_{provider}",
            )
            model = (
                st.text_input(
                    "Custom model ID",
                    value="",
                    key=f"custom_model_{provider}",
                    placeholder=spec.models[0],
                ).strip()
                if choice == CUSTOM_MODEL
                else choice
            )
            # Demo mode = the server key would be used. The sliders are then
            # shown disabled at the fixed budgets; answer() enforces the same
            # values server-side. Separate widget keys per mode keep the demo
            # sliders at the fixed values and preserve a visitor's own choices.
            demo_mode = not (own_key or "").strip() and server_key_configured(
                provider
            )
            mode = "demo" if demo_mode else "own"
            max_attempts = st.slider(
                "Max attempts per step", 1, 5, DEMO_MAX_ATTEMPTS,
                disabled=demo_mode, key=f"max_attempts_{mode}",
            )
            max_steps = st.slider(
                "Max analysis steps", 1, 5, DEMO_MAX_STEPS,
                disabled=demo_mode, key=f"max_steps_{mode}",
            )
            if demo_mode:
                st.caption(DEMO_FIXED_CAPTION)

    run = RunChoice(
        provider=provider,
        model=model,
        own_key=(own_key or "").strip(),
        max_attempts=max_attempts,
        max_steps=max_steps,
    )
    return run, counter


def show_demo_counter(slot: DeltaGenerator, run: RunChoice) -> None:
    """'Demo runs left' while the server key is the one that would be used."""
    if run.own_key or not server_key_configured(run.provider):
        slot.empty()
        return
    slot.caption(
        f"Demo runs left: {demo_runs_left(st.session_state)} of {DEMO_RUN_LIMIT}"
    )


def key_format_warning(run: RunChoice) -> str | None:
    """Only warn where the provider actually guarantees a key format. Gemini
    keys are mid-migration (AIza... -> AQ....), so no assumption there."""
    spec = PROVIDERS[run.provider]
    if not (run.own_key and spec.key_prefixes):
        return None
    if run.own_key.startswith(spec.key_prefixes):
        return None
    expected = " or ".join(f"`{p}`" for p in spec.key_prefixes)
    return (
        f"That does not look like a {spec.label} key (expected it to start "
        f"with {expected}). Trying anyway."
    )


def main() -> None:
    st.set_page_config(page_title="Agentic Text-to-SQL", layout="wide")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    st.session_state.setdefault("turns", [])
    st.session_state.setdefault("pending", None)

    run, counter = render_sidebar(*configured_defaults())
    show_demo_counter(counter, run)

    st.title("Agentic Text-to-SQL")
    # Checked in the code: run_question() receives only the current question,
    # never the earlier turns, so the chat look must not imply memory.
    st.caption(
        "Each question is answered independently: the agent does not see "
        "earlier questions in this chat."
    )

    # st.chat_input is pinned to the bottom of the page wherever it is called.
    # Calling it before the history means this script run already knows
    # whether a question was submitted, so the example chips can be skipped.
    submitted = st.chat_input("Ask a question about the sales data")
    question = submitted or st.session_state.pending
    st.session_state.pending = None

    for turn in st.session_state.turns:
        render_turn(turn)

    if question:
        # The question appears at once; the spinner then sits where the reply
        # will be, for the whole run.
        _render_user_message(question)
        with st.container():
            warning = key_format_warning(run)
            if warning:
                st.warning(warning)
            # Filled only once the question escalates (see stage_reporter).
            progress_slot = st.empty()

            def show_progress(text: str, fraction: float) -> None:
                progress_slot.progress(fraction, text=text)

            with st.spinner("Querying..."):
                turn = answer(
                    question,
                    provider=run.provider,
                    model=run.model,
                    own_key=run.own_key,
                    max_attempts=run.max_attempts,
                    max_steps=run.max_steps,
                    state=st.session_state,
                    show_progress=show_progress,
                )
            progress_slot.empty()
            render_reply(turn)
        st.session_state.turns = [*st.session_state.turns, turn]
        show_demo_counter(counter, run)
    elif not st.session_state.turns:
        render_examples()


if __name__ == "__main__":
    main()
