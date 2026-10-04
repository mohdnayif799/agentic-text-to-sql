"""Deterministic verification checks for architecture D (escalated path only).

Each check returns a Signal (kind, exact detail) or None. A failed check
triggers a disciplined repair, which is accepted only if the signal is gone.

  reconcile    a total reported with a count and an average of the same thing
               must satisfy total = count x average (e.g. revenue = orders x
               average order value) within 1%
  percent      columns named as percentages or shares must lie within 0..100
  date_format  a date column compared with a literal in another format, e.g.
               a 'YYYY-MM' column against '2025-07-01', which silently drops
               rows in SQLite's text comparison
  date_range   the query's date literals must overlap the periods the
               question names (only checked when the question names some)
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from agentcrew.archs.checks import Schema, Signal
from agentcrew.schemas import ExecutionResult

RECONCILE_REL_TOL = 0.01
RECONCILE_ABS_TOL = 0.5

_SPLIT = re.compile(r"[^a-z0-9]+")
_AVG_WORDS = {"avg", "average", "mean", "per", "aov"}
_VALUE_WORDS = {"value", "revenue", "sales", "spend", "amount", "aov"}
_TOTAL_WORDS = {"revenue", "sales", "spend", "amount", "total"}
_COUNT_WORDS = {"count", "n", "num", "number", "orders", "units", "quantity",
                "customers", "tickets", "items", "lines", "buyers"}
_NOISE = {"avg", "average", "mean", "per", "value", "revenue", "sales", "spend",
          "amount", "total", "count", "n", "num", "number", "completed", "of",
          "the", "a", "all"}
_PERCENT_WORDS = {"pct", "percent", "percentage", "share"}
_CHANGE_WORDS = {"change", "growth", "diff", "difference", "delta", "vs"}


def _tokens(name: str) -> set[str]:
    words = {w for w in _SPLIT.split(name.lower()) if w}
    stems = {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}
    if "aov" in words:
        stems |= {"order", "value", "avg"}
    return words | stems


def _numeric(rows: Sequence[Sequence[object]], j: int) -> bool:
    values = [r[j] for r in rows if r[j] is not None]
    return bool(values) and all(isinstance(v, int | float) and not isinstance(v, bool)
                                for v in values)


def reconcile_problem(result: ExecutionResult) -> str | None:
    cols, rows = result.columns, result.rows
    if not rows:
        return None
    numeric = [j for j in range(len(cols)) if _numeric(rows, j)]
    toks = {j: _tokens(cols[j]) for j in numeric}
    derived = _CHANGE_WORDS | _PERCENT_WORDS
    averages = [j for j in numeric if toks[j] & _AVG_WORDS and toks[j] & _VALUE_WORDS
                and not toks[j] & derived]
    totals = [j for j in numeric if toks[j] & _TOTAL_WORDS
              and not toks[j] & (_AVG_WORDS | derived)]
    counts = [j for j in numeric if toks[j] & _COUNT_WORDS
              and not toks[j] & (_AVG_WORDS | derived)]
    semantic = _NOISE | _COUNT_WORDS | _AVG_WORDS | _VALUE_WORDS | _TOTAL_WORDS
    for a in averages:
        for c in counts:
            subject = (toks[a] - _NOISE) & (toks[c] - _NOISE)
            if c == a or not subject:
                continue
            qualifier = toks[a] - semantic - subject
            if toks[c] - semantic - subject != qualifier:
                continue  # e.g. q2_orders with a q3 average
            for t in totals:
                if t in (a, c) or toks[t] - semantic - subject != qualifier:
                    continue
                for i, row in enumerate(rows):
                    total, count, average = row[t], row[c], row[a]
                    if None in (total, count, average):
                        continue
                    expected = count * average
                    if abs(total - expected) > max(RECONCILE_ABS_TOL,
                                                   RECONCILE_REL_TOL * abs(total)):
                        return (
                            f"Reconciliation failed: {cols[t]} should equal "
                            f"{cols[c]} x {cols[a]}, but in row {i + 1} "
                            f"{count} x {average} = {expected:,.2f}, not "
                            f"{total:,.2f}. Compute the average as the total "
                            "divided by the count, per the metric definitions."
                        )
    return None


def percent_problem(result: ExecutionResult) -> str | None:
    for j, name in enumerate(result.columns):
        words = _tokens(name)
        if not (words & _PERCENT_WORDS) or words & _CHANGE_WORDS:
            continue
        if not _numeric(result.rows, j):
            continue
        bad = [r[j] for r in result.rows
               if r[j] is not None and not -1e-9 <= r[j] <= 100 + 1e-9]
        if bad:
            return (f"Percentage out of range: column {name} has value {bad[0]}, "
                    "outside 0 to 100.")
    return None


def _literal_format(text: str) -> str | None:
    text = text.rstrip("%")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "YYYY-MM-DD"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return "YYYY-MM"
    return None


def date_format_problem(
    sql: str, schema: Schema, formats: Mapping[tuple[str, str], str]
) -> str | None:
    try:
        tree = qualify(sqlglot.parse_one(sql, read="sqlite"), schema=schema,
                       dialect="sqlite", validate_qualify_columns=False)
    except (ParseError, OptimizeError):
        return None
    tables = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}

    def column_format(node: exp.Expression) -> tuple[str, str] | None:
        if isinstance(node, exp.Column) and node.table in tables:
            key = (tables[node.table], node.name)
            if key in formats:
                return f"{key[0]}.{key[1]}", formats[key]
        return None

    comparisons = []
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        comparisons.append((node.left, [node.right]))
        comparisons.append((node.right, [node.left]))
    for node in tree.find_all(exp.Between):
        comparisons.append((node.this, [node.args.get("low"), node.args.get("high")]))
    for col, others in comparisons:
        found = column_format(col)
        if not found:
            continue
        name, fmt = found
        for other in others:
            if isinstance(other, exp.Literal) and other.is_string:
                lit_fmt = _literal_format(other.this)
                if lit_fmt and lit_fmt != fmt:
                    return (f"Date format mismatch: {name} holds {fmt} text but "
                            f"is compared with '{other.this}' ({lit_fmt}); text "
                            f"comparison then drops or keeps the wrong rows. "
                            f"Compare it with {fmt} values.")
    return None


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
     "dec"), start=1)}


def _month_span(year: int, month: int) -> tuple[date, date]:
    end = date(year + (month == 12), month % 12 + 1, 1)
    return date(year, month, 1), date.fromordinal(end.toordinal() - 1)


def question_periods(question: str) -> list[tuple[date, date]]:
    text = question.lower()
    spans = []
    for q, y in re.findall(r"\bq([1-4])\s*(20\d\d)\b", text):
        start = date(int(y), 3 * int(q) - 2, 1)
        spans.append((start, _month_span(int(y), 3 * int(q))[1]))
    for m, y in re.findall(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                           r"[a-z]*\.?\s+(20\d\d)\b", text):
        spans.append(_month_span(int(y), _MONTHS[m]))
    for y in re.findall(r"\b(20\d\d)\b", text):
        spans.append((date(int(y), 1, 1), date(int(y), 12, 31)))
    return spans


def _literal_span(text: str) -> tuple[date, date] | None:
    text = text.rstrip("%")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = date.fromisoformat(text)
            return day, day
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return _month_span(int(text[:4]), int(text[5:]))
        if re.fullmatch(r"20\d\d", text):
            return date(int(text), 1, 1), date(int(text), 12, 31)
    except ValueError:
        return None
    return None


def date_range_problem(sql: str, question: str) -> str | None:
    periods = question_periods(question)
    if not periods:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except ParseError:
        return None
    spans = [s for lit in tree.find_all(exp.Literal) if lit.is_string
             for s in [_literal_span(lit.this)] if s]
    if not spans:
        return None
    overlaps = any(a0 <= b1 and b0 <= a1 for a0, a1 in spans for b0, b1 in periods)
    if overlaps:
        return None
    asked = ", ".join(f"{a}..{b}" for a, b in periods)
    used = ", ".join(sorted({f"{a}..{b}" for a, b in spans}))
    return (f"Date range mismatch: the question is about {asked}, but the query "
            f"only filters on {used}.")


def verify(
    sql: str,
    result: ExecutionResult,
    question: str,
    schema: Schema,
    formats: Mapping[tuple[str, str], str],
) -> Signal | None:
    """First failed verification check for an executed query, or None."""
    if not result.ok or not result.rows:
        return None
    for kind, problem in (
        ("reconcile", reconcile_problem(result)),
        ("percent", percent_problem(result)),
        ("date_format", date_format_problem(sql, schema, formats)),
        ("date_range", date_range_problem(sql, question)),
    ):
        if problem:
            return Signal(kind, problem)
    return None
