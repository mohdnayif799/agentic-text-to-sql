"""Architecture D: full (C plus exploration, compute tools and verification).

Up to the follow-up decision D is exactly C:
- step 0 is A's first call (`step0_llm`, A's response cache in evaluation);
- C's deterministic signals and disciplined repair (`shared_llm`, C's cache);
- C's follow-up decision (`shared_llm`), or C's question signal (a forecast
  or trend fit, or a condition on the answer), which escalates without that
  call.
With no signal and no follow-up, D answers directly with A's answer call, so
the direct path is exactly A's calls (router.py describes the one sentence code
may add after them).

When the run escalates, D adds:
- verification checks (archs/verify.py) on every query of the escalated
  path, step 0 included; a failed check triggers a disciplined repair;
- for a question that asks why (is_why_question), a metric analysis first:
  one validated MetricPlan picks the metric and the comparison, and
  decompose_change splits the metric into its components (or, for a metric
  without components, contribution_by_dimension breaks it down). The plan may
  give two periods together with groups, and decompose_change then compares each
  component's change across the groups, so the answer names what differs, not
  what fell most. Choosing that comparison is the planner's call; no wording of
  the question decides it. For a breakdown, the dimension that concentrates the
  change most comes first (compute.concentration, no LLM call), and the planner's
  own dimension stays as the second analysis. If it asks
  about a sudden change (is_spike_question), the next step reads the same metric
  over the periods before it (compute.history.period_history, no LLM call), so the
  answer can say what is normal. Added after a holdout failure (ha08: an answer
  compared Q3 with Q2 only, so nothing showed that Q3 was unusual);
- for a question that asks for a forecast or a fitted trend (C's forecast
  signal), a trend fit first: one validated TrendPlan gives the series query
  (run and verified like any query) and the fit, which linear_trend_forecast
  or zscore_anomalies (against the trend) computes. A first query that fitted
  the line inside SQL is not trusted: added after a development failure
  (dev2 dv03: the planner accepted A's one-query fit, wrong by an x offset);
- for a question whose answer depends on a condition, the condition first: one
  validated ConditionPlan gives a query that evaluates only the condition, and
  then, with its values in view, one validated BranchPlan gives the query for
  the branch that applies. Added after holdout failures (ha19 named the wrong
  branch, ha20 kept a hollow row from the other branch);
- an exploration loop: decide_next_action returns one validated NextAction
  (archs/explore.py) given the full shared context, the question verbatim,
  why more work is needed and capped previews of earlier steps. Tools:
  get_distinct_values, search_values, sample_rows, run_sql, compute, answer.

A why-question's final table is its metric analyses stacked (they share one
schema), and the answer step is told how to read it and to state every
component's comparison, including components that are about the same. If the
answer does not name the main driver (the largest contributor) in plain
words, a sentence built from the table states it (driver_sentence).
A trend-fit question's final table is the fit's output. A conditional
question's final table is the branch's result alone; the condition's values go
to the answer step beside it, so the answer can name the branch taken without
them changing the table. Other questions are assembled as in C.

`force_escalation` is a development-only switch (the evaluation runner
refuses it on holdout sets; the app never sets it): D escalates on every
question, so a smoke test can exercise tools a direct answer never reaches.
Its results do not count.

Budgets are the live demo's: at most `max_steps` steps (step 0 counts as
one; every tool call is one step) and `max_attempts_per_step` executions per
query. The answer is written from the final result.
"""

from __future__ import annotations

import re

from agentcrew.archs import progress
from agentcrew.archs.assemble import stack
from agentcrew.archs.baseline import SQL_SYSTEM, SQL_USER
from agentcrew.archs.checks import WRONG_FIGURES
from agentcrew.archs.explore import (
    MAX_TIED_MATCHES,
    METRIC_TOOLS,
    TREND_FUNCTIONS,
    BranchPlan,
    ConditionPlan,
    MetricPlan,
    NextAction,
    TrendPlan,
    compute,
    history_result,
    lookup,
)
from agentcrew.archs.outcome import ArchOutcome, SqlAttempt
from agentcrew.archs.router import (
    PREVIEW_ROWS,
    FollowupDecision,
    Router,
    StepRun,
    WriteAttempt,
)
from agentcrew.archs.verify import verify
from agentcrew.compute.concentration import concentrating_dimension
from agentcrew.compute.history import MAX_HISTORY_ROWS, has_dimension
from agentcrew.compute.metrics import GROUP_GAP_PREFIX, SIMILAR_PCT, catalog
from agentcrew.config import Settings
from agentcrew.context import SharedContext
from agentcrew.context.values import profile_columns
from agentcrew.llm import LLMClient, StructuredOutputError
from agentcrew.schemas import ExecutionResult, SqlDraft
from agentcrew.tools import ToolContext, execute_readonly_sql

