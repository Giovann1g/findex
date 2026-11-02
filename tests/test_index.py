"""Index building, Boolean search and persistence."""

import pickle
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from findex import store
from findex.corpus import Document
from findex.index import LAYOUTS, DocMeta, Posting, build_index, guess_title
from findex.merge import MERGE, SET, difference, intersect, union
from findex.query import And, Term, parse


def evaluate(index, query, engine=MERGE):
    node = parse(query)
    return [] if node is None else list(node.evaluate(index, engine))


TEXTS = [
    "Hello world. The cat sat.",  # 0
    "The dog and the cat.",  # 1
    "Привіт world!",  # 2
    "Dogs are not cats; the dog barks.",  # 3
]


def docs():
    return (Document(f"d{i}.txt", Path(f"d{i}.txt"), t) for i, t in enumerate(TEXTS))


@pytest.fixture(params=LAYOUTS)
def index(request):
    return build_index(docs(), layout=request.param)


# --- records ---------------------------------------------------------------
def test_records_are_frozen_slotted_hashable():
    p = Posting(3, 2)
    assert {p, Posting(3, 2), Posting(4, 1)} == {Posting(3, 2), Posting(4, 1)}
    assert not hasattr(p, "__dict__")
    with pytest.raises(FrozenInstanceError):
        p.tf = 5  # type: ignore[misc]  # on purpose: the test expects FrozenInstanceError
    assert hash(DocMeta(1, "a", "A")) == hash(DocMeta(1, "a", "A"))
    assert len({DocMeta(1, "a", "A"), DocMeta(1, "a", "A")}) == 1


# --- building --------------------------------------------------------------
def test_postings_sorted_and_counted(index):
    assert list(index.doc_ids("the")) == [0, 1, 3]
    assert list(index.tfs("the")) == [1, 2, 1]
    assert list(index.doc_ids("dog")) == [1, 3]
    for term in index.postings:
        ids = list(index.doc_ids(term))
        assert ids == sorted(set(ids)), term
    assert index.doc_lengths == {0: 5, 1: 5, 2: 2, 3: 7}
    assert index.doc_meta[2].path == "d2.txt"


def test_build_consumes_a_stream():
    idx = build_index(iter(list(docs())))  # a one-shot iterator is enough
    assert idx.num_docs == 4


def test_positions_flag():
    idx = build_index(docs(), positions=True)
    (p0, p1, p3) = idx.postings["the"]
    assert p1.positions == (0, 3) and p1.tf == 2
    assert p3.positions == (4,)
    with pytest.raises(ValueError):
        build_index(docs(), layout="array", positions=True)


def test_guess_title():
    text = ":mod:`!secrets` --- Random numbers\n================================\n\nbody"
    assert guess_title(text, "x") == "secrets --- Random numbers"
    assert guess_title("no title here", "fallback") == "fallback"


# --- merges ----------------------------------------------------------------
def test_merges():
    a, b = [1, 3, 5, 7, 9], [2, 3, 4, 9, 10]
    assert intersect(a, b) == [3, 9]
    assert union(a, b) == [1, 2, 3, 4, 5, 7, 9, 10]
    assert difference(a, b) == [1, 5, 7]
    assert intersect([], b) == union([], []) == difference([], b) == []
    assert difference(a, []) == a


# --- queries ---------------------------------------------------------------
@pytest.mark.parametrize("engine", [MERGE, SET], ids=["merge", "set"])
def test_queries(index, engine):
    assert evaluate(index, "the cat", engine) == [0, 1]  # implicit AND
    assert evaluate(index, "the AND cat", engine) == [0, 1]
    assert evaluate(index, "cat OR привіт", engine) == [0, 1, 2]
    assert evaluate(index, "the NOT cat", engine) == [3]
    assert evaluate(index, "NOT the", engine) == [2]
    # Lab 3 precedence: world OR (dog AND NOT cat)
    assert evaluate(index, "world OR dog NOT cat", engine) == [0, 2, 3]
    assert evaluate(index, "(world OR dog) NOT cat", engine) == [2, 3]
    assert evaluate(index, "world OR (dog cat)", engine) == [0, 1, 2]
    assert evaluate(index, "HELLO", engine) == [0]  # normalized like the corpus
    assert evaluate(index, "zebra", engine) == []
    assert evaluate(index, "", engine) == []


def test_parse_lowercase_operators_are_words():
    assert parse("cats and dogs") == Term("cats") & Term("and") & Term("dogs")
    assert parse("state-of-the-art") == And(
        And(And(Term("state"), Term("of")), Term("the")), Term("art")
    )


# --- persistence -----------------------------------------------------------
@pytest.mark.parametrize("suffix", [".pkl", ".json", ".bin"])
def test_roundtrip(tmp_path, index, suffix):
    path = tmp_path / f"index{suffix}"
    store.save(index, path)
    for lazy in (False, True) if suffix == ".bin" else (False,):
        back = store.load(path, lazy=lazy)
        assert back.num_docs == index.num_docs
        assert back.doc_lengths == index.doc_lengths
        assert back.doc_meta == index.doc_meta
        assert set(back.postings) == set(index.postings)
        for term in index.postings:
            assert list(back.doc_ids(term)) == list(index.doc_ids(term))
            assert list(back.tfs(term)) == list(index.tfs(term))
        assert evaluate(back, "the NOT cat") == [3]


def test_roundtrip_positions_json(tmp_path):
    idx = build_index(docs(), positions=True)
    store.save(idx, tmp_path / "i.json")
    back = store.load(tmp_path / "i.json")
    assert back.postings["the"][1].positions == (0, 3)


def test_bin_rejects_foreign_file(tmp_path):
    bad = tmp_path / "x.bin"
    bad.write_bytes(pickle.dumps("not an index"))
    with pytest.raises(ValueError):
        store.load(bad)
