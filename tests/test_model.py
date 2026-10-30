"""Lab 3: Index as a Python object, open_index, @timed, the match cache."""

import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from findex import store
from findex.corpus import Document
from findex.index import Index, Posting, build_index
from findex.query import parse
from findex.timing import timed

TEXTS = ["the cat sat on the mat", "the dog", "a cat and a dog and a cat"]


def docs():
    return (Document(f"d{i}.txt", Path(f"d{i}.txt"), t) for i, t in enumerate(TEXTS))


@pytest.fixture
def index():
    return build_index(docs(), positions=True)


def test_mapping_protocol(index):
    assert isinstance(index, Mapping)
    assert len(index) == 8  # terms, not documents
    assert "cat" in index and "zebra" not in index
    assert index["dog"] == [Posting(1, 1, (1,)), Posting(2, 1, (4,))]
    with pytest.raises(KeyError):
        index["zebra"]
    assert sorted(index) == sorted(index.keys())
    assert index.get("zebra") is None  # free from Mapping
    assert dict(index.items())["sat"] == [Posting(0, 1, (2,))]
    assert repr(index) == "Index(terms=8, docs=3, layout='slots', positions)"


def test_truthiness_comes_from_len():
    assert not Index({}, {}, {})
    assert Index({"a": []}, {}, {})


def test_document_statistics(index):
    assert index.num_docs == 3
    assert index.doc_length(2) == 8
    assert index.df("cat") == 2 and index.df("zebra") == 0
    assert "avg_doc_length" not in vars(index)
    assert index.avg_doc_length == pytest.approx((6 + 2 + 8) / 3)
    assert "avg_doc_length" in vars(index)  # cached_property stored the value


def test_array_layout_rebuilds_postings():
    ix = build_index(docs(), layout="array")
    assert ix["cat"] == [Posting(0, 1), Posting(2, 2)]


def test_open_index_closes_even_on_error(tmp_path, index):
    path = tmp_path / "ix.bin"
    store.save(index, path)
    with pytest.raises(RuntimeError), store.open_index(path, lazy=True) as ix:
        assert ix["cat"][0].doc_id == 0  # reads through the mmap
        captured = ix
        raise RuntimeError("boom")
    assert captured.closed and captured.postings.closed  # mmap released
    with pytest.raises(ValueError):  # and really unusable afterwards
        captured["cat"]


def test_open_index_return_inside_with(tmp_path, index):
    path = tmp_path / "ix.pkl"
    store.save(index, path)

    def first_title():
        with store.open_index(path) as ix:
            nonlocal opened
            opened = ix
            return ix.title(0)

    opened = None
    assert first_title() == "d0"
    assert opened.closed


def test_pickle_skips_caches(tmp_path, index):
    index.match(parse("cat"))
    _ = index.avg_doc_length
    store.save(index, tmp_path / "i.pkl")  # an lru_cache wrapper is not picklable
    back = store.load(tmp_path / "i.pkl")
    assert "_match_cache" not in vars(back) and "avg_doc_length" not in vars(back)
    assert back.match(parse("cat")) == (0, 2)


def test_match_cache_hit_is_logged(index, caplog):
    caplog.set_level(logging.INFO, logger="findex.index")
    assert index.match(parse("cat dog")) == (2,)
    assert index.match(parse("cat AND dog")) == (2,)  # same tree -> same entry
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0].startswith("match cache miss") and lines[1].startswith("match cache hit")
    assert index._match_cache.cache_info().hits == 1


def test_match_returns_immutable(index):
    assert isinstance(index.match(parse("cat")), tuple)


def test_timed_keeps_metadata_and_logs(caplog):
    caplog.set_level(logging.INFO, logger="findex.timed")

    @timed
    def work(x):
        """Doc."""
        return x * 2

    @timed(level=logging.WARNING)
    def loud():
        raise ValueError

    assert work(21) == 42
    assert (work.__name__, work.__doc__) == ("work", "Doc.")
    assert work.__wrapped__(1) == 2 and work.last_ms is not None
    with pytest.raises(ValueError):
        loud()
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert msgs[0][0] == logging.INFO and "work took" in msgs[0][1]
    assert msgs[1][0] == logging.WARNING and "loud took" in msgs[1][1]  # logged despite raise
