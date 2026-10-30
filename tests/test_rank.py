"""Lab 3: scorers, ranked search and snippets."""

import math
from pathlib import Path

import pytest

from findex.corpus import Document
from findex.index import Posting, build_index
from findex.rank import BM25, Scorer, SearchResult, TfIdf, search
from findex.snippets import iter_spans, make_snippet
from findex.tokenize import tokenize


def make_index(texts, positions=False):
    docs = (Document(f"d{i}", Path(f"d{i}"), t) for i, t in enumerate(texts))
    return build_index(docs, positions=positions)


FILLER = " ".join(f"w{i}" for i in range(200))


@pytest.fixture
def index():
    return make_index(
        [
            "common rare",  # 0
            "common words here",  # 1
            "common " + "repeat " * 20,  # 2
            "common repeat",  # 3
            "common needle " + FILLER,  # 4: long, one needle
            "common needle",  # 5: short, one needle
        ]
    )


def test_scorers_follow_the_protocol():
    assert isinstance(BM25(), Scorer) and isinstance(TfIdf(), Scorer)
    assert not isinstance(object(), Scorer)
    assert repr(BM25()) == "BM25(k1=1.5, b=0.75)"
    with pytest.raises(ValueError):
        BM25(b=2)


def test_tfidf_formula(index):
    p = Posting(2, 20)
    assert TfIdf().score("repeat", p, index) == pytest.approx((1 + math.log(20)) * math.log(6 / 2))
    assert TfIdf().score("common", Posting(0, 1), index) == 0  # in every document


def test_rare_beats_common(index):
    for scorer in (TfIdf(), BM25()):
        ranked = search(index, "common OR rare", scorer=scorer)
        assert ranked[0].doc_id == 0


def test_bm25_saturates(index):
    bm = BM25()
    s = [bm.tf_part(tf, 100, 100) for tf in (1, 2, 19, 20)]
    assert s[1] - s[0] > 10 * (s[3] - s[2])  # the 20th copy adds ~nothing
    assert s[3] < bm.k1 + 1  # bounded
    assert TfIdf().score("repeat", Posting(2, 20), index) > 3 * TfIdf().score(
        "repeat", Posting(3, 1), index
    )  # tf-idf keeps growing


def test_short_document_beats_long(index):
    ranked = search(index, "needle", scorer=BM25())
    assert [r.doc_id for r in ranked] == [5, 4]
    flat = search(index, "needle", scorer=BM25(b=0))  # no length normalization
    assert flat[0].score == pytest.approx(flat[1].score)


def test_search_results(index):
    results = search(index, "common", k=3)
    assert len(results) == 3
    assert results == sorted(results)  # best first; order=True on rank_key
    assert all(isinstance(r, SearchResult) for r in results)
    assert search(index, "zebra") == [] and search(index, "") == []
    assert search(index, "common", k=0) == []


def test_only_matched_documents_are_ranked(index):
    ranked = search(index, "common NOT rare NOT needle")
    assert {r.doc_id for r in ranked} == {1, 2, 3}
    everything_but = search(index, "NOT rare", k=10)  # pure NOT: all score 0
    assert {r.doc_id for r in everything_but} == {1, 2, 3, 4, 5}


def test_search_result_ordering():
    lo, hi, tie = SearchResult(1, 1.0, "x"), SearchResult(2, 5.0, "y"), SearchResult(0, 1.0, "z")
    assert sorted([lo, hi, tie]) == [hi, tie, lo]  # score desc, then doc_id asc
    assert hi < lo and min([lo, hi]) is hi


def test_index_search_method(index):
    assert index.search("rare") == search(index, "rare")


# --- snippets -------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Hello WORLD, it's Straße — café and п’ять м'ята!",
        "sys_path __init__ 3.13 state-of-the-art",
        "",
    ],
)
def test_spans_agree_with_tokenizer(text):
    assert [t for t, _, _ in iter_spans(text)] == list(tokenize(text))


def test_snippet_window_and_marks():
    text = "intro " * 40 + "the EVENT loop runs here " + "outro " * 40
    snip = make_snippet(text, {"event", "loop"}, width=30)
    assert "**EVENT** **loop**" in snip  # original case kept, normalized match
    assert snip.startswith("… ") and snip.endswith(" …")
    assert len(snip) < 30 * 2 + 40


def test_snippet_prefers_window_with_more_distinct_terms():
    text = "event " + "x " * 100 + "event and loop together " + "y " * 100
    snip = make_snippet(text, {"event", "loop"}, width=20, mark="brackets")
    assert "[event] and [loop]" in snip


def test_snippet_without_hits_shows_the_start():
    assert make_snippet("short text", {"zebra"}) == "short text"
