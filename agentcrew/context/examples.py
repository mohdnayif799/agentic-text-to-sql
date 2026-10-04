"""Verified question/SQL examples and TF-IDF retrieval.

The pool lives in examples.yaml (built by scripts/build_example_pool.py). It
holds question text and SQL only, never results, and excludes anything that
paraphrases or shares an answer with an evaluation question.

TF-IDF is hand-rolled (word unigrams and bigrams, a small stop-word list,
smoothed IDF, cosine similarity) to avoid a scikit-learn dependency for a
pool of a few dozen items. The same function is used to screen the pool for
overlap with evaluation questions.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

EXAMPLES_PATH = Path(__file__).with_name("examples.yaml")

STOPWORDS = frozenset(
    "a an and are as at be by did do does for from give how in is it its of "
    "on or our the that their them this to was we were what when which who "
    "with".split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


@dataclass(frozen=True)
class Example:
    id: str
    question: str
    sql: str


def load_examples(path: Path = EXAMPLES_PATH) -> tuple[Example, ...]:
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    return tuple(
        Example(id=e["id"], question=e["question"], sql=e["sql"].strip())
        for e in data.get("pool", [])
    )


def terms(text: str) -> list[str]:
    words = [w for w in _TOKEN.findall(text.lower()) if w not in STOPWORDS]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


class TfidfIndex:
    """Cosine similarity of a query against a fixed set of documents."""

    def __init__(self, documents: Sequence[str]) -> None:
        counts = [Counter(terms(d)) for d in documents]
        n = len(documents)
        df = Counter(t for c in counts for t in c)
        self._default_idf = math.log(1 + n) + 1
        self._idf = {t: math.log((1 + n) / (1 + k)) + 1 for t, k in df.items()}
        self._vectors = [self._vector(c) for c in counts]

    def _vector(self, counts: Counter) -> dict[str, float]:
        weights = {
            t: tf * self._idf.get(t, self._default_idf) for t, tf in counts.items()
        }
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {t: w / norm for t, w in weights.items()}

    def similarities(self, query: str) -> list[float]:
        q = self._vector(Counter(terms(query)))
        return [sum(w * v.get(t, 0.0) for t, w in q.items()) for v in self._vectors]


class ExampleIndex:
    def __init__(self, examples: Sequence[Example]) -> None:
        self.examples = tuple(examples)
        self._index = TfidfIndex([e.question for e in self.examples])

    def top_k(self, question: str, k: int = 3) -> tuple[Example, ...]:
        """The k most similar examples with a non-zero similarity."""
        scored = sorted(
            zip(self._index.similarities(question), range(len(self.examples)),
                strict=True),
            key=lambda pair: (-pair[0], pair[1]),
        )
        return tuple(self.examples[i] for s, i in scored[:k] if s > 0)


def pairwise_similarity(left: Sequence[str], right: Sequence[str]) -> list[list[float]]:
    """Cosine similarity of every left text to every right text, with IDF fitted
    on both sets together (used to screen the pool against eval questions)."""
    index = TfidfIndex(list(right) + list(left))
    return [index.similarities(text)[: len(right)] for text in left]
