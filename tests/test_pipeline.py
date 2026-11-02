"""The whole pipeline on the shared on-disk corpus from conftest.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TEXT_DOCS, doc_id_of

from findex import store
from findex.corpus import count_documents, iter_documents
from findex.index import Index
from findex.query import parse
from findex.rank import BM25, Scorer, TfIdf, search
from findex.snippets import document_text, make_snippet

SCORERS = [pytest.param(BM25(), id="bm25"), pytest.param(TfIdf(), id="tfidf")]


def test_reads_the_tree_and_skips_other_suffixes(corpus_dir: Path) -> None:
    paths = sorted(d.doc_id for d in iter_documents(corpus_dir))
    assert len(paths) == TEXT_DOCS == count_documents(corpus_dir)
    assert "sub/sorting.txt" in paths and not any(p.endswith(".bin") for p in paths)


def test_index_shape(corpus_index: Index) -> None:
    assert corpus_index.num_docs == TEXT_DOCS
    assert corpus_index.has_positions
    titles = {m.path: m.title for m in corpus_index.doc_meta.values()}
    assert titles["asyncio.md"] == "Asyncio"  # reST title
    assert titles["short.txt"] == "short"  # fallback: file stem
    for term in corpus_index:
        ids = list(corpus_index.doc_ids(term))
        assert ids == sorted(set(ids))


@pytest.mark.parametrize(
    ("term", "present"),
    [("café", True), ("strasse", True), ("привіт", True), ("п'ять", True), ("straße", False)],
)
def test_unicode_terms_are_normalized(corpus_index: Index, term: str, present: bool) -> None:
    assert (term in corpus_index) is present


def test_phrase_and_boolean(corpus_index: Index) -> None:
    def match(q: str) -> set[str]:
        node = parse(q)
        assert node is not None
        return {corpus_index.doc_meta[d].path for d in corpus_index.match(node)}

    assert match('"event loop"') == {"intro.txt", "asyncio.md"}
    assert match('"loop runs"') == {"intro.txt"}
    assert match('"runs loop"') == set()
    # punctuation is not a token: "event loop, event loop" contains "loop event"
    assert match('"loop event"') == {"asyncio.md"}
    assert match("needle NOT haystack") == {"short.txt"}
    assert match("(sort OR asyncio) NOT stable") == {"asyncio.md"}


# --- the three ranking checks from Lab 3, for both scorers ---------------------
@pytest.mark.parametrize("scorer", SCORERS)
def test_rare_term_outranks_common_term(corpus_index: Index, scorer: Scorer) -> None:
    # "event" is in 2 documents, "stable" in 1; both docs below have tf = 1.
    ranked = [r.doc_id for r in search(corpus_index, "event OR stable", scorer, k=10)]
    rare = doc_id_of(corpus_index, "sorting.txt")
    common = doc_id_of(corpus_index, "intro.txt")
    assert ranked.index(rare) < ranked.index(common)


@pytest.mark.parametrize(("scorer", "bounded"), [(BM25(), True), (TfIdf(), False)])
def test_repetition_saturates_only_in_bm25(
    corpus_index: Index, scorer: BM25 | TfIdf, bounded: bool
) -> None:
    from findex.index import Posting

    doc = doc_id_of(corpus_index, "asyncio.md")

    def s(tf: int) -> float:
        return scorer.score("event", Posting(doc, tf), corpus_index)

    first, twentieth = s(1), s(20) - s(19)
    assert twentieth < 0.1 * first  # both damp repetition...
    if bounded:  # ...but only BM25 has a ceiling
        assert s(1000) < scorer.idf("event", corpus_index) * (BM25().k1 + 1)
    else:
        assert s(1000) > 3 * s(1)


@pytest.mark.parametrize(("scorer", "short_wins"), [(BM25(), True), (TfIdf(), False)])
def test_short_document_beats_long_one(
    corpus_index: Index, scorer: Scorer, short_wins: bool
) -> None:
    ranked = search(corpus_index, "needle", scorer)
    short, long = doc_id_of(corpus_index, "short.txt"), doc_id_of(corpus_index, "long.txt")
    scores = {r.doc_id: r.score for r in ranked}
    if short_wins:
        assert scores[short] > scores[long]
    else:  # no length normalization: one occurrence is one occurrence
        assert scores[short] == pytest.approx(scores[long])


def test_bm25_without_length_normalization_ties(corpus_index: Index) -> None:
    ranked = search(corpus_index, "needle", BM25(b=0))
    assert ranked[0].score == pytest.approx(ranked[1].score)


# --- persistence ---------------------------------------------------------------
@pytest.mark.parametrize("suffix", [".pkl", ".json"])
def test_round_trip_keeps_positions(corpus_index: Index, tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"ix{suffix}"
    store.save(corpus_index, path)
    with store.open_index(path) as back:
        assert dict(back.items()) == dict(corpus_index.items())
        assert back.meta["root"] == corpus_index.meta["root"]
        assert [r.doc_id for r in search(back, '"event loop"')] == [
            r.doc_id for r in search(corpus_index, '"event loop"')
        ]


def test_snippet_from_the_corpus(corpus_index: Index) -> None:
    doc = doc_id_of(corpus_index, "unicode.rst")
    text = document_text(corpus_index, doc)
    assert text is not None
    assert "**Straße**" in make_snippet(text, {"strasse"})  # original spelling, marked
