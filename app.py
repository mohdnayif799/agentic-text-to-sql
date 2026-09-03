"""AgentCrew Streamlit interface.

Kept deliberately plain. The interesting thing to demonstrate is the agent's
behaviour - the plan, the queries it wrote, the failures it recovered from -
so the UI's job is to expose the trace clearly, not to be a dashboard.

Run:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentcrew.config import Settings  # noqa: E402
from agentcrew.db.engine import DatabaseNotFoundError  # noqa: E402
from agentcrew.graph.build import RunOutcome, build_deps, run_question  # noqa: E402
from agentcrew.llm import PROVIDERS, SELECTABLE_PROVIDERS  # noqa: E402
from agentcrew.schemas import ExecutionResult  # noqa: E402

st.set_page_config(page_title="AgentCrew Analytics", page_icon="*", layout="wide")

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
        return
    labels = [
        c
        for c in df.columns
        if pd.api.types.is_string_dtype(df[c])
        and not pd.api.types.is_numeric_dtype(df[c])
    ]
    numbers = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not (labels and numbers):
        return

    label, value = labels[0], numbers[0]
    chart = (
        alt.Chart(df)
        .mark_bar()
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
    st.altair_chart(chart, use_container_width=True)


CUSTOM_MODEL = "Custom..."


def env_fallback_key(provider: str) -> str:
    """Key from .env or the environment, if one is configured.

    The UI field takes precedence, so a developer with a .env keeps their
    workflow while anyone cloning the repo can simply paste their own key.
    """
    try:
        return getattr(Settings(), PROVIDERS[provider].key_field) or ""
    except Exception:
        return ""


def redact(text: str, secret: str) -> str:
    """Never echo the key back into the UI, even inside a provider error."""
    if secret and len(secret) > 8:
        return text.replace(secret, "***")
    return text


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
        st.warning(caveat, icon=":material/info:")

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
                st.dataframe(df, use_container_width=True, hide_index=True)
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
    error: str | None = None


def execute(question: str, cfg: dict, spec, key: str) -> Turn:
    """Run one question. Failures are returned on the Turn, never raised."""
    turn = Turn(question=question)
    try:
        settings = Settings(**cfg)
        deps = build_deps(settings)
    except DatabaseNotFoundError as exc:
        turn.error = str(exc)
        return turn
    except Exception as exc:
        turn.error = (
            f"Could not start the agent: {redact(str(exc), key)}\n\n"
            f"Check the API key in the sidebar. If the SDK is missing, run "
            f"`{spec.install}`."
        )
        return turn

    with st.spinner("Planning, querying, verifying..."):
        try:
            turn.outcome = run_question(question, settings=settings, deps=deps)
        except Exception as exc:
            turn.error = f"Run failed: {type(exc).__name__}: {redact(str(exc), key)}"
    return turn


def main() -> None:
    st.title("AgentCrew - Autonomous Analytics Agent")
    st.caption(
        "Ask a question in plain English. The agent inspects the schema, plans "
        "the analysis, writes SQL, runs it against a read-only database, reads "
        "the actual result, repairs failures, and verifies the answer before "
        "reporting it."
    )

    with st.sidebar:
        st.header("Configuration")
        provider = st.selectbox(
            "Provider",
            SELECTABLE_PROVIDERS,
            index=0,
            format_func=lambda p: PROVIDERS[p].label,
        )
        spec = PROVIDERS[provider]

        fallback = env_fallback_key(provider)
        api_key = st.text_input(
            f"{spec.label} API key",
            value=fallback,
            type="password",
            key=f"api_key_{provider}",
            placeholder=spec.key_hint,
            help=(
                "Used only for this session. It is never written to disk, "
                "logged, or included in traces."
            ),
        )
        if fallback:
            st.caption("Loaded from your local `.env`. You can override it here.")

        # Widget keys include the provider, so each provider keeps its own
        # selection. Switching provider cannot leave another provider's model
        # selected - the incompatible combination is unrepresentable rather
        # than merely validated against.
        choice = st.selectbox(
            "Model",
            [*spec.models, CUSTOM_MODEL],
            index=0,
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

        max_attempts = st.slider("Max attempts per step", 1, 5, 3)
        max_steps = st.slider("Max analysis steps", 1, 5, 3)
        st.divider()
        st.caption(
            "**Read-only by design.** The database handle is opened with "
            "`mode=ro` and `PRAGMA query_only=ON`, and every statement is "
            "AST-checked before execution. Try the delete example to see it "
            "refuse."
        )

    st.session_state.setdefault("turns", [])
    st.session_state.setdefault("pending", None)
    st.session_state.setdefault("pending_key", "")

    # --- run anything queued by the previous script run --------------------
    # Done before rendering so the new turn appears in place, in order.
    pending = st.session_state.pending
    if pending:
        st.session_state.pending = None
        pending_key = st.session_state.pending_key
        st.session_state.pending_key = ""
        st.session_state.turns.append(
            execute(
                pending,
                {
                    "provider": provider,
                    "max_attempts_per_step": max_attempts,
                    "max_steps": max_steps,
                    "model": model,
                    spec.key_field: pending_key,
                },
                spec,
                pending_key,
            )
        )

    # --- history: one section per submitted question -----------------------
    turns = st.session_state.turns
    for i, turn in enumerate(turns, start=1):
        with st.container():
            st.markdown(f"#### Q{i}. {turn.question}")
            if turn.error:
                st.error(turn.error)
            if turn.outcome is not None:
                render_run(turn.outcome)
        st.divider()

    # --- the single active input, always after the last result -------------
    # Keyed on the turn count, so finishing a question produces a brand-new
    # empty widget rather than needing to be cleared. There is never more than
    # one question box on the page.
    idx = len(turns)
    field = f"question_{idx}"
    if idx == 0:
        st.session_state.setdefault(field, EXAMPLES[0])

    st.write("**Try one:**")
    cols = st.columns(3)
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 3].button(ex, use_container_width=True, key=f"ex_{idx}_{i}"):
            # Writes to the active field by construction, so presets can never
            # target a question that has already been answered.
            st.session_state[field] = ex
            st.rerun()

    question = st.text_area(
        "Your question" if idx == 0 else "Ask another question",
        key=field,
        height=80,
        placeholder="e.g. Which product category drove that decline?",
    )

    if st.button("Run agent", type="primary", key=f"run_{idx}"):
        text = (question or "").strip()
        key = (api_key or "").strip()
        if not text:
            st.warning("Type a question first.")
        elif not key:
            st.error(
                f"Enter your {spec.label} API key in the sidebar to run the agent."
            )
            st.caption(
                "The key is used only for this session. It is never stored, "
                "logged, or written into the run traces."
            )
        elif not model:
            st.error("Enter a custom model ID, or pick one from the list.")
        else:
            # Only warn where the provider actually guarantees a format. Gemini
            # keys are mid-migration (AIza... -> AQ....), so no assumption.
            if spec.key_prefixes and not key.startswith(spec.key_prefixes):
                expected = " or ".join(f"`{p}`" for p in spec.key_prefixes)
                st.warning(
                    f"That does not look like a {spec.label} key (expected it "
                    f"to start with {expected}). Trying anyway."
                )
            st.session_state.pending = text
            st.session_state.pending_key = key
            st.rerun()


if __name__ == "__main__":
    main()