DECIDE_SYSTEM = """\
You plan the next step of an analysis of a read-only SQLite database. Choose \
exactly ONE action and return it as JSON.

Actions:
- get_distinct_values: table, column. Lists a column's real values.
- search_values: term (optional table, column). Fuzzy-matches a word from \
the question to real values stored in the database.
- sample_rows: table. Shows a few rows.
- run_sql: sql. One read-only SQLite SELECT (or WITH ... SELECT); only the \
tables and columns in the schema, text values exactly as listed, metric \
definitions from the data dictionary.
- compute: function, params. Deterministic calculations, no code:
  * on an earlier step's table (params.step = that step's number):
    pct_change(value, order?), yoy_growth(period, value),
    compare_periods(period, value, period_a, period_b, by?),
    share_of_total(value, label),
    zscore_anomalies(value, label, threshold?, direction?, against?) where \
direction is both|above|below and against is mean|trend,
    linear_trend_forecast(value, order, periods_ahead?),
    correlation(a, b);
  * metric analyses whose SQL is built from the metric definitions:
    decompose_change(metric, comparison) splits a metric's difference \
between two periods, or between groups, into its components (for example orders x \
average order value, or units x average price x (1 - average discount)) and \
reports each component's change;
    contribution_by_dimension(metric, dimension, comparison) shows how much \
each value of a dimension contributes to the change and names the largest.
    comparison is {"period_a": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, \
"period_b": {...}} (period_b is the one being explained), or \
{"group_dimension": ..., "groups": [the group being explained, then each \
group it is compared with], "period": {...}}, or, with decompose_change, to \
compare how a change differs between groups, {"period_a": {...}, "period_b": \
{...}, "group_dimension": ..., "groups": [...]}, with optional "filters": \
{dimension: value}.
  Metrics and their dimensions:
{catalog}
- answer: stop, because the steps so far contain everything needed.

Method: for a "why" or difference question, split the metric into its \
components and check which values of the relevant dimension contribute most \
before answering. Every tool call uses one step; stop with answer when the \
results suffice.
"""

DECIDE_USER = """\
{context}

## Question
{question}

## Why more work is needed
{reason}

## Steps so far
{history}

## Steps left
{remaining}
"""


METRIC_PLAN_SYSTEM = """\
You start the analysis of a question that asks why. A first query has been \
run. Choose the metric analysis that explains the difference the question \
asks about, and return its arguments as JSON:
- metric: the metric the question is about, one of the metrics listed below.
- comparison: what is compared, either
  * two periods: {"period_a": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, \
"period_b": {...}}, where period_b is the period being explained and \
period_a the period it is compared with; or
  * groups of one dimension: {"group_dimension": ..., "groups": [the group \
being explained, then each group it is compared with], "period": {...}}; the \
period is optional; or
  * how a change differs between groups, when the question asks why one group \
changed more or less than others: {"period_a": {...}, "period_b": {...}, \
"group_dimension": ..., "groups": [the group being explained, then every group \
it is compared with]}.
  Optional "filters": {dimension: value} restrict both sides. Group and \
filter values are real values, exactly as listed under allowed values.
- dimension: only for a metric listed without decompose_change: the dimension \
whose values may explain the difference.
- reason: one sentence.

The analysis splits the metric into its components (for example orders x \
average order value, or units x average price x (1 - average discount)) and \
reports how each component compares between the sides.

Metrics and their dimensions:
{catalog}
"""

