"""Ranking: interchangeable scorers and ranked top-k search.

A *scorer* is anything with ``score(term, posting, index) -> float`` — that is
the whole :class:`Scorer` protocol. ``TfIdf`` and ``BM25`` don't inherit from
it or from each other; ``search`` only relies on the method being there
(duck typing, checked statically by the Protocol and at runtime by
``isinstance(x, Scorer)`` thanks to ``@runtime_checkable``).

Formulas (N = documents, df = documents containing the term, tf = occurrences
of the term in the document, dl = document length in tokens, avgdl = mean dl)::

    TfIdf:  (1 + ln tf) · ln(N / df)
    BM25:   idf · tf·(k1 + 1) / (tf + k1·(1 − b + b·dl/avgdl))
            idf = ln(1 + (N − df + 0.5) / (df + 0.5))

``1 + ln tf`` already damps repetitions, but it never stops growing; BM25's
fraction tends to ``k1 + 1`` (saturation, set by ``k1``) and divides by the
relative document length (normalization, set by ``b``; ``b = 0`` turns it off).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from findex.merge import MERGE, Engine
from findex.query import Node, parse
from findex.timing import timed

if TYPE_CHECKING:
    from findex.index import Index, Posting


@runtime_checkable
class Scorer(Protocol):
    def score(self, term: str, posting: Posting, index: Index) -> float: ...


@dataclass(frozen=True, slots=True)
class TfIdf:
    """Log-scaled tf times plain idf. A term in every document scores 0."""

    def idf(self, term: str, index: Index) -> float:
        return math.log(index.num_docs / index.df(term))

    def score(self, term: str, posting: Posting, index: Index) -> float:
        return (1 + math.log(posting.tf)) * self.idf(term, index)

    def __call__(self, term: str, posting: Posting, index: Index) -> float:
        return self.score(term, posting, index)


@dataclass(frozen=True, slots=True)
class BM25:
    """Okapi BM25 with the always-positive (Lucene) idf."""

    k1: float = 1.5
    b: float = 0.75

    def __post_init__(self) -> None:
        if self.k1 < 0 or not 0 <= self.b <= 1:
            raise ValueError(f"need k1 >= 0 and 0 <= b <= 1, got k1={self.k1}, b={self.b}")

    def idf(self, term: str, index: Index) -> float:
        n, df = index.num_docs, index.df(term)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def tf_part(self, tf: int, doc_length: int, avg_doc_length: float) -> float:
        """The saturating, length-normalized tf factor; tends to k1 + 1."""
        norm = self.k1 * (1 - self.b + self.b * doc_length / avg_doc_length)
        return tf * (self.k1 + 1) / (tf + norm)

    def score(self, term: str, posting: Posting, index: Index) -> float:
        dl = index.doc_length(posting.doc_id)
        return self.idf(term, index) * self.tf_part(posting.tf, dl, index.avg_doc_length)

    def __call__(self, term: str, posting: Posting, index: Index) -> float:
        return self.score(term, posting, index)


SCORERS = {"bm25": BM25, "tfidf": TfIdf}

# Defaults are evaluated once, at ``def`` time, and shared by every call. That
# is the classic mutable-default bug for lists; here it's harmless because
# BM25 is frozen, but a named module-level value makes the sharing explicit.
DEFAULT_SCORER: Scorer = BM25()


@dataclass(frozen=True, slots=True, order=True)
class SearchResult:
    """One ranked hit. ``order=True`` compares only the hidden ``rank_key``
    (−score, doc_id), so ``sorted(results)`` is best-first with a stable
    tie-break by doc id, and ``min(results)`` is the top hit."""

    rank_key: tuple[float, int] = field(init=False, repr=False)
    doc_id: int = field(compare=False)
    score: float = field(compare=False)
    title: str = field(compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rank_key", (-self.score, self.doc_id))


@timed
def search(
    index: Index,
    query: str | Node,
    scorer: Scorer = DEFAULT_SCORER,
    k: int = 10,
    engine: Engine = MERGE,
) -> list[SearchResult]:
    """Top-``k`` documents for ``query``, best first.

    1. The Boolean tree decides *which* documents qualify (cached per index).
    2. Every positive term (not under NOT) adds ``scorer.score`` for each
       qualifying document it occurs in — the term-at-a-time accumulator.
    3. ``heapq.nlargest`` keeps only k candidates: O(n log k), not a full sort.
    """
    node = parse(query) if isinstance(query, str) else query
    if node is None or k <= 0:
        return []
    matched = index.match(node, engine)
    if not matched:
        return []
    allowed = frozenset(matched)
    scores = dict.fromkeys(matched, 0.0)  # pure-NOT queries still return documents
    for term in sorted(node.terms()):
        if term not in index:
            continue
        for posting in index[term]:
            if posting.doc_id in allowed:
                scores[posting.doc_id] += scorer.score(term, posting, index)
    top = heapq.nlargest(k, scores.items(), key=lambda item: (item[1], -item[0]))
    return [SearchResult(doc_id, score, index.title(doc_id)) for doc_id, score in top]
