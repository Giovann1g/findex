"""BM25 and TF-IDF as array expressions, top-k with ``np.argpartition`` (Lab 8).

:func:`findex.rank.search` (Lab 3) calls ``scorer.score(term, posting, index)``
once per posting: a Python method call, an ``idf`` recomputed with
``math.log``, a ``df`` and a ``doc_length`` lookup — hundreds of interpreter
round trips for one frequent term. Here each query term costs a handful of
NumPy calls over its whole postings list::

    norm   = k1 · (1 − b + b·lengths / avgdl)            # once per index, all docs
    scores[ids] += idf · tf·(k1 + 1) / (tf + norm[ids])  # once per term, all postings

``scores`` is a dense ``float64`` array over all documents; the Boolean query
still decides *which* documents qualify (``index.match`` with the NumPy set
operations), and only those compete for the top k.

**Same results as Lab 3, to the last bit.** The order of the arithmetic
follows :mod:`findex.rank` operation for operation (IEEE-754 ``+ − × ÷`` are
exactly rounded, so equal inputs give equal floats), idf is a scalar computed
with ``math.log`` like before, and TF-IDF's ``ln tf`` is taken with
``math.log`` on the few distinct tf values — ``np.log`` may differ from the C
library in the last bit, which is enough to swap two tied documents. Ties are
broken by doc id, as in ``SearchResult``. ``tests/test_vecrank.py`` checks the
order against Lab 3 on the fixture corpus and on random corpora.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
from numpy.typing import NDArray

from findex.index import Index
from findex.npindex import NUMPY
from findex.query import Node, parse
from findex.rank import BM25, ScorerName, SearchResult, TfIdf
from findex.timing import timed

type F64 = NDArray[np.float64]
type I64 = NDArray[np.int64]


def lengths_of(index: Index) -> NDArray[np.int32]:
    """Document lengths as one ``int32`` array (stored by the numpy layout)."""
    if index.lengths is None:
        n = index.num_docs
        index.lengths = np.array([index.doc_lengths[i] for i in range(n)], dtype=np.int32)
    return index.lengths


def bm25_norm(index: Index, k1: float, b: float) -> F64:
    """``k1 · (1 − b + b·dl/avgdl)`` for every document, cached per (k1, b)."""
    key = f"bm25_norm:{k1}:{b}"
    cache: dict[str, F64] = index.__dict__.setdefault("_vec_cache", {})
    if key not in cache:
        dl = lengths_of(index).astype(np.float64)
        cache[key] = k1 * (1 - b + b * dl / index.avg_doc_length)
    return cache[key]


def term_arrays(index: Index, term: str) -> tuple[NDArray[np.integer], NDArray[np.integer]]:
    """``(doc_ids, tfs)`` of a term as arrays — views, no copy, in the numpy layout."""
    return np.asarray(index.doc_ids(term)), np.asarray(index.tfs(term))


def log_table(index: Index) -> F64:
    """``ln(tf)`` for every tf that occurs, computed with ``math.log`` (bit-exact
    with Lab 3), once per index; TF-IDF then just indexes into it."""
    cache: dict[str, F64] = index.__dict__.setdefault("_vec_cache", {})
    if "log_tf" not in cache:
        top = max((int(np.max(np.asarray(index.tfs(t)))) for t in index), default=1)
        cache["log_tf"] = np.array([0.0] + [math.log(t) for t in range(1, top + 1)])
    return cache["log_tf"]


def matched(index: Index, node: Node) -> I64:
    """``index.match`` as a read-only ``int64`` array, cached per index and query
    (converting the cached tuple on every call cost more than the scoring)."""
    cache: dict[Node, I64] = index.__dict__.setdefault("_np_match", {})
    ids = cache.get(node)
    if ids is None:
        ids = np.asarray(index.match(node, NUMPY), dtype=np.int64)
        ids.setflags(write=False)
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))  # oldest first: a small FIFO
        cache[node] = ids
    return ids


def bm25_scores(index: Index, terms: Iterable[str], k1: float = 1.5, b: float = 0.75) -> F64:
    """BM25 of every document for ``terms`` (summed in the given order)."""
    n = index.num_docs
    scores = np.zeros(n, dtype=np.float64)
    norm = bm25_norm(index, k1, b)
    for term in terms:
        df = index.df(term)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))  # a scalar, as in Lab 3
        ids, tf = term_arrays(index, term)
        scores[ids] += idf * (tf * (k1 + 1) / (tf + norm[ids]))
    return scores


def tfidf_scores(index: Index, terms: Iterable[str]) -> F64:
    """TF-IDF ``(1 + ln tf) · ln(N/df)`` of every document for ``terms``."""
    n = index.num_docs
    scores = np.zeros(n, dtype=np.float64)
    for term in terms:
        df = index.df(term)
        if df == 0:
            continue
        idf = math.log(n / df)
        ids, tf = term_arrays(index, term)
        scores[ids] += (1 + log_table(index)[tf]) * idf
    return scores


def top_k(ids: I64, scores: F64, k: int) -> I64:
    """The ``k`` best ``ids`` by score (desc), ties by id (asc) — like ``SearchResult``.

    ``np.argpartition`` finds the k largest in O(n) without sorting the rest;
    every candidate tied with the k-th score is kept too, so a tie at the
    boundary is decided by doc id exactly as Lab 3's ``heapq.nlargest`` does.
    Only that short list is then fully sorted.
    """
    if k <= 0 or ids.size == 0:
        return ids[:0]
    if ids.size > k:
        part = np.argpartition(-scores, k - 1)[:k]
        keep = scores >= scores[part].min()
        ids, scores = ids[keep], scores[keep]
    order = np.lexsort((ids, -scores))[:k]
    return ids[order]


@timed
def search_np(
    index: Index,
    query: str | Node,
    scorer: ScorerName | BM25 | TfIdf = "bm25",
    k: int = 10,
) -> list[SearchResult]:
    """:func:`findex.rank.search`, vectorized: same results, same order."""
    node = parse(query) if isinstance(query, str) else query
    if node is None or k <= 0:
        return []
    hits = matched(index, node)
    if hits.size == 0:
        return []
    terms = sorted(node.terms())
    if scorer == "tfidf" or isinstance(scorer, TfIdf):
        scores = tfidf_scores(index, terms)
    else:
        bm = scorer if isinstance(scorer, BM25) else BM25()
        scores = bm25_scores(index, terms, bm.k1, bm.b)
    best = top_k(hits, scores[hits], k)
    return [SearchResult(d, float(scores[d]), index.title(d)) for d in best.tolist()]
