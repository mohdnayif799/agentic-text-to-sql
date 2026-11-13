"""AgentCrew Streamlit interface.

Kept deliberately plain. The interesting thing to demonstrate is the agent's
behaviour - whether one query was enough or the question escalated, which
tools each step used, the queries it wrote - so the UI's job is to expose
that clearly, not to be a dashboard. The answer reads as prose; how it was
reached sits one quiet line and one collapsed section below it.

Wording and number formats (headers, $ and % formats, which chart, step
labels, the summary line) live in agentcrew/presentation.py as pure,
unit-tested functions; this file does the layout.

The architecture comes from Settings.arch (AGENTCREW_ARCH). The default is D
(`full`): one query first, escalation only on a signal, then tools. `v1`
runs the original multi-step agent.

Run:  streamlit run app.py
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit.delta_generator import DeltaGenerator

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentcrew import data_panel, runlog, service  # noqa: E402
from agentcrew.archs.app_runner import AppRun  # noqa: E402
from agentcrew.config import Settings  # noqa: E402
from agentcrew.datasets.source import DataSource  # noqa: E402
from agentcrew.graph.build import RunOutcome  # noqa: E402
from agentcrew.llm import PROVIDERS, SELECTABLE_PROVIDERS  # noqa: E402
from agentcrew.presentation import (  # noqa: E402
    FINAL_TABLE,
    arch_summary,
    chart_plan,
    column_formats,
    column_order,
    count,
    escalation_reason,
    escape_dollars,
    seconds_text,
    step_label,
    summary,
)
from agentcrew.schemas import ExecutionResult  # noqa: E402
from agentcrew.service import (  # noqa: E402
    DEMO_MAX_ATTEMPTS,
    DEMO_MAX_STEPS,
    DEMO_RUN_LIMIT,
    Turn,
    answer,
    demo_runs_left,
    redact,
    server_key_configured,
    trace_content_enabled,  # noqa: E402, F401
)
from agentcrew.service import deployment_cap as _daily_cap  # noqa: E402

# The names this file had before the ask flow moved to agentcrew/service.py;
# tests still reach them here.
from agentcrew.service import log_run as _log_run  # noqa: E402, F401
from agentcrew.sql_editor import EditorRun, runs_left  # noqa: E402

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
# The run record itself goes out as one JSON line on stdout (agentcrew/runlog.py),
# which Cloud Logging stores as a structured entry.
runlog.configure()

# Must match primaryColor in .streamlit/config.toml (a test checks this), so
# charts use the same blue as buttons, links and the question bubble.
CHART_COLOR = "#2563EB"
# The sans family from [[theme.fontFaces]] (a test checks this). Charts draw
# their text themselves, so they name the font explicitly.
CHART_FONT = "IBM Plex Sans"
# Small muted text (captions, labels, chart axes): Tailwind slate-500. 4.8:1
# on white and 4.6:1 on the sidebar's #F8FAFC, above the 4.5:1 WCAG AA asks
# of small text. A test recomputes both from config.toml.
MUTED_TEXT = "#64748B"
# Chart gridlines and axis lines: the theme's borderColor.
GRID_COLOR = "#E2E8F0"

PAGE_CSS = """
<style>
/* Every rule is scoped to markup this file creates: its own classes
   (.user-msg, .note) or a keyed container's st-key-<key> class, the hook
   Streamlit documents. Never Streamlit's data-testid attributes or its
   generated class names, which change between releases. */

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
/* Small muted text: captions, labels and the run summary line, written by
   note() below. Not st.caption: it draws the text colour at 60% opacity,
   about 4.1:1 on white, under the 4.5:1 small text needs. Tabular figures
   keep the numbers in the summary line from shifting between runs (IBM
   Plex's digits are equal width already; this keeps it so for a fallback). */
p.note {
    font-size: 0.8125rem;
    line-height: 1.5;
    color: #64748B;
    font-variant-numeric: tabular-nums;
}
/* The answer as prose at body size: about 68 characters a line, the range
   that reads comfortably, where the old wide layout ran to 1,400 px. */
