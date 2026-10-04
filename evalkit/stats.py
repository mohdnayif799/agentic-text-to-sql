"""Small-sample statistics for paired pass/fail evaluation.

Wilson score intervals for accuracy, and the exact (binomial) McNemar test
for two architectures graded on the same questions. Exact rather than the
chi-square approximation because the discordant counts here are tiny.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

Z_95 = 1.959963984540054


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for k successes out of n."""
    if n == 0:
        return 0.0, 1.0
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts.

    b = questions only arm 1 passed, c = questions only arm 2 passed. Under
    the null each discordant question is a fair coin, so p is the two-sided
    binomial tail at min(b, c), capped at 1.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_counts(
    a: Mapping[str, bool], b: Mapping[str, bool]
) -> dict[str, int]:
    """2x2 table of paired outcomes. Both arms must cover the same questions."""
    if set(a) != set(b):
        raise ValueError(
            f"arms graded on different questions: {sorted(set(a) ^ set(b))}"
        )
    counts = {"both": 0, "neither": 0, "only_a": 0, "only_b": 0}
    for qid, a_pass in a.items():
        b_pass = b[qid]
        key = (
            "both" if a_pass and b_pass
            else "neither" if not (a_pass or b_pass)
            else "only_a" if a_pass
            else "only_b"
        )
        counts[key] += 1
    return counts
