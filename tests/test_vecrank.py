"""Lab 8: the numpy layout and the vectorized scorers rank exactly like Lab 3."""

from __future__ import annotations

import itertools
import pickle
import tempfile
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from findex.corpus import Document, iter_documents
from findex.index import Index, build_index
from findex.merge import MERGE
from findex.npindex import NUMPY, CsrPostings, to_numpy
from findex.query import parse
from findex.rank import BM25, TfIdf, search
from findex.vecrank import bm25_scores, lengths_of, search_np, top_k


def same_ranking(slots: Index, numpy_ix: Index, query: str, k: int) -> None:
    for lab3, vec in ((BM25(), "bm25"), (TfIdf(), "tfidf")):
        a = [(r.doc_id, r.score) for r in search(slots, query, scorer=lab3, k=k)]
        b = [(r.doc_id, r.score) for r in search_np(numpy_ix, query, scorer=vec, k=k)]
        assert a == b, (query, vec, k)  # same documents, same order, same floats


# --- the layout ------------------------------------------------------------------------
def test_numpy_layout_holds_the_same_postings(corpus_index: Index) -> None:
    nx = to_numpy(corpus_index)
    assert nx.layout == "numpy" and isinstance(nx.postings, CsrPostings)
    assert list(nx) == list(corpus_index) and len(nx) == len(corpus_index)
    for term in corpus_index:
        assert list(nx.doc_ids(term)) == list(corpus_index.doc_ids(term))
        assert list(nx.tfs(term)) == list(corpus_index.tfs(term))
        assert nx.df(term) == corpus_index.df(term)
        assert list(nx[term]) == list(corpus_index[term])  # Posting objects, positions too
    assert nx.postings.doc_ids.dtype == np.int32 and nx.postings.tfs.dtype == np.int32
    assert nx.lengths is not None and nx.lengths.dtype == np.int32
    assert nx.lengths.tolist() == [corpus_index.doc_length(d) for d in range(nx.num_docs)]
    assert nx.collection_frequency("loop") == corpus_index.collection_frequency("loop")
    assert type(nx.collection_frequency("loop")) is int


def test_numpy_index_pickles_and_builds_directly(corpus_dir: Path, corpus_index: Index) -> None:
    nx = to_numpy(corpus_index)
    again = pickle.loads(pickle.dumps(nx, protocol=pickle.HIGHEST_PROTOCOL))
    assert again == nx and again.lengths is not None
    assert np.array_equal(again.lengths, nx.lengths)  # type: ignore[arg-type]
    direct = build_index(iter_documents(corpus_dir), layout="numpy", positions=True)
    assert direct == nx and direct.has_positions
    no_pos = build_index(iter_documents(corpus_dir), layout="numpy")
    assert not no_pos.has_positions and no_pos.postings.pos_flat is None  # type: ignore[union-attr]


def test_match_returns_plain_ints_on_every_engine(corpus_index: Index) -> None:
    nx = to_numpy(corpus_index)
    for q in ["loop", "event AND loop", "loop OR needle", "loop NOT asyncio", '"event loop"']:
        node = parse(q)
        assert node is not None
        expected = corpus_index.match(node, MERGE)
        assert nx.match(node, NUMPY) == expected == nx.match(node, MERGE)
        assert all(type(d) is int for d in nx.match(node, NUMPY))


# --- identical rankings ---------------------------------------------------------------------
def test_ranking_identical_to_lab3_on_the_fixture_corpus(corpus_index: Index) -> None:
    nx = to_numpy(corpus_index)
    terms = sorted(corpus_index)
    queries = list(terms)
    queries += [f"{a} {b}" for a, b in itertools.combinations(terms[:25], 2)]
    queries += [f"{a} OR {b}" for a, b in itertools.combinations(terms[:25], 2)]
    queries += ["NOT loop", "loop NOT asyncio", '"event loop"', '"event loop" OR needle']
    for q in queries:
        for k in (1, 2, 3, 10):  # small k: ties at the top-k boundary get decided by id
            same_ranking(corpus_index, nx, q, k)


@settings(max_examples=60, deadline=None)
@given(
    texts=st.lists(
        st.lists(st.sampled_from(["a", "b", "c", "d", "e"]), min_size=1, max_size=12).map(" ".join),
        min_size=1,
        max_size=25,
    ),
    query=st.sampled_from(["a", "b c", "a OR e", "a NOT b", "c d e", '"a b"', "NOT c"]),
    k=st.integers(1, 8),
)
def test_ranking_identical_on_random_corpora(texts: list[str], query: str, k: int) -> None:
    # five words, short documents: equal scores everywhere — ties are the hard part
    docs = [Document(f"d{i}", Path(f"d{i}"), t) for i, t in enumerate(texts)]
    slots = build_index(docs, positions=True)
    same_ranking(slots, to_numpy(slots), query, k)


# --- the pieces -------------------------------------------------------------------------------
def test_top_k_breaks_ties_by_doc_id() -> None:
    ids = np.array([9, 3, 7, 1, 5], dtype=np.int64)
    scores = np.array([1.0, 2.0, 2.0, 2.0, 0.5])
    assert top_k(ids, scores, 2).tolist() == [1, 3]  # three tie for 2.0: lowest ids win
    assert top_k(ids, scores, 10).tolist() == [1, 3, 7, 9, 5]
    assert top_k(ids, scores, 0).tolist() == []


def test_bm25_scores_match_the_per_posting_formula(corpus_index: Index) -> None:
    bm = BM25()
    vec = bm25_scores(corpus_index, ["event", "loop"])
    for d in range(corpus_index.num_docs):
        expected = sum(
            bm.score(t, p, corpus_index) for t in ("event", "loop") for p in corpus_index[t]
            if p.doc_id == d
        )  # fmt: skip
        assert vec[d] == expected
    assert lengths_of(corpus_index).dtype == np.int32


@pytest.mark.parametrize("engine_op", ["and_", "or_", "not_"])
def test_numpy_set_operations_equal_the_merges(engine_op: str) -> None:
    rng = np.random.default_rng(3)
    for _ in range(50):
        a = sorted(set(rng.integers(0, 40, size=int(rng.integers(0, 20))).tolist()))
        b = sorted(set(rng.integers(0, 40, size=int(rng.integers(0, 20))).tolist()))
        assert getattr(NUMPY, engine_op)(a, b) == getattr(MERGE, engine_op)(a, b)


def test_cli_default_layout_is_numpy(corpus_dir: Path) -> None:
    from typer.testing import CliRunner

    from findex.cli import app
    from findex.store import load

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ix.pkl"
        r = CliRunner().invoke(app, ["index", str(corpus_dir), "-o", str(out), "--positions"])
        assert r.exit_code == 0, r.output
        assert load(out).layout == "numpy"
        found = CliRunner().invoke(app, ["search", str(out), '"event loop"', "--json"])
        assert found.exit_code == 0 and '"title": "Asyncio"' in found.stdout
