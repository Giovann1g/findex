"""The ``numpy`` layout: postings as NumPy ``int32`` arrays (Lab 8).

All postings of the index live in **one** pair of ``int32`` arrays, term after
term (the CSR — "compressed sparse row" — layout of a sparse term × document
matrix)::

    doc_ids  [ 0 4 9 | 1 2 | 4 … ]      int32, one per posting
    tfs      [ 3 1 7 | 1 1 | 2 … ]      int32, aligned with doc_ids
    spans    {"asyncio": (0, 3), "await": (3, 5), …}   term -> [start, end)

``postings["asyncio"]`` is ``(doc_ids[0:3], tfs[0:3])`` — two *views*, no copy.
Positions, when kept, are the same trick one level down: a flat ``int32``
array of token positions and an offsets array with one entry per posting.
Document lengths are a single ``int32`` array (``Index.lengths``).

Why: the Lab 8 profile showed 1.4 s of every ``findex search`` spent unpickling
329,364 ``Posting`` objects one Python ``__setstate__`` at a time. A few big
arrays pickle as raw bytes; and the scorers in :mod:`findex.vecrank` can
compute a whole postings list with one array expression.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from findex.merge import Engine

if TYPE_CHECKING:
    from findex.index import Index, Posting

type I32 = NDArray[np.int32]
type NpPostings = tuple[I32, I32]  # (doc_ids, tfs) — views into the CSR arrays


class CsrPostings(Mapping[str, NpPostings]):
    """``term -> (doc_ids, tfs)`` backed by one pair of ``int32`` arrays."""

    def __init__(
        self,
        doc_ids: I32,
        tfs: I32,
        spans: dict[str, tuple[int, int]],
        pos_offsets: I32 | None = None,
        pos_flat: I32 | None = None,
    ) -> None:
        self.doc_ids = doc_ids
        self.tfs = tfs
        self.spans = spans
        self.pos_offsets = pos_offsets  # len == len(doc_ids) + 1
        self.pos_flat = pos_flat

    def __getitem__(self, term: str) -> NpPostings:
        start, end = self.spans[term]
        return self.doc_ids[start:end], self.tfs[start:end]

    def __iter__(self) -> Iterator[str]:
        return iter(self.spans)

    def __len__(self) -> int:
        return len(self.spans)

    def __contains__(self, term: object) -> bool:
        return term in self.spans

    @property
    def nbytes(self) -> int:
        arrays = (self.doc_ids, self.tfs, self.pos_offsets, self.pos_flat)
        return sum(a.nbytes for a in arrays if a is not None)

    def records(self, term: str) -> list[Posting]:
        """``Posting`` objects for one term (what phrase matching reads)."""
        from findex.index import Posting

        start, end = self.spans[term]
        ids = self.doc_ids[start:end].tolist()
        tfs = self.tfs[start:end].tolist()
        if self.pos_offsets is None or self.pos_flat is None:
            return [Posting(d, t) for d, t in zip(ids, tfs, strict=True)]
        offs = self.pos_offsets[start : end + 1].tolist()
        flat = self.pos_flat
        return [
            Posting(d, t, tuple(flat[offs[i] : offs[i + 1]].tolist()))
            for i, (d, t) in enumerate(zip(ids, tfs, strict=True))
        ]


def to_numpy(index: Index) -> Index:
    """The same index in the ``numpy`` layout (any source layout)."""
    from findex.index import Index

    terms = list(index)
    df = np.fromiter((index.df(t) for t in terms), dtype=np.int64, count=len(terms))
    ends = np.cumsum(df)
    starts = ends - df
    total = int(ends[-1]) if len(terms) else 0
    doc_ids = np.empty(total, dtype=np.int32)
    tfs = np.empty(total, dtype=np.int32)
    for term, s, e in zip(terms, starts.tolist(), ends.tolist(), strict=True):
        doc_ids[s:e] = index.doc_ids(term)
        tfs[s:e] = index.tfs(term)
    spans = {t: (s, e) for t, s, e in zip(terms, starts.tolist(), ends.tolist(), strict=True)}

    pos_offsets: I32 | None = None
    pos_flat: I32 | None = None
    if index.has_positions:
        counts = np.empty(total, dtype=np.int64)
        chunks: list[Sequence[int]] = []
        i = 0
        for term in terms:
            for p in index[term]:
                pos = p.positions or ()
                counts[i] = len(pos)
                chunks.append(pos)
                i += 1
        pos_offsets = np.zeros(total + 1, dtype=np.int32)
        pos_offsets[1:] = np.cumsum(counts)
        pos_flat = np.fromiter(
            (x for c in chunks for x in c), dtype=np.int32, count=int(pos_offsets[-1])
        )

    n = index.num_docs
    lengths = np.array([index.doc_lengths[i] for i in range(n)], dtype=np.int32)
    ix = Index(
        CsrPostings(doc_ids, tfs, spans, pos_offsets, pos_flat),
        dict(index.doc_lengths),
        dict(index.doc_meta),
        "numpy",
        index.has_positions,
        index.meta,
    )
    ix.lengths = lengths
    return ix


# --- Boolean operators over sorted int arrays ----------------------------------------
def _arr(a: Sequence[int]) -> NDArray[np.int64]:
    return np.asarray(a, dtype=np.int64)


def _and(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return np.intersect1d(_arr(a), _arr(b), assume_unique=True).tolist()


def _or(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return np.union1d(_arr(a), _arr(b)).tolist()


def _not(a: Sequence[int], b: Sequence[int]) -> list[int]:
    return np.setdiff1d(_arr(a), _arr(b), assume_unique=True).tolist()


NUMPY = Engine("numpy", _and, _or, _not)