[class*="st-key-answer_"] p,
[class*="st-key-answer_"] li {
    max-width: 68ch;
    line-height: 1.6;
    font-variant-numeric: tabular-nums;
}
/* Example questions as chips. A radius of half a single line's height draws
   a pill when the question fits on one line and a rounded box when it wraps
   (a 999px radius pinched wrapped questions into a capsule); left-aligned
   so wrapped lines read like text. */
.st-key-example_chips button {
    border-radius: 1.25rem;
    font-size: 0.875rem;
    text-align: left;
}
/* The original agent's trace is an HTML table (st.table), where CSS can set
   tabular figures. st.dataframe draws on a canvas, where it cannot. */
[class*="st-key-trace_"] table {
    font-variant-numeric: tabular-nums;
}
</style>
"""

# One chip per path the agent can take, each with a caption naming it. The
# questions are worded exactly as in the final evaluation runs, where D
# answered them correctly on that path (tests/test_chat_ui.py pins which run).
# The delete example shows the read-only guard; it is not an evaluated question.
EXAMPLES: tuple[tuple[str, str], ...] = (
    ("How many customers signed up in 2025?", "answered directly"),
    ("What was total completed revenue in 2024?", "answered directly"),
    (
        "What was the 2024 completed revenue of our customer Idirs Rosis? "
        "Give the customer's ID and name with the revenue.",
        "escalates: fuzzy value search",
    ),
    (
        "Why did Hardware revenue drop so sharply from Q2 to Q3 2025?",
        "escalates: metric analysis",
    ),
    (
        "Using a straight-line trend fitted to monthly completed revenue from "
        "January 2024 to December 2025, what is the forecast for January 2026?",
        "escalates: trend forecast",
    ),
    ("Delete all customers in the LATAM region.", "refused: read-only database"),
)


def to_frame(result: ExecutionResult) -> pd.DataFrame:
    if not result.columns:
        return pd.DataFrame()
    return pd.DataFrame(result.rows, columns=result.columns)


def note(*lines: str, where: Any = None) -> None:
    """Small muted text, one line per argument: captions, labels, the run
    summary line (styled by .note in PAGE_CSS). The text is HTML-escaped, so
    words from the model, such as a step's reason, are shown, never run.

    One paragraph, lines joined by <br>: a paragraph's bottom margin is what
    offsets the negative margin Streamlit gives every markdown block, so a
    note keeps the normal gap to whatever follows it."""
    body = "<br>".join(html.escape(line) for line in lines)
    (where or st).markdown(f'<p class="note">{body}</p>', unsafe_allow_html=True)


def render_answer(text: str, slot: int) -> None:
    """The answer as prose at body size. Not st.success: its green box read
    as a status alert, and green is kept for real success states. The keyed
    container gives the prose its 68ch measure (PAGE_CSS); `slot` keeps the
    key unique per turn. Dollar signs are escaped, or markdown would read the
    text between two amounts as a formula."""
    with st.container(key=f"answer_{slot}"):
        st.markdown(escape_dollars(text))


def column_config(df: pd.DataFrame) -> dict[str, Any]:
    """Plain-words headers and number formats (presentation.column_formats):
    $33,475.85 for money, 12,873 for counts, 16.29% for rates, and IDs as
    plain left-aligned integers, since 1,133 right-aligned reads as a
    quantity."""
    config: dict[str, Any] = {}
    for name, fmt in column_formats(df).items():
        if fmt.number_format is None:
            config[name] = st.column_config.Column(fmt.label)
        else:
            config[name] = st.column_config.NumberColumn(
                fmt.label, format=fmt.number_format, alignment=fmt.align)
    return config


def show_table(df: pd.DataFrame) -> None:
    st.dataframe(df, width="stretch", hide_index=True,
                 column_config=column_config(df), column_order=column_order(df))


def maybe_chart(df: pd.DataFrame) -> None:
    chart = build_chart(df)
    if chart is not None:
        st.altair_chart(chart, width="stretch")


# Value-axis labels and tooltips by the value column's kind. d3 formats:
# "$~s" gives $250k / $1.2M on the axis, where exact amounts would crowd it.
_AXIS_FORMAT = {"money": "$~s", "fraction": ".0%", "count": "~s", "number": "~s"}
_TOOLTIP_FORMAT = {"money": "$,.2f", "fraction": ".2%", "count": ",d"}


def build_chart(df: pd.DataFrame) -> alt.Chart | None:
    """Chart only when it genuinely helps; presentation.chart_plan decides
    whether and which: a line in time order for months, quarters, years or
    dates, horizontal bars by value for categories (long names stay readable
    on a phone), never an ID as the value, nothing for a single row.

    Built with Altair rather than `st.bar_chart` because the built-in chart
    gives no control over plot padding, and the left-most y-axis tick was
    being clipped ("100" rendering as "00"). Ticks are still chosen by Vega
    from the data - nothing about the scale is hardcoded.
    """
    plan = chart_plan(df)
    if plan is None:
        return None
    axis = {"title": plan.value_title, "tickCount": 5, "labelPadding": 6,
            "titlePadding": 8}
    if plan.value_kind == "percent":  # already 0-100: show the sign only
        axis["labelExpr"] = "datum.value + '%'"
    else:
        axis["format"] = _AXIS_FORMAT[plan.value_kind]
    value_title = plan.value_title + (" (%)" if plan.value_kind == "percent" else "")
    tooltip = [
        alt.Tooltip(f"{plan.label}:N", title=plan.label_title),
        alt.Tooltip(f"{plan.value}:Q", title=value_title,
                    format=_TOOLTIP_FORMAT.get(plan.value_kind, ",.2f")),
    ]
    value = alt.Axis(**axis)
    if plan.kind == "line":
        # Ordinal x sorted as text: for these label forms that is time order.
        chart = alt.Chart(df).mark_line(
            color=CHART_COLOR, point=alt.OverlayMarkDef(color=CHART_COLOR),
        ).encode(
            x=alt.X(f"{plan.label}:O", sort="ascending", title=None,
                    axis=alt.Axis(labelAngle=0, labelOverlap="greedy")),
            y=alt.Y(f"{plan.value}:Q", axis=value),
            tooltip=tooltip,
        )
    else:
        chart = alt.Chart(df).mark_bar(color=CHART_COLOR).encode(
            y=alt.Y(f"{plan.label}:N", sort="-x", title=None,
                    axis=alt.Axis(labelLimit=160)),
            x=alt.X(f"{plan.value}:Q", axis=value),
            tooltip=tooltip,
        )
    return (
        chart.properties(
            height=plan.height,
            # The clipping fix. `autosize` with contains="padding" makes Vega
            # fit the whole drawing - axis labels included - inside the
            # container instead of letting the widest tick label run off the
            # left edge, and the explicit padding reserves room for it.
            padding={"left": 16, "right": 16, "top": 10, "bottom": 10},
            autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        )
        .configure(font=CHART_FONT)
        .configure_view(strokeWidth=0)
        .configure_axis(labelFontSize=12, titleFontSize=12, labelColor=MUTED_TEXT,
                        titleColor=MUTED_TEXT, titleFontWeight=500,
                        gridColor=GRID_COLOR, domainColor=GRID_COLOR,
                        tickColor=GRID_COLOR)
    )


CUSTOM_MODEL = "Custom..."

# ---- Demo mode ---------------------------------------------------------------
# The ask flow (which key pays, the allowances, the daily cap, the run and
# its log line) lives in agentcrew/service.py, shared with the REST API.
# This file draws it. The server key is never placed in a widget or in
# st.session_state: Streamlit sends every widget value to the browser, and
# a password field has a show/hide toggle.
DEMO_FIXED_CAPTION = "Fixed in demo mode. Add your own key to change."


# ---- Idle guard ----------------------------------------------------------------
# Streamlit holds one websocket per open tab and its client reconnects by
# itself, so an idle tab keeps a request in flight and Cloud Run keeps the
# instance (and its billing) alive indefinitely. Two halves end such a session:
#
# * In the browser: the small script below (components.html, so it runs)
#   watches input in the page and, after `idle_timeout_seconds` without any,
#   leaves for the static page static/idle.html. Leaving unloads the Streamlit
#   client, which closes the connection for good.
# * On the server: each script run stamps the session's last activity. A run
#   that arrives after the timeout (the script is off, or the tab came back
#   from suspension) finds the conversation cleared and a notice; Phase 2 also
#   deletes the session's uploaded data here.
LAST_ACTIVITY = "last_activity"  # st.session_state key, time.monotonic()
IDLE_PAGE = "/app/static/idle.html"  # server.enableStaticServing serves static/
IDLE_MESSAGE = (
    "This session ended after a while without activity, so the conversation was "
    "cleared. Ask a new question to start again."
)
# The component iframe is sandboxed without allow-top-navigation, so a
# redirect from inside it is refused ("Unsafe attempt to initiate navigation",
# seen in a browser check). It is same-origin with scripts allowed, though, so
# it may add a script to the page itself; the timer then lives in the page,
# navigates as the page, and survives the reruns that re-mount the iframe.
# Installed once per page (the flag), whatever the number of reruns.
IDLE_GUARD_HTML = """\
<script>
(function () {
  var timeout = %d * 1000;
  var target = %s;
  var page = window.parent;
  if (page.__agentcrewIdleGuard) { return; }
  function install(timeout, target) {
    if (window.__agentcrewIdleGuard) { return; }
    window.__agentcrewIdleGuard = true;
    var last = Date.now();
    function bump() { last = Date.now(); }
    ["mousemove", "mousedown", "keydown", "touchstart", "scroll", "click"]
      .forEach(function (name) {
        document.addEventListener(name, bump, {passive: true, capture: true});
      });
    setInterval(function () {
      if (Date.now() - last > timeout) { window.location.replace(target); }
    }, 15000);
  }
  var script = page.document.createElement("script");
  script.textContent = "(" + install.toString() + ")(" + timeout + "," +
    JSON.stringify(target) + ");";
  page.document.head.appendChild(script);
})();
</script>
"""


def idle_timeout() -> int:
    """AGENTCREW_IDLE_TIMEOUT_SECONDS, or the default when it cannot be read."""
    try:
        return int(Settings().idle_timeout_seconds)
    except Exception:
        return int(Settings.model_fields["idle_timeout_seconds"].default)


def session_expired(state: MutableMapping[str, Any], *, now: float,
                    timeout: float) -> bool:
    """True when the session's last activity is older than the timeout. A
    session with no stamp yet is new, not stale."""
    last = state.get(LAST_ACTIVITY)
    return last is not None and now - last > timeout


def touch(state: MutableMapping[str, Any], *, now: float) -> None:
    state[LAST_ACTIVITY] = now


def end_session(state: MutableMapping[str, Any]) -> None:
    """Forget the conversation. The demo run count survives on purpose, as it
    does for 'New chat'."""
    state["turns"] = []
    state["pending"] = None


def idle_guard_html(timeout_seconds: int) -> str:
    return IDLE_GUARD_HTML % (int(timeout_seconds), json.dumps(IDLE_PAGE))


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


STEPS_LABEL = "How this was answered"


def render_arch_run(run: AppRun, slot: int) -> None:
    """Answer, then one muted line on how it was reached (path, steps,
    repairs, model calls, time) and why it escalated in plain words, the
    final table, and the steps collapsed under STEPS_LABEL. The line
    replaces five st.metric tiles that were louder than the answer."""
    outcome = run.outcome
    if outcome.status in ("done", "refused"):
        render_answer(outcome.answer or "", slot)
    else:
        st.error(outcome.answer or "The agent stopped without an answer.")
    note(arch_summary(outcome, llm_calls=run.llm_calls, seconds=run.seconds),
         escalation_reason(outcome))
    for caveat in outcome.caveats:
        st.warning(escape_dollars(caveat))

    if outcome.selected is not None:
        df = to_frame(outcome.selected)
        with st.container(gap="small"):  # keeps the label next to its table
            note("Final result")
            if df.empty:
                note("No rows returned.")
            else:
                show_table(df)
        if not df.empty:
            maybe_chart(df)
        final = [s for s in outcome.steps if s.get("step") == "final"]
        if final:
            made = final[0].get("tools", ["assemble"])[0]
            note(f"Final table: {FINAL_TABLE.get(made, made)}.")

    steps = [s for s in outcome.steps if s.get("step") != "final"]
    if steps:
        with st.expander(STEPS_LABEL):
            for step in steps:
                with st.expander(step_label(step, outcome.repairs)):
                    if step.get("reason"):
                        note(step["reason"])
                    if step.get("sql"):
                        st.code(step["sql"], language="sql", wrap_lines=True)


def render_run(outcome: RunOutcome, slot: int) -> None:
    """The original agent's reply (AGENTCREW_ARCH=v1), laid out like D's:
    prose, one muted summary line, everything else collapsed."""
    if outcome.status == "done":
        render_answer(outcome.answer or "", slot)
    elif outcome.status == "needs_clarification":
        render_answer(f"**I need one clarification:** {outcome.answer}", slot)
    else:
        st.error(outcome.answer or "The agent stopped without an answer.")
        note(f"Stop reason: {outcome.stop_reason}")

    if outcome.key_numbers:
        # These are free-form strings from the model ("North America: 84 lost
        # customers"). st.metric renders them at display size and clips them
        # ("North America: 8..."), so they are rendered as wrapping text.
        with st.container(key=f"answer_{slot}_figures"):
            st.markdown("**Key figures**\n\n" + "\n".join(
                f"- {escape_dollars(item)}" for item in outcome.key_numbers))

    counters = outcome.trace["counters"]
    note(summary(count(len(outcome.step_results), "step"),
                 count(counters["sql_executions"], "SQL run"),
                 count(counters["repairs"], "repair"),
                 count(counters["llm_calls"], "LLM call"),
                 seconds_text(outcome.trace["elapsed_seconds"])))
    for caveat in outcome.caveats:
        st.warning(escape_dollars(caveat))

    with st.expander(STEPS_LABEL):
        render_run_details(outcome, slot)


def render_run_details(outcome: RunOutcome, slot: int) -> None:
    """Each step with its query and result, the agent trace and the schema
    selection. Step 1 opens with the section: v1 has no separate final
    table, so its first step's result is the one to see first."""
    for i, step in enumerate(outcome.step_results, start=1):
        badge = "verified" if step.verified else "not verified"
        with st.expander(
            f"Step {i}: {step.goal} ({count(step.attempts_used, 'attempt')}, "
            f"{badge})",
            expanded=(i == 1),
        ):
            st.code(step.sql, language="sql", wrap_lines=True)
            df = to_frame(step.result)
            if df.empty:
                note("No rows returned.")
            else:
                show_table(df)
                maybe_chart(df)
            note(
                f"{count(step.result.row_count, 'row')} in "
                f"{step.result.duration_ms:.0f} ms"
                + (", truncated at the row cap" if step.result.truncated else "")
            )

    with st.expander("Agent trace"):
        note(
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
        with st.container(key=f"trace_{slot}"):
            st.table(spans.set_index("node"))

    with st.expander("Schema selection"):
        st.write("**Tables shown to the model:**", ", ".join(outcome.selected_tables))
        dbg = outcome.schema_debug
        if dbg.get("seeded_by"):
            st.write("Lexically matched:", ", ".join(dbg["seeded_by"]))
        if dbg.get("expanded_by_fk"):
            st.write("Added via foreign keys:", ", ".join(dbg["expanded_by_fk"]))
        note(
            "Selection is deterministic: token overlap plus one-hop foreign-key "
            "closure. No LLM call is spent here."
        )


def describer(run: RunChoice) -> data_panel.Describer:
    """Drafts a dataset's descriptions on this session's key choice
    (agentcrew/service.describer)."""
    return service.describer(provider=run.provider, model=run.model,
                             own_key=run.own_key)


def render_data_panel(run: RunChoice) -> DataSource | None:
    """The 'Your data' expander (agentcrew/data_panel.py); returns the source
    this session's questions are answered from, None for NorthStar."""
    try:
        settings = Settings()
    except Exception:
        return None
    if not (settings.uploads_enabled or settings.database_url):
        return None
    cap = _daily_cap()
    try:
        return data_panel.render(settings=settings, describe=describer(run),
                                 reserve_upload=cap.reserve_upload,
                                 release_upload=cap.release_upload)
    except Exception as exc:
        # The panel must never take the app down with it; NorthStar stays.
        # The detail goes to the log through redact(), which also scrubs the
        # database URL; the browser gets a fixed sentence.
        log.warning("data panel failed: %s: %s", type(exc).__name__,
                    " ".join(redact(str(exc), "").split())[:400])
        st.sidebar.error("Your data could not be loaded. Questions use the "
                         "sample data.")
        return None


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


def render_reply(turn: Turn, slot: int, source: DataSource | None = None) -> None:
    """`slot` is the turn's position in the conversation; it keeps the keys
    of the reply's containers unique on the page. `source` is the data
    source selected now (None = NorthStar), for the SQL editor."""
    if turn.notice:
        st.warning(turn.notice)
    if turn.error:
        st.error(turn.error)
    if turn.outcome is not None:
        render_run(turn.outcome, slot)
    if turn.arch_run is not None:
        render_arch_run(turn.arch_run, slot)
    render_sql_editor(turn, slot, source)


def render_turn(turn: Turn, slot: int, source: DataSource | None = None) -> None:
    _render_user_message(turn.question)
    # Plain container, not st.chat_message: chat_message always renders an
    # avatar and there is no supported way to suppress it.
    with st.container():
        render_reply(turn, slot, source)


# ---- SQL editor (agentcrew/sql_editor.py) -------------------------------------
SQL_EDITOR_LABEL = "Edit and run the SQL"
SQL_OTHER_SOURCE = (
    "This answer came from a different data source than the one selected now, "
    "so its SQL can't run here."
)


def source_id(source: DataSource | None) -> str:
    """A data source's identity that survives edits of its descriptions
    (which bump the revision at the end of its key)."""
    return "northstar" if source is None else source.key.rsplit(":", 1)[0]


def final_sql(turn: Turn) -> str | None:
    """The SQL behind the answer's final table: what the editor starts from."""
    if turn.arch_run is not None and turn.arch_run.outcome.selected is not None:
        return turn.arch_run.outcome.selected.sql or None
    if turn.outcome is not None and turn.outcome.step_results:
        return turn.outcome.step_results[-1].sql or None
    return None


def run_editor_sql(sql: str, source: DataSource | None,
                   state: MutableMapping[str, Any]) -> tuple[EditorRun, int]:
    """Run the editor's SQL on the agent's own ToolContext for `source`
    (agentcrew/service.run_sql_on). Returns the run and the row cap it ran
    under. No model call is made."""
    return service.run_sql_on(sql, source, state=state)


def _session_sql_cap() -> int:
    try:
        return Settings().session_sql_run_cap
    except Exception:
        return Settings.model_fields["session_sql_run_cap"].default


def render_sql_editor(turn: Turn, slot: int, source: DataSource | None) -> None:
    """A collapsed expander under the answer, prefilled with its final SQL.
    Runs go through tools.execute_readonly_sql (agentcrew/sql_editor.py):
    the agent's guard, LIMIT, row cap and timeout, never a model call."""
    sql = final_sql(turn)
    if sql is None:
        return
    # Keyed, with its open state tracked: otherwise the rerun a Run click
    # triggers draws it as a new element, collapsed, hiding the result.
    with st.expander(SQL_EDITOR_LABEL, expanded=False, key=f"sql_editor_{slot}",
                     on_change="rerun"):
        if turn.source_id != source_id(source):
            note(SQL_OTHER_SOURCE)
            st.code(sql, language="sql", wrap_lines=True)
            return
        text = st.text_area("SQL", value=sql, key=f"sql_text_{slot}", height=160,
                            label_visibility="collapsed")
        result_key = f"sql_result_{slot}"
        if st.button("Run", key=f"sql_run_{slot}"):
            st.session_state[result_key] = run_editor_sql(text, source,
                                                          st.session_state)
        if result_key in st.session_state:
            render_editor_run(*st.session_state[result_key])
        cap = _session_sql_cap()
        left = runs_left(st.session_state, cap)
        if left is not None:
            note(f"SQL runs left this session: {left} of {cap}. Read-only, with "
                 "the same checks and limits as the agent's queries.")


def render_editor_run(run: EditorRun, max_rows: int) -> None:
    if run.message:
        # A refused or failed statement is an error; a limit or empty input
        # (nothing ran) is a notice.
        (st.error if run.charged else st.warning)(run.message)
    result = run.result
    if result is None or not result.ok:
        return
    df = to_frame(result)
    if df.empty:
        note("No rows returned.")
    else:
        show_table(df)
    capped = result.truncated or (max_rows > 0 and result.row_count >= max_rows)
    note(f"{count(result.row_count, 'row')} in {result.duration_ms:.0f} ms"
         + (f", capped at {max_rows:,} rows" if capped else ""))


def _queue_question(question: str) -> None:
    """Example-chip callback: the rerun this click triggers runs the question."""
    st.session_state.pending = question


def _new_chat() -> None:
    """Clear the conversation. The demo run count deliberately survives."""
    st.session_state.turns = []
    st.session_state.pending = None


def render_examples() -> None:
    note("Try an example:")
    with st.container(key="example_chips", horizontal=True, gap="medium"):
        for i, (example, shows) in enumerate(EXAMPLES):
            # A content-width box per chip keeps its caption directly under it
            # while the chips still wrap side by side.
            with st.container(key=f"example_box_{i}", width="content", gap=None):
                st.button(
                    example, key=f"example_{i}", on_click=_queue_question,
                    args=(example,),
                )
                note(shows)


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
                note(DEMO_FIXED_CAPTION)

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
    note(f"Demo runs left: {demo_runs_left(st.session_state)} of {DEMO_RUN_LIMIT}",
         where=slot)


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
    # Centered: one readable column. "wide" stretched answer lines to about
    # 1,400 px on a desktop screen.
    st.set_page_config(page_title="AgentCrew: Text-to-SQL Agent", layout="centered")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    st.session_state.setdefault("turns", [])
    st.session_state.setdefault("pending", None)

    # Idle guard, server half: every script run is an interaction, so a run
    # that finds the stamp older than the timeout is a session coming back
    # from idle. It starts over, and says so.
    timeout = idle_timeout()
    now = time.monotonic()
    expired = session_expired(st.session_state, now=now, timeout=timeout)
    if expired:
        end_session(st.session_state)
        try:
            data_panel.drop_session_data(st.session_state, Settings())
        except Exception as exc:  # the sweeper deletes it later regardless
            log.warning("could not drop an idle session's data: %s",
                        type(exc).__name__)
    touch(st.session_state, now=now)

    run, counter = render_sidebar(*configured_defaults())
    source = render_data_panel(run)
    show_demo_counter(counter, run)

    st.title("Agentic Text-to-SQL")
    # Checked in the code: run_question() receives only the current question,
    # never the earlier turns, so the chat look must not imply memory.
    note(
        "Each question is answered independently: the agent does not see "
        "earlier questions in this chat."
    )
    if expired:
        st.warning(IDLE_MESSAGE)
    if source is not None:
        note(f"Answering from {source.label}. The sample questions below the "
             "chat do not apply to it.")
    # Idle guard, browser half (height 0: nothing to see). Last in the page
    # order would be tidier, but a component mounts where it is called and
    # the chat input below is pinned to the bottom regardless.
    components.html(idle_guard_html(timeout), height=0)

    # st.chat_input is pinned to the bottom of the page wherever it is called.
    # Calling it before the history means this script run already knows
    # whether a question was submitted, so the example chips can be skipped.
    submitted = st.chat_input("Ask a question about the sales data"
                              if source is None else "Ask a question about your data")
    question = submitted or st.session_state.pending
    st.session_state.pending = None

    for slot, turn in enumerate(st.session_state.turns):
        render_turn(turn, slot, source)

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
                    stages=lambda max_steps: stage_reporter(show_progress,
                                                            max_steps),
                    source=source,
                )
            turn = replace(turn, source_id=source_id(source))
            progress_slot.empty()
            # The same slot it gets from the loop above on later reruns.
            render_reply(turn, len(st.session_state.turns), source)
        st.session_state.turns = [*st.session_state.turns, turn]
        show_demo_counter(counter, run)
    elif not st.session_state.turns and source is None:
        render_examples()


if __name__ == "__main__":
    main()