TREND_PLAN_SYSTEM = """\
You start the analysis of a question that asks for a forecast, a projection \
or a fitted trend line. The fit is done by a deterministic tool, not in SQL. \
Return its inputs as JSON:
- series_sql: ONE read-only SQLite query that returns the series to fit: one \
row per period over exactly the fitting window the question gives (all of it, \
nothing after it), with a period label column and a numeric value column, \
ordered by period. Label months 'YYYY-MM', quarters 'YYYY-Qn', years 'YYYY'. \
Compute no trend, slope or forecast in SQL. Use only the tables and columns in \
the schema, text values exactly as listed under allowed values, and the metric \
definitions from the data dictionary.
- period and value: the names of those two result columns.
- function: linear_trend_forecast to forecast periods after the series, or \
zscore_anomalies to find periods far from the trend line.
- periods: for linear_trend_forecast, the labels of the periods the question \
asks to forecast (["YYYY-MM", ...]); or periods_ahead, how many periods after \
the last one.
- threshold and direction (both, above or below): for zscore_anomalies.
- reason: one sentence.

The tool fits a least-squares line with x = 1, 2, 3, ... for consecutive \
periods (1 = the first period of the series).
"""

CONDITION_PLAN_SYSTEM = """\
You start the analysis of a question whose answer depends on a condition (for \
example "if the gap is more than X, show ...; otherwise ..."). Evaluate the \
condition first, on its own, and return its query as JSON:
- condition_sql: ONE read-only SQLite query returning ONE row with the values \
the condition compares (one clearly named column per value), the difference or \
ratio the condition tests, and a column condition_met that is 1 if the \
condition holds and 0 if it does not. If a branch depends on which item is \
higher or lower, also return that item's name in a column. Compute nothing that \
only a branch asks for. Use only the tables and columns in the schema, text \
values exactly as listed under allowed values, and the metric definitions from \
the data dictionary.
- reason: one sentence.
"""

BRANCH_PLAN_SYSTEM = """\
The condition of a question has been evaluated: its result is the last step \
below. Return the query for the branch that applies as JSON:
- condition_met: true if that result says the condition holds, otherwise false.
- branch: one sentence saying which branch applies and why, naming the group it \
is about.
- sql: ONE read-only SQLite query that returns exactly what that branch asks \
for: no row and no column for any other branch, and none of the condition's \
values. Use only the tables and columns in the schema, text values exactly as \
listed under allowed values, and the metric definitions from the data dictionary.
- reason: one sentence.
"""

#: Appended to the answer prompt when the final table is a conditional
#: question's branch. The values stay out of the table, which holds what the
#: branch asks for, and are given here so the answer can state them.
CONDITION_GUIDANCE = """\
## The condition, evaluated first
{branch}
{values}
The result above is the branch that applies. Open the answer with the condition: \
the compared values from this table (they count as part of the result) and \
which branch they select, naming the group. Then answer from the result.
"""

#: Appended to the answer prompt when the final table is D's stacked metric
#: analyses. It explains the tool's columns and asks for every component's
#: comparison: a why-answer that names only the component that moved leaves
#: out the evidence that the others did not.
METRIC_GUIDANCE = f"""\
## How to read this result
Each row compares one item between side_a (the reference) and side_b (what \
the question asks about). Rows of one decomposition are components that \
multiply (or add) to the metric; rows "<metric> by <dimension>" break the \
metric down by that dimension. share_of_change_pct is the item's share of \
the metric's difference; comparison says whether side_b is higher, lower or \
about the same as side_a (within {SIMILAR_PCT:g}%); largest_contributor marks \
the item that explains most of the difference.
Write the answer (up to six sentences) in plain words:
- First sentence: the main driver, which is the largest contributor, named \
as in the item column, with how side_b compares with side_a and the figures.
- Then, for EVERY other component, how side_b compares with side_a, with its \
figures, including the components that are about the same (say that they \
are about the same, so they do not explain the difference).
- Never write column names (such as value_a or largest_contributor); round \
figures (whole numbers for counts, at most two decimals otherwise).
"""

