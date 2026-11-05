"""State the reading of a question that ranks things without saying by what.

"Which segment is most valuable?" names no metric and no period, and different
readings give different winners. The answer instruction already asks the model
to say which reading it used, but a model can skip an instruction. On the
holdout it skipped it on ha17 and ha18 with a correct table, while following
it on ha16. So the sentence is built here, from the query that produced the
table the answer is written from, and it can only state what that query did:
the metric is the column the query ranks by, the window is its date literals.

Only C and D use this (router.Router._answer). A is the baseline, and its
answer stays the model's own words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot.errors import ParseError

from agentcrew.archs.checks import is_number
from agentcrew.archs.verify import question_periods, sql_date_spans
from agentcrew.schemas import ExecutionResult

#: Ranking words that do not say what is being ranked. "Most customers" does
#: not match: it names its measure. General English, not from a question set.
_VAGUE = re.compile(
    r"\b(?:(?:most|least)\s+(?:valuable|popular|important|successful|profitable"
    r"|loyal|engaged|active)|best(?![-\s]selling)|worst|strongest|weakest"
    r"|top(?!\s+of\b))\b",
    re.IGNORECASE,
)
#: "by month" and the like split a result; they do not measure anything.
_NAMED_METRIC = re.compile(
    r"\bby\s+(?!(?:month|quarter|year|week|day|region|segment|channel|category"
    r"|product|customer)s?\b)\w"
    r"|\bin\s+terms\s+of\b|\bbased\s+on\b|\bmeasured\s+(?:by|as|on)\b"
    r"|\baccording\s+to\b",
    re.IGNORECASE,
)
_NAMED_WINDOW = re.compile(
    r"\b(?:last|this|past|previous|current)\s+(?:\d+\s+)?(?:year|quarter|month|week"
    r"|day)s?\b|\byear[-\s]to[-\s]date\b|\bytd\b|\ball[-\s]time\b|\bever\b|\bsince\b"
    r"|\boverall\b",
    re.IGNORECASE,
)
#: Numeric columns that label a row rather than measure anything.
_LABEL_COLUMN = re.compile(r"(?:^|_)(?:id|key|code|year|month|quarter)$",
                           re.IGNORECASE)
_STATED = re.compile(r"\bassum", re.IGNORECASE)


@dataclass(frozen=True)
class OpenTerms:
    phrase: str
    metric: bool
    window: bool


def open_terms(question: str) -> OpenTerms | None:
    """What a ranking question leaves open, or None if it leaves nothing open."""
    found = _VAGUE.search(question)
    if found is None:
        return None
    metric = _NAMED_METRIC.search(question) is None
    window = not (question_periods(question) or _NAMED_WINDOW.search(question))
    if not (metric or window):
        return None
    return OpenTerms(" ".join(found.group(0).lower().split()), metric, window)


def _ranked_column(sql: str, candidates: list[str]) -> str | None:
    """The result column the query orders by, if it orders by one."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except ParseError:
        return None
    order = tree.args.get("order")
    for ordered in order.expressions if order else ():
        for select in tree.selects:
            if select.alias_or_name in candidates and (
                    select.unalias() == ordered.this
                    or ordered.this.name == select.alias_or_name):
                return select.alias_or_name
    return None


def _measure(result: ExecutionResult) -> str | None:
    numeric = [name for j, name in enumerate(result.columns)
               if any(is_number(row[j]) for row in result.rows)
               and not _LABEL_COLUMN.search(name)]
    if not numeric or not _is_query(result.sql):
        return None
    return _ranked_column(result.sql, numeric) or numeric[-1]


def _is_query(sql: str) -> bool:
    """False for a tool's result, whose provenance is only a comment."""
    try:
        return sqlglot.parse_one(sql, read="sqlite") is not None
    except ParseError:
        return False


def _window(sql: str) -> str:
    spans = sql_date_spans(sql)
    if not spans:
        return "across all dates in the data"
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    if start == end:
        return f"with a date filter at {start}"
    if (start.month, start.day, end.month, end.day) == (1, 1, 12, 31) \
            and start.year == end.year:
        return f"in {start.year}"
    return f"from {start} to {end}"


def assumption_sentence(question: str, result: ExecutionResult) -> str | None:
    """The sentence stating the reading `result` reflects, or None when the
    question leaves nothing open or the result has no measure to name."""
    terms = open_terms(question)
    if terms is None or not result.rows:
        return None
    if terms.metric:
        measure = _measure(result)
        if measure is None:
            return None
        sentence = (f'Assuming "{terms.phrase}" is measured by '
                    f'{measure.replace("_", " ")}')
        return (f"{sentence}, {_window(result.sql)}." if terms.window
                else f"{sentence}.")
    if not _is_query(result.sql):
        return None
    window = _window(result.sql)
    if window.startswith("across "):
        return "Assuming all dates in the data."
    return f"Assuming the period {window}."


def states_assumption(answer: str) -> bool:
    return _STATED.search(answer) is not None
