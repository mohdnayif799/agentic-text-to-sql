"""How the Streamlit app presents a run. Pure functions, no Streamlit.

app.py decides the layout; this module decides the words and the number
formats: column headers in plain words, the format each column gets, which
chart (if any) a result gets, step labels, the run summary line and the
plain-words reason a question escalated. Nothing here is read by the agent or
the evaluation, so changing it changes only what a visitor sees.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from agentcrew.archs.outcome import ArchOutcome, RepairEvent
from agentcrew.compute.metrics import COLUMNS as METRIC_COLUMNS

# ---- Column headers ------------------------------------------------------------
_UPPER = {"id": "ID", "ids": "IDs", "aov": "AOV", "yoy": "YoY", "sql": "SQL"}
_WORDS = {"avg": "average", "pct": "%", "num": "number", "qty": "quantity",
          "ym": "year-month"}
_AGGREGATE = re.compile(
    r"(count|sum|avg|min|max)\s*\(\s*(distinct\s+)?([\w.]+|\*)\s*\)", re.IGNORECASE)
_AGGREGATE_WORDS = {"count": "count of", "sum": "total", "avg": "average",
                    "min": "minimum", "max": "maximum"}


def _word(token: str) -> str:
    low = token.lower()
    if low in _UPPER:
        return _UPPER[low]
    if re.fullmatch(r"q[1-4]", low) or len(low) == 1:
        return low.upper()  # Q2, value_a -> Value A
    return _WORDS.get(low, low)


def _words(name: str) -> str:
    return " ".join(_word(t) for t in re.split(r"[_\s]+", name.strip()) if t)


def humanize(name: str) -> str:
    """A column name in plain words, sentence case: customer_id -> Customer ID,
    pct_change -> % change, SUM(amount) -> Total amount, COUNT(*) -> Count."""
    found = _AGGREGATE.fullmatch(name.strip())
    if found is None:
        text = _words(name)
    elif found[3] == "*":
        text = "count"
    else:
        distinct = "distinct " if found[2] else ""
        column = _words(found[3].split(".")[-1])  # o.amount -> amount
        text = f"{_AGGREGATE_WORDS[found[1].lower()]} {distinct}{column}"
    return text[:1].upper() + text[1:]


# ---- Number formats ------------------------------------------------------------
@dataclass(frozen=True)
class ColumnFormat:
    """How one result column is shown in st.dataframe."""

    label: str
    kind: str
    """identifier, money, fraction, percent, count, number or text."""
    number_format: str | None = None
    """A Streamlit NumberColumn format: a preset ("dollar", "percent") or printf."""
    align: str | None = None


MONEY_WORDS = ("revenue", "amount", "spend", "price", "cost", "value", "margin")
RATE_WORDS = frozenset({"pct", "percent", "percentage", "rate", "share", "ratio"})
CALENDAR_WORDS = frozenset({"year", "quarter", "month", "week", "day"})
#: The metric analysis table (compute/metrics.py COLUMNS) stacks rows of
#: different units in these columns: revenue, order counts, discount rates.
#: "value" in their names must not turn an order count into dollars.
MIXED_UNIT_COLUMNS = frozenset({"value_a", "value_b", "change"})

_FORMATS: dict[str, tuple[str, str | None]] = {
    "identifier": ("%d", "left"),   # 1133, not 1,133; left like a label
    "money": ("dollar", None),      # $33,475.85
    "fraction": ("percent", None),  # 0.1234 -> 12.34%
    "percent": ("%.2f%%", None),    # 16.2866 -> 16.29%
    "count": ("%,d", None),         # 12,873
    "number": ("%,.2f", None),      # 1,220,726.85
}


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _integral(column: pd.Series) -> bool:
    if pd.api.types.is_integer_dtype(column):
        return True
    values = column.dropna()
    return (pd.api.types.is_float_dtype(column) and not values.empty
            and bool((values == values.round()).all()))


def column_kind(df: pd.DataFrame, name: str) -> str:
    """identifier, money, fraction, percent, count, number or text.

    Checked in this order: IDs (and integer years, months ...) are labels, so
    never quantities; a rate is never money (refund_rate, gross_margin_pct),
    and whether it is a fraction or already a percentage is read from its
    values (all within -1..1 means a fraction); then money by name; then
    whole numbers are counts and the rest plain numbers.
    """
    column = df[name]
    if (pd.api.types.is_bool_dtype(column)
            or not pd.api.types.is_numeric_dtype(column)):
        return "text"
    tokens = _tokens(str(name))
    last = tokens[-1] if tokens else ""
    integral = _integral(column)
    if last == "id" or (integral and last in CALENDAR_WORDS):
        return "identifier"
    if RATE_WORDS & set(tokens):
        return "fraction" if bool((column.dropna().abs() <= 1).all()) else "percent"
    mixed = tuple(df.columns) == METRIC_COLUMNS and name in MIXED_UNIT_COLUMNS
    if not mixed and any(word in str(name).lower() for word in MONEY_WORDS):
        return "money"
    return "count" if integral and not mixed else "number"


#: The metric analysis table's columns, findings first: each item and its
#: numbers, then which analysis it belongs to and the two compared sides,
#: which repeat on every row.
METRIC_COLUMN_ORDER = ("item", "value_a", "value_b", "change", "pct_change",
                       "share_of_change_pct", "comparison", "largest_contributor",
                       "analysis", "side_a", "side_b")


def column_order(df: pd.DataFrame) -> list[str] | None:
    """The order to show a table's columns in, or None for the query's own.
    Only the metric analysis table is reordered: it opens with the compared
    periods, so in the centered column its numbers sat out of view."""
    return list(METRIC_COLUMN_ORDER) if tuple(df.columns) == METRIC_COLUMNS else None


def column_formats(df: pd.DataFrame) -> dict[str, ColumnFormat]:
    """Header and number format for every column, by column name. Empty when
    names repeat (SELECT a.x, b.x): a config keyed by name would be ambiguous,
    so Streamlit's defaults are kept."""
    if df.columns.duplicated().any():
        return {}
    formats = {}
    for name in df.columns:
        kind = column_kind(df, name)
        number_format, align = _FORMATS.get(kind, (None, None))
        formats[name] = ColumnFormat(humanize(str(name)), kind, number_format, align)
    return formats


