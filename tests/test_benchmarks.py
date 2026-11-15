"""Lab 8: benchmarks that run in CI (``pytest-benchmark``).

In a normal ``pytest`` run the ``benchmark`` fixture calls each function once
(``--benchmark-disable`` in ``pyproject.toml``), so these are ordinary tests.
CI's benchmark step runs them for real (``--benchmark-enable``), compares the
medians with the previous run on ``main`` and fails on a 50 % regression;
``test_numpy_scorer_beats_lab3_on_a_frequent_term`` additionally fails, on any
machine, if the vectorized scorer stops being several times faster.
"""

from __future__ import annotations

import random
import timeit
from pathlib import Path
from typing import Any

import pytest

from findex.corpus import Document, iter_documents
from findex.index import Index, build_index
from findex.npindex import to_numpy
from findex.rank import BM25, search
from findex.vecrank import search_np


@pytest.fixture(scope="module")
def synthetic() -> tuple[Index, Index]:
    """3,000 documents of Zipf-distributed words: big enough for a frequent term."""
    rng = random.Random(8)
    vocab = [f"w{i}" for i in range(2_000)]
    weights = [1 / (i + 1) for i in range(len(vocab))]
    docs = [
        Document(f"doc{i}", Path(f"doc{i}"), " ".join(rng.choices(vocab, weights, k=120)))
        for i in range(3_000)
    ]
    slots = build_index(docs, positions=False)
    return slots, to_numpy(slots)


def test_bench_build_index(benchmark: Any, corpus_dir: Path) -> None:
    ix = benchmark(lambda: build_index(iter_documents(corpus_dir), positions=True))
    assert ix.num_docs > 0


def test_bench_build_index_numpy(benchmark: Any, corpus_dir: Path) -> None:
    ix = benchmark(lambda: build_index(iter_documents(corpus_dir), "numpy", positions=True))
    assert ix.layout == "numpy"


@pytest.mark.parametrize("query", ["w0", "w1 w2 w3", "w1999"])
def test_bench_search_lab3(benchmark: Any, synthetic: tuple[Index, Index], query: str) -> None:
    slots, _ = synthetic
    results = benchmark(search, slots, query, BM25(), 10)
    assert results


@pytest.mark.parametrize("query", ["w0", "w1 w2 w3", "w1999"])
def test_bench_search_numpy(benchmark: Any, synthetic: tuple[Index, Index], query: str) -> None:
    _, nx = synthetic
    results = benchmark(search_np, nx, query, "bm25", 10)
    assert results


def test_numpy_scorer_beats_lab3_on_a_frequent_term(synthetic: tuple[Index, Index]) -> None:
    slots, nx = synthetic
    assert [r.doc_id for r in search(slots, "w0")] == [r.doc_id for r in search_np(nx, "w0")]
    lab3 = min(timeit.repeat(lambda: search(slots, "w0", BM25(), 10), number=5, repeat=5))
    vec = min(timeit.repeat(lambda: search_np(nx, "w0", "bm25", 10), number=5, repeat=5))
    assert lab3 / vec > 5, f"NumPy scorer only {lab3 / vec:.1f}× faster than Lab 3"