#: Replaces METRIC_GUIDANCE when the table compares a change across groups: the
#: component that fell most in a group is not the answer if it fell as much in
#: every other group.
ACROSS_GROUPS_GUIDANCE = f"""\
## How to read this result
The question asks why one group changed more or less than others. Rows whose \
analysis starts with "{GROUP_GAP_PREFIX.strip()}" compare the percent change of \
each component: value_b is the group being explained (side_b), value_a is the \
average of the other groups, and change is the difference in percentage points. \
share_of_change_pct is the component's share of the extra change, and \
largest_contributor marks the component that explains it. comparison is "about \
the same" when the difference is within {SIMILAR_PCT:g} points.
Rows whose analysis starts with a group's name give that group's own figures in \
the two periods (value_a, value_b) and their pct_change.
Write the answer (up to six sentences) in plain words:
- First sentence: the component marked largest_contributor, named as in the item \
column, with its figures and pct_change in the group being explained.
- Then how much the same component changed in each other group (their pct_change).
- Then, for each other component, whether it changed by a similar amount in every \
group (give the range of pct_change). Say that a component which changed similarly \
in every group does not explain the difference, however large its own change.
- Never write column names (such as value_a or largest_contributor); round \
figures (whole numbers for counts, at most two decimals otherwise).
"""

#: Added to METRIC_GUIDANCE when the concentration check put another breakdown first.
CONCENTRATION_GUIDANCE = """\
Several breakdowns are shown, and the first breakdown concentrates the change most, \
so its largest contributor is the main driver.
"""

#: Added to METRIC_GUIDANCE when the question asks about a sudden change.
BASELINE_GUIDANCE = """\
## What is normal
The same metric in the period the question asks about (the last column) and the \
periods before it. earlier_low and earlier_high are the lowest and highest of \
those earlier periods; a total row, if there is one, covers all values. These \
figures count as part of the result.
{table}
Also say, with these figures: what was normal before for the main driver (its \
range in the earlier periods), how much of the total in this period it accounts \
for if there is a total row, and whether the other values moved comparably.
"""

#: Questions that ask for the cause of something. General English phrasing,
#: not drawn from any question set.
_WHY = re.compile(
    r"\b(why|how\s+come|what\s+(explains|explained|drove|drives|caused|causes"
    r"|led\s+to|is\s+behind|was\s+behind)|reasons?\s+(for|behind))\b",
    re.IGNORECASE,
)
#: Words for a sudden change in a metric. General English phrasing, not drawn
#: from any question set.
_SPIKE = re.compile(
    r"\b(spik\w*|surg\w*|jump\w*|soar\w*|sky-?rocket\w*|dip\w*|drop\w*|plung\w*"
    r"|slump\w*|tank\w*|fell|fall\w*|declin\w*|collaps\w*|crash\w*)\b",
    re.IGNORECASE,
)
_METRIC_STEPS = {f"compute:{name}" for name in METRIC_TOOLS}
_TREND_STEPS = {f"compute:{name}" for name in TREND_FUNCTIONS}

#: Replaces the metric catalog in the planning prompts on a database other than
#: NorthStar, where decompose_change and contribution_by_dimension have no
#: definitions to build their SQL from.
NO_METRICS = ("(none: no metric analyses are defined for this database, so do "
              "not use decompose_change or contribution_by_dimension)")

FORCED_REASON = (
    "Forced escalation for a development smoke test: re-check the first result "
    "with the tools (look up the values it filters on, recompute any statistic "
    "with the compute functions, run any follow-up query the question implies) "
    "before answering."
)


def is_why_question(question: str) -> bool:
    return _WHY.search(question) is not None


def is_spike_question(question: str) -> bool:
    return _SPIKE.search(question) is not None