# ---- Charts --------------------------------------------------------------------
CHART_HEIGHT = 260
BAR_STEP = 20
"""Pixels per bar once more than 13 bars would crowd CHART_HEIGHT."""
MIN_CHART_ROWS = 2
MAX_CHART_ROWS = 25
VALUE_KINDS = frozenset({"money", "fraction", "percent", "count", "number"})

_TIME_LABEL = re.compile(
    r"(19|20)\d\d"                                           # 2025
    r"|(19|20)\d\d-(0[1-9]|1[0-2])"                          # 2025-07
    r"|(19|20)\d\d[- ]?Q[1-4]"                               # 2025-Q3
    r"|(19|20)\d\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"    # 2025-07-01
    r"([ T]\d\d:\d\d(:\d\d(\.\d+)?)?)?"                      # ... 10:30:00
)


def is_time_labels(values: Iterable[object]) -> bool:
    """True if every label is a year, month, quarter or date in one of the
    forms above. In those forms text order is time order, so sorting the
    labels as text sorts them chronologically."""
    items = [v for v in values
             if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return bool(items) and all(
        isinstance(v, str) and _TIME_LABEL.fullmatch(v.strip()) for v in items)


@dataclass(frozen=True)
class ChartPlan:
    kind: Literal["line", "bar"]
    label: str
    value: str
    label_title: str
    value_title: str
    value_kind: str
    height: int


def chart_plan(df: pd.DataFrame) -> ChartPlan | None:
    """The chart a result gets, or None when a chart would not help.

    One text label per row (a repeated label would pile bars on top of each
    other) and the first numeric column that is a quantity: never an ID or
    a calendar number. Time labels get a line in time order, anything else
    horizontal bars by value. No chart for a single row or more than 25.
    """
    if not MIN_CHART_ROWS <= len(df) <= MAX_CHART_ROWS:
        return None
    formats = column_formats(df)
    labels = [c for c, f in formats.items() if f.kind == "text"
              and not pd.api.types.is_bool_dtype(df[c])
              and pd.api.types.is_string_dtype(df[c])]
    values = [c for c, f in formats.items() if f.kind in VALUE_KINDS]
    if not (labels and values):
        return None
    label, value = labels[0], values[0]
    if df[label].isna().any() or df[label].duplicated().any():
        return None
    line = is_time_labels(df[label])
    return ChartPlan(
        kind="line" if line else "bar", label=label, value=value,
        label_title=formats[label].label, value_title=formats[value].label,
        value_kind=formats[value].kind,
        height=CHART_HEIGHT if line else max(CHART_HEIGHT, BAR_STEP * len(df)),
    )


# ---- Steps ---------------------------------------------------------------------
#: Every tool a step can use (archs/explore.py ACTIONS and COMPUTE_FUNCTIONS),
#: by its id without the "compute:" prefix.
TOOL_LABELS = {
    "run_sql": "SQL query",
    "search_values": "Fuzzy value search",
    "get_distinct_values": "Distinct values lookup",
    "sample_rows": "Sample rows",
    "decompose_change": "Metric decomposition",
    "contribution_by_dimension": "Contribution analysis",
    "linear_trend_forecast": "Trend forecast",
    "zscore_anomalies": "Anomaly check",
    "pct_change": "Percent change",
    "yoy_growth": "Year-over-year growth",
    "compare_periods": "Period comparison",
    "share_of_total": "Share of total",
    "correlation": "Correlation",
}

#: How the final table was made, when it is not one step's own result.
FINAL_TABLE = {
    "assemble": "assembled from the steps below",
    "stack": "the metric analyses below, stacked",
    "fallback": "the first query's result (answering the escalated table failed)",
}


def tool_label(tool: str) -> str:
    name = tool.removeprefix("compute:")
    return TOOL_LABELS.get(name) or humanize(name)


def count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def step_label(step: Mapping[str, Any], repairs: Sequence[RepairEvent] = ()) -> str:
    """'Step 1: SQL query, 2 repairs' from a step record's tool ids."""
    tools = list(step.get("tools", []))
    names = " + ".join(tool_label(t) for t in tools if t != "repair")
    label = f"Step {step['step']}: {names or 'no tool'}"
    if "repair" in tools:
        made = sum(1 for r in repairs if r.step == step["step"])
        label += f", {count(max(made, 1), 'repair')}"
    return label


# ---- Summary line and escalation reason ----------------------------------------
def summary(*parts: str) -> str:
    return " · ".join(parts)


def seconds_text(seconds: float) -> str:
    return f"{seconds:.1f} s"


def arch_summary(outcome: ArchOutcome, *, llm_calls: int, seconds: float) -> str:
    """'Escalated · 3 steps · 2 repairs · 6 LLM calls · 9.3 s'."""
    steps = [s for s in outcome.steps if s.get("step") != "final"]
    return summary(
        "Escalated" if outcome.path == "escalated" else "Direct",
        count(len(steps), "step"), count(len(outcome.repairs), "repair"),
        count(llm_calls, "LLM call"), seconds_text(seconds),
    )


#: Why a run escalated, by signal kind (archs/checks.py and archs/verify.py).
SIGNAL_WORDS = {
    "error": "the first query failed",
    "schema": "the first query named a column that does not exist",
    "fanout": "the first query's join would have counted rows more than once",
    "empty": "the first query returned no rows",
    "sanity": "the first query's result looked wrong (all empty, or too many rows)",
    "reconcile": "the first query's totals did not add up",
    "percent": "a percentage in the first result was out of range",
    "date_format": "the first query compared dates written in different formats",
    "date_range": "the first query used other dates than the question",
    "forecast": "the question asks for a forecast or a fitted trend",
}
#: Signals raised when a query runs, so before the follow-up decision.
#: The verification kinds only run once a question has already escalated.
_RAISED_ON_RUN = frozenset({"error", "schema", "fanout", "empty", "sanity"})
_SOME_CHECK = "a check on the first result asked for more work"

#: What a tool did, as the end of "..., so it <phrase>."
ACTION_WORDS = {
    "run_sql": "ran a follow-up query",
    "search_values": "searched for similar names",
    "get_distinct_values": "looked up the values a column holds",
    "sample_rows": "looked at sample rows",
    "decompose_change": "split the change into its components",
    "contribution_by_dimension": "measured how much each group contributed",
    "linear_trend_forecast": "fitted a straight-line trend",
    "zscore_anomalies": "looked for unusual values",
    "pct_change": "computed percentage changes",
    "yoy_growth": "computed year-over-year growth",
    "compare_periods": "compared the two periods",
    "share_of_total": "computed each item's share of the total",
    "correlation": "measured a correlation",
}


def _join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _trigger(outcome: ArchOutcome) -> str | None:
    """Why the run escalated, or None when the follow-up decision's own
    words (outcome.plan) say it best."""
    first = outcome.steps[0] if outcome.steps else {}
    if first.get("escalation_signal"):  # read from the question, before any call
        return SIGNAL_WORDS.get(first["escalation_signal"], _SOME_CHECK)
    on_first = [r for r in outcome.repairs if r.step == 1]
    if on_first and on_first[0].signal in _RAISED_ON_RUN:
        return SIGNAL_WORDS[on_first[0].signal]
    if outcome.plan and outcome.plan[0].strip():
        return None
    if on_first:
        return SIGNAL_WORDS.get(on_first[0].signal, _SOME_CHECK)
    return _SOME_CHECK


def _actions(outcome: ArchOutcome) -> list[str]:
    """What the steps after the first one did, in order, without repeats."""
    done: list[str] = []
    for step in outcome.steps:
        if not isinstance(step.get("step"), int) or step["step"] < 2:
            continue
        for tool in step.get("tools", []):
            if tool == "repair":
                continue
            name = tool.removeprefix("compute:")
            phrase = ACTION_WORDS.get(name) or f"used {tool_label(tool).lower()}"
            if phrase not in done:
                done.append(phrase)
    if done:
        return done
    on_first = [r for r in outcome.repairs if r.step == 1]
    if any(r.accepted for r in on_first):
        return ["repaired the query"]
    return ["tried to repair the query"] if on_first else []


def escalation_reason(outcome: ArchOutcome) -> str:
    """The reason in plain words: 'The first query returned no rows, so it
    searched for similar names and ran a follow-up query.'"""
    if outcome.path != "escalated":
        return "One query was enough, so it answered directly."
    actions = _actions(outcome)
    trigger = _trigger(outcome)
    if trigger is None:
        decided = _sentence(outcome.plan[0])
        return f"{decided} It then {_join(actions)}." if actions else decided
    trigger = trigger[:1].upper() + trigger[1:]
    return f"{trigger}, so it {_join(actions)}." if actions else f"{trigger}."


# ---- Markdown ------------------------------------------------------------------
def escape_dollars(text: str) -> str:
    """Streamlit's markdown reads $...$ as maths, so an answer with two dollar
    amounts would lose the text between them to a formula. Escaped, each $
    shows as itself."""
    return text.replace("$", "\\$")
