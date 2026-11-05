"""Which dimension concentrates a change most (architecture D).

Before breaking a metric down, compare the dimensions: the one where a single
value holds the largest share of the change says the most about where it happened.
Added after a holdout failure (ha08): the planner chose between region and segment
differently from run to run, and that choice alone decided the answer, although
the data clearly favoured one. Nothing here reads the question; the choice comes
from the data.

The share favours dimensions with few values, since each value starts with a larger
share by chance. Plain share is kept because it is easy to explain and reads the
same way as the tool's own share_of_change_pct.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agentcrew.compute.functions import ComputeError
from agentcrew.compute.metrics import (
    Comparison,
    Runner,
    _metric,
    _source_of,
    contribution_by_dimension,
)

#: Two periods split a time dimension into different values on each side, so its
#: "contribution" only restates the comparison.
TIME_DIMENSIONS = frozenset({"month", "quarter", "year"})


@dataclass(frozen=True)
class Concentration:
    dimension: str
    item: object
    share_pct: float


def _candidates(metric_name: str, comparison: Comparison,
                preferred: str | None) -> list[str]:
    """Dimensions that can explain the change, the plan's first so a tie keeps it.
    A dimension the comparison already fixes holds the whole change trivially."""
    fixed = {*comparison.filters, comparison.group_dimension}
    names = [name for name in _source_of(_metric(metric_name)).dimensions
             if name not in TIME_DIMENSIONS and name not in fixed]
    return sorted(names, key=lambda name: name != preferred)


def concentrating_dimension(
    run: Runner, metric_name: str, comparison: Comparison,
    preferred: str | None = None,
) -> Concentration | None:
    """The dimension whose largest contributor holds the largest share of the
    change, or None if no dimension can say (no change, or a metric that cannot be
    broken down). Costs two queries per dimension and no LLM call."""
    if not _metric(metric_name).additive:
        return None
    best: Concentration | None = None
    for dimension in _candidates(metric_name, comparison, preferred):
        try:
            table, _ = contribution_by_dimension(run, metric_name, dimension,
                                                 comparison)
        except ComputeError:
            continue
        top = table[table["largest_contributor"]]
        if table["item"].nunique() < 2 or top.empty:
            continue
        share = top.iloc[0]["share_of_change_pct"]
        if pd.isna(share):  # no change: the share is undefined
            continue
        if best is None or share > best.share_pct:
            best = Concentration(dimension, top.iloc[0]["item"], float(share))
    return best