def _figure(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _drivers(result: ExecutionResult) -> list[tuple[tuple[str, str], list]]:
    """Per compared pair, the largest-contributor row of its first analysis."""
    col = {c: i for i, c in enumerate(result.columns)}
    first: dict[tuple[str, str], str] = {}
    found = []
    for row in result.rows:
        pair = (row[col["side_a"]], row[col["side_b"]])
        analysis = row[col["analysis"]]
        if analysis == "metric":
            continue
        is_first = first.setdefault(pair, analysis) == analysis
        if is_first and row[col["largest_contributor"]]:
            found.append((pair, row))
    return found


def driver_sentence(result: ExecutionResult) -> str | None:
    """The main driver in plain words, built from a stacked metric table.

    For each compared pair: the largest contributor of the first analysis
    (the first decomposition, or the breakdown if there is none), with both
    sides' figures and the percentage difference.
    """
    col = {c: i for i, c in enumerate(result.columns)}
    parts = []
    for (side_a, side_b), row in _drivers(result):
        analysis, item = row[col["analysis"]], row[col["item"]]
        name = f"{item} ({analysis})" if " by " in analysis else str(item)
        pct = row[col["pct_change"]]
        parts.append(
            f"{name}, {side_b} {_figure(row[col['value_b']])} vs {side_a} "
            f"{_figure(row[col['value_a']])}"
            + (f" ({pct:+.1f}%)" if pct is not None else ""))
    return ("Main driver (largest contributor): " + "; ".join(parts) + ".") \
        if parts else None


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[_\-]+", " ", text.lower()).split())


def names_driver(answer: str, result: ExecutionResult) -> bool:
    """True if the answer names every main driver (case, '-' and '_' ignored)."""
    item = result.columns.index("item")
    return all(_plain(str(row[item])) in _plain(answer)
               for _, row in _drivers(result))


_FORMATS: dict[str, dict[tuple[str, str], str]] = {}


def _date_formats(tools: ToolContext) -> dict[tuple[str, str], str]:
    url = str(tools.engine.url)
    if url not in _FORMATS:
        profile = profile_columns(tools.engine, tools.catalog)
        _FORMATS[url] = {(d.table, d.column): d.fmt for d in profile.dates}
    return _FORMATS[url]


class Full(Router):
    def __init__(
        self,
        question: str,
        *,
        llm: LLMClient,
        tools: ToolContext,
        context: SharedContext,
        settings: Settings,
        step0_llm: LLMClient | None = None,
        shared_llm: LLMClient | None = None,
        force_escalation: bool = False,
    ) -> None:
        super().__init__(question, llm=llm, tools=tools, context=context,
                         settings=settings, step0_llm=step0_llm)
        self.shared_llm = shared_llm or llm
        self.force_escalation = force_escalation
        self.formats = _date_formats(tools)
        # The metric analyses are built from NorthStar's data dictionary
        # (compute/metrics.py). On any other database (tools.metrics False) a
        # why-question goes through the general exploration loop instead, and
        # the planning prompt says the metric tools do not apply.
        self.why = tools.metrics and is_why_question(question)
        kind = self.question_signal.kind if self.question_signal else None
        self.forecast = kind == "forecast"
        self.conditional = kind == "conditional"
        self.condition_taken: tuple[StepRun, StepRun, str] | None = None
        """(condition step, branch step, which branch), once a branch ran."""
        self.metric_plan: MetricPlan | None = None
        self.concentrated = False
        """The concentration check put another breakdown before the planner's."""
        self.history: StepRun | None = None
        """The earlier periods of a spike or dip question, once they were read."""
        # replace(), not format(): the prompts contain literal JSON braces.
        metrics = catalog() if tools.metrics else NO_METRICS
        self.system = DECIDE_SYSTEM.replace("{catalog}", metrics)
        self.metric_system = METRIC_PLAN_SYSTEM.replace("{catalog}", metrics)
        self.trend_system = TREND_PLAN_SYSTEM

    def _verify(self, sql: str, result: ExecutionResult):
        return verify(sql, result, self.question, self.schema, self.formats,
                      forecast=self.forecast)

    def _explore_history(self, steps: list[StepRun]) -> str:
        # A lookup's rows are all shown: a preview must not hide the value it found.
        return "\n\n".join(
            f"Step {s.number} [{s.tool}]"
            + (f" - {s.reason}" if s.reason else "")
            + f"\n{s.sql}\nResult:\n"
            + s.result.preview(limit=MAX_TIED_MATCHES if s.kind == "lookup"
                               else PREVIEW_ROWS)
            for s in steps
        )

    def _decide_prompt(self, steps: list[StepRun], reason: str) -> str:
        return DECIDE_USER.format(
            context=self.context_text, question=self.question,
            reason=reason or "(not given)", history=self._explore_history(steps),
            remaining=self.settings.max_steps - len(steps),
        )

    def _next_action(self, steps: list[StepRun], reason: str) -> NextAction | None:
        progress.enter(f"step {len(steps) + 1}: decide_next_action")
        try:
            return self._structured(NextAction, self.system,
                                    self._decide_prompt(steps, reason))
        except StructuredOutputError:
            return None

    def _metric_step(self, number: int, steps: list[StepRun],
                     reason: str) -> StepRun | None:
        """A why-question's first exploration step: a metric analysis. If no
        valid plan comes back, the ordinary loop continues instead."""
        progress.enter(f"step {number}: metric plan")
        try:
            plan: MetricPlan = self._structured(
                MetricPlan, self.metric_system, self._decide_prompt(steps, reason))
        except StructuredOutputError:
            return None
        self.metric_plan, action = self._concentrated(plan)
        return self._act(number, action, steps)

    def _concentrated(self, plan: MetricPlan) -> tuple[MetricPlan, NextAction]:
        """A breakdown leads with the dimension that concentrates the change most.
        The planner's dimension stays as the second analysis, so a question that
        asks about it still gets it. Other plans are left as they are."""
        action = plan.to_action()
        if action.function != "contribution_by_dimension":
            return plan, action
        best = concentrating_dimension(
            lambda sql: execute_readonly_sql(self.tools, sql), plan.metric,
            plan.comparison, preferred=plan.dimension)
        if best is None or best.dimension == plan.dimension:
            return plan, action
        self.concentrated = True
        chosen = plan.model_copy(update={"dimension": best.dimension, "reason": (
            f"{plan.reason} Broken down first by {best.dimension}: one value "
            f"({best.item}) holds the largest share of the change "
            f"({best.share_pct:.0f}%).").strip()})
        action = chosen.to_action()
        return chosen, action.model_copy(update={
            "params": {**action.params, "also_dimension": plan.dimension}})

    def _history_step(self, number: int) -> StepRun | None:
        """The metric over the periods before the one the question asks about.
        Evidence for the answer, not a table to grade: its kind keeps it out of
        the final table. None if the plan names no period or there is no history."""
        plan = self.metric_plan
        period = plan.comparison.explained_period
        if period is None:
            return None
        progress.enter(f"step {number}: compute:period_history")
        dimension = (plan.dimension if has_dimension(plan.metric, plan.dimension)
                     else None)
        result = history_result(plan.metric, period, dimension, plan.comparison.filters,
                                lambda sql: execute_readonly_sql(self.tools, sql))
        if not result.ok:
            return None
        step = StepRun(number, result.sql, result, None,
                       "What was normal before this period.", result.sql,
                       kind="lookup", tool="compute:period_history")
        step.attempts.append(SqlAttempt.from_result(result, result.sql))
        self.history = step
        return step

    def _trend_steps(self, number: int, steps: list[StepRun],
                     reason: str) -> list[StepRun]:
        """A trend-fit question's first exploration steps: the series query,
        then the fit on it. Without a valid plan, or if the series query
        fails, the ordinary loop continues instead."""
        progress.enter(f"step {number}: trend plan")
        try:
            plan: TrendPlan = self._structured(
                TrendPlan, self.trend_system, self._decide_prompt(steps, reason))
        except StructuredOutputError:
            return []
        series = self.run_step(number, plan.series_sql, plan.reason,
                               verify=self._verify)
        if not (series.result.ok and series.result.rows) or self._figures_wrong(series):
            return [series]
        return [series, self._act(number + 1, plan.compute_action(number),
                                  [*steps, series])]

    def _branch_steps(self, number: int, steps: list[StepRun],
                      reason: str) -> list[StepRun]:
        """A conditional question's first exploration steps: the condition's
        query, then the query of the branch it selects. Without a valid plan,
        or if a query fails, the ordinary loop continues instead."""
        progress.enter(f"step {number}: condition plan")
        try:
            plan: ConditionPlan = self._structured(
                ConditionPlan, CONDITION_PLAN_SYSTEM,
                self._decide_prompt(steps, reason))
        except StructuredOutputError:
            return []
        condition = self.run_step(number, plan.condition_sql, plan.reason,
                                  verify=self._verify)
        if not (condition.result.ok and condition.result.rows):
            return [condition]
        progress.enter(f"step {number + 1}: branch plan")
        ran = [*steps, condition]
        try:
            chosen: BranchPlan = self._structured(
                BranchPlan, BRANCH_PLAN_SYSTEM, self._decide_prompt(ran, reason))
        except StructuredOutputError:
            return [condition]
        branch = self.run_step(number + 1, chosen.sql, chosen.branch,
                               verify=self._verify)
        if branch.result.ok and branch.result.rows:
            self.condition_taken = (condition, branch, chosen.branch)
        return [condition, branch]

    @staticmethod
    def _figures_wrong(step: StepRun) -> bool:
        """A repair could not clear a signal that means the figures are wrong, so
        nothing is computed from this step."""
        return step.signal is not None and step.signal.kind in WRONG_FIGURES

    def _guidance(self, selected: ExecutionResult, stacked: bool) -> str:
        if stacked and GROUP_GAP_PREFIX in " ".join(
                str(row[selected.columns.index("analysis")]) for row in selected.rows):
            return ACROSS_GROUPS_GUIDANCE
        if stacked:
            guidance = METRIC_GUIDANCE
            if self.concentrated:
                guidance += CONCENTRATION_GUIDANCE
            if self.history:
                table = self.history.result.preview(limit=MAX_HISTORY_ROWS + 1)
                guidance += BASELINE_GUIDANCE.format(table=table)
            return guidance
        if self.condition_taken and selected is self.condition_taken[1].result:
            condition, _, branch = self.condition_taken
            return CONDITION_GUIDANCE.format(
                branch=branch, values=condition.result.preview(limit=PREVIEW_ROWS))
        return ""

    def _select(self, steps: list[StepRun]):
        """A trend-fit question's final table is the fit; a conditional
        question's is the branch; a why-question's is its metric analyses,
        stacked; any other question (or no successful analysis) is assembled
        as in C."""
        if self.condition_taken:
            branch = self.condition_taken[1]
            return branch.result, branch.number, None
        if self.forecast:
            fits = [s for s in steps if s.tool in _TREND_STEPS
                    and s.result.ok and s.result.rows]
            if fits:
                return fits[-1].result, fits[-1].number, None
        if self.why:
            metric = [s for s in steps if s.tool in _METRIC_STEPS
                      and s.result.ok and s.result.rows]
            if metric:
                numbers = [s.number for s in metric]
                return (stack([s.result for s in metric], numbers), numbers[0],
                        {"stacked_steps": numbers})
        return super()._select(steps)

    def _act(self, number: int, action: NextAction,
             steps: list[StepRun]) -> StepRun:
        tool = (f"compute:{action.function}" if action.action == "compute"
                else action.action)
        progress.enter(f"step {number}: {tool}")
        if action.action == "run_sql":
            step = self.run_step(number, action.sql, action.reason,
                                 verify=self._verify)
            return step
        if (action.action == "compute" and action.function in METRIC_TOOLS
                and not self.tools.metrics):
            result = ExecutionResult(
                ok=False, sql=f"-- compute: {action.function}",
                error_message=f"{action.function} is not available for this "
                              "database; use run_sql or another compute function")
            step = StepRun(number, result.sql, result, None, action.reason,
                           result.sql, kind="compute",
                           tool=f"compute:{action.function}")
        elif action.action == "compute":
            result, _ = compute(
                action, {s.number: s.result for s in steps
                         if not self._figures_wrong(s)},
                lambda sql: execute_readonly_sql(self.tools, sql),
            )
            step = StepRun(number, result.sql, result, None, action.reason,
                           result.sql, kind="compute",
                           tool=f"compute:{action.function}")
        else:
            result = lookup(action, self.tools)
            step = StepRun(number, result.sql, result, None, action.reason,
                           result.sql, kind="lookup", tool=action.action)
        step.attempts.append(SqlAttempt.from_result(result, result.sql))
        return step

    def run(self) -> ArchOutcome:
        decision = FollowupDecision()
        steps: list[StepRun] = []
        try:
            # Step 0 = A's first call; repairs and decision = C's own calls.
            progress.enter("step 1: SQL (A's call)")
            draft: SqlDraft = self._structured(
                SqlDraft, SQL_SYSTEM,
                SQL_USER.format(context=self.context_text, question=self.question,
                                retry_block=""),
                llm=self.step0_llm,
            )
            first = self.run_step(1, draft.sql, repair_llm=self.shared_llm,
                                  from_a=True)
            steps.append(first)
            decision = self._followup_decision(first, llm=self.shared_llm)
            escalated = self.escalated(decision, first) or self.force_escalation
            explore = (decision.needs_followup or first.signal is not None
                       or self.force_escalation)
            if escalated and first.result.ok and first.signal is None:
                progress.enter("step 1: verification")
                first.signal = self._verify(first.sql, first.result)
                self.repair(first, verify=self._verify)
                # A failed check that repair could not resolve: explore.
                explore = explore or first.signal is not None
            if explore:
                # The decision's reason only when it asked for a follow-up.
                reason = (decision.reason if decision.needs_followup else "") or (
                    first.signal.detail if first.signal else "")
                if self.force_escalation and not reason:
                    reason = FORCED_REASON
                if self.why and len(steps) < self.settings.max_steps:
                    opened = self._metric_step(len(steps) + 1, steps, reason)
                    if opened is not None:
                        steps.append(opened)
                        if (is_spike_question(self.question)
                                and not self.metric_plan.comparison.across_groups
                                and len(steps) < self.settings.max_steps
                                and opened.result.ok and opened.result.rows):
                            history = self._history_step(len(steps) + 1)
                            steps += [history] if history else []
                elif self.forecast and len(steps) + 2 <= self.settings.max_steps:
                    steps += self._trend_steps(len(steps) + 1, steps, reason)
                elif self.conditional and len(steps) + 2 <= self.settings.max_steps:
                    steps += self._branch_steps(len(steps) + 1, steps, reason)
                while len(steps) < self.settings.max_steps:
                    action = self._next_action(steps, reason)
                    if action is None or action.action == "answer":
                        break
                    steps.append(self._act(len(steps) + 1, action, steps))
            selected, selected_step, plan_used = self._select(steps)
            if selected is None:
                failed = self._failed(steps, decision, "every query failed")
                return self._as_full(failed)
            untracked = self._untracked(selected, steps)
            if untracked:
                return self._as_full(self._untracked_outcome(
                    selected, steps, escalated, untracked))
            stacked = bool(plan_used and "stacked_steps" in plan_used)
            final, selected, selected_step, fell_back = self._answer(
                selected, selected_step, steps, escalated,
                guidance=self._guidance(selected, stacked))
            if fell_back:
                stacked = False
                plan_used = {"fallback": "the answer call failed on the final "
                                         "table; answered from step 1"}
            answer, added = final.answer, False
            sentence = driver_sentence(selected) if stacked else None
            if sentence and not names_driver(answer, selected):
                answer, added = f"{sentence} {answer}", True
        except WriteAttempt as write:
            return self._as_full(self._refused(write, bool(steps)))
        except StructuredOutputError as exc:
            return ArchOutcome(
                arch="full", question=self.question, status="failed",
                answer=f"Could not complete the analysis: {exc}", selected=None,
                path="escalated" if steps else "direct", stop_reason="error",
                error=str(exc),
                attempts=tuple(a for s in steps for a in s.attempts),
                repairs=tuple(r for s in steps for r in s.repairs),
            )

        records = [
            {"step": s.number,
             "tools": [s.tool] + (["repair"] if s.repairs else []),
             "sql": s.sql, "reason": s.reason,
             "signal": s.repairs[0].signal if s.repairs else None,
             **self._signal_record(s.number)}
            for s in steps
        ]
        if plan_used:
            tool = ("fallback" if "fallback" in plan_used
                    else "stack" if stacked else "assemble")
            records.append({"step": "final",
                            "tools": [tool],
                            "plan": plan_used,
                            **({"driver_sentence_added": added} if stacked else {})})
        return ArchOutcome(
            arch="full", question=self.question, status="done",
            answer=answer, selected=selected,
            path="escalated" if escalated else "direct",
            plan=tuple([decision.reason] if decision.needs_followup
                       else [FORCED_REASON] if self.force_escalation else []),
            attempts=tuple(a for s in steps for a in s.attempts),
            steps=tuple(records),
            key_numbers=tuple(final.key_numbers), caveats=tuple(final.caveats),
            repairs=tuple(r for s in steps for r in s.repairs),
            selected_step=selected_step,
        )

    @staticmethod
    def _as_full(outcome: ArchOutcome) -> ArchOutcome:
        from dataclasses import replace

        return replace(outcome, arch="full")


def run_full(
    question: str,
    *,
    llm: LLMClient,
    tools: ToolContext,
    context: SharedContext,
    settings: Settings,
    step0_llm: LLMClient | None = None,
    shared_llm: LLMClient | None = None,
    force_escalation: bool = False,
) -> ArchOutcome:
    return Full(question, llm=llm, tools=tools, context=context, settings=settings,
                step0_llm=step0_llm, shared_llm=shared_llm,
                force_escalation=force_escalation).run()
