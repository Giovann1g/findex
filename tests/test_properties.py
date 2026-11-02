"""Property-based tests (Hypothesis): statements that hold for *every* input.

When a property fails, Hypothesis shrinks the failing input to a minimal
counterexample before reporting it — a 2-word corpus instead of a 40-word one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from findex import store
from findex.corpus import Document
from findex.index import Index, Layout, build_index
from findex.merge import SET, difference, intersect, intersect_gallop, union
from findex.query import And, Node, Not, Or, Phrase, Term, parse
from findex.tokenize import tokenize

# --- strategies ---------------------------------------------------------------
WORDS = ["cat", "dog", "Straße", "café", "п'ять", "loop", "event", "x2", "the", "a"]
texts = st.lists(st.sampled_from(WORDS), max_size=12).map(" ".join)
corpora = st.lists(texts, min_size=1, max_size=8)
sorted_ids = st.lists(st.integers(0, 300), unique=True, max_size=60).map(sorted)


def build(docs: list[str], layout: Layout = "slots", positions: bool = False) -> Index:
    stream = (Document(f"d{i}", Path(f"d{i}"), t) for i, t in enumerate(docs))
    return build_index(stream, layout, positions)


# --- 1. load(save(index)) == index, for every format -----------------------------
@settings(max_examples=60, deadline=None)
@given(docs=corpora, positions=st.booleans(), suffix=st.sampled_from([".pkl", ".json", ".bin"]))
def test_save_load_round_trip(docs: list[str], positions: bool, suffix: str) -> None:
    keep_positions = positions and suffix != ".bin"  # .bin stores no positions
    ix = build(docs, positions=keep_positions)
    with tempfile.TemporaryDirectory() as tmp:  # not tmp_path: one dir per example
        path = Path(tmp) / f"ix{suffix}"
        store.save(ix, path)
        back = store.load(path)
    assert back.num_docs == ix.num_docs
    assert back.doc_lengths == ix.doc_lengths and back.doc_meta == ix.doc_meta
    assert set(back) == set(ix)
    for term in ix:
        assert list(back.doc_ids(term)) == list(ix.doc_ids(term))
        assert list(back.tfs(term)) == list(ix.tfs(term))
        if keep_positions:
            assert [p.positions for p in back[term]] == [p.positions for p in ix[term]]


# --- 2. postings are always sorted, unique and consistent ------------------------
@settings(max_examples=150)
@given(docs=corpora, layout=st.sampled_from(["plain", "slots", "array"]))
def test_postings_invariants(docs: list[str], layout: Layout) -> None:
    ix = build(docs, layout)
    total = 0
    for term in ix:
        ids = list(ix.doc_ids(term))
        assert ids == sorted(set(ids))  # strictly increasing doc ids
        assert all(tf > 0 for tf in ix.tfs(term))
        total += sum(ix.tfs(term))
    assert total == sum(ix.doc_lengths.values())  # every token counted exactly once
    assert ix.num_docs == len(docs)


# --- 3. the merges agree with Python's set algebra (the oracle) ------------------
@given(a=sorted_ids, b=sorted_ids)
def test_merges_match_set_operations(a: list[int], b: list[int]) -> None:
    assert intersect(a, b) == sorted(set(a) & set(b)) == SET.and_(a, b)
    assert intersect_gallop(a, b) == intersect(a, b)
    assert union(a, b) == sorted(set(a) | set(b))
    assert difference(a, b) == sorted(set(a) - set(b))


# --- 4. the tokenizer: no empty tokens, and normalization is a fixed point -------
@pytest.mark.slow  # ~1.4 s: 300 arbitrary Unicode strings through NFC/casefold
@settings(max_examples=300)
@given(st.text())
def test_tokens_are_non_empty_and_stable(text: str) -> None:
    tokens = list(tokenize(text))
    assert all(tokens)
    assert all(not any(c.isspace() for c in t) for t in tokens)
    for t in tokens:  # an already-normalized token re-tokenizes to itself
        assert list(tokenize(t)) == [t]


# --- 5. the parser inverts str(): parse(str(tree)) == tree -----------------------
words = st.sampled_from(["cat", "dog", "loop", "and", "or", "not", "x2"])
leaves = st.one_of(
    words.map(Term),
    st.lists(words, min_size=1, max_size=3).map(lambda ws: Phrase(tuple(ws))),
)


def _extend(children: st.SearchStrategy[Node]) -> st.SearchStrategy[Node]:
    return st.one_of(
        st.builds(And, children, children),
        st.builds(Or, children, children),
        st.builds(Not, children),
    )


trees = st.recursive(leaves, _extend, max_leaves=12)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(trees)
def test_parse_inverts_str(tree: Node) -> None:
    assert parse(str(tree)) == tree
