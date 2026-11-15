"""Lab 8: chunking, embeddings store, best-chunk-per-document, RRF, the three modes.

The model itself is replaced by ``HashingEmbedder`` (hashed bag of words), so
these tests need no 90 MB download; one test runs the real model when
``models/all-MiniLM-L6-v2`` is present and checks it against the reference
output of ``sentence-transformers``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from findex.index import Index
from findex.modes import EmptyQuery, Semantic, SemanticUnavailable, run, snippet
from findex.npindex import to_numpy
from findex.semantic import (
    MODEL_FILES,
    Embeddings,
    HashingEmbedder,
    check_model,
    chunk_spans,
    clean_rst,
    embed_index,
    query_text,
    rrf,
    semantic_rank,
)
from findex.web.app import create_app
from findex.web.deps import get_holder
from findex.web.service import IndexHolder
from findex.web.settings import Settings

MODEL_DIR = Path("models/all-MiniLM-L6-v2")


@pytest.fixture
def semantic(corpus_index: Index) -> Semantic:
    embedder = HashingEmbedder(dim=128)
    return Semantic(embed_index(corpus_index, embedder, max_words=8), embedder)


# --- chunks and the store ------------------------------------------------------------------
def test_chunks_cover_the_text_in_runs_of_words() -> None:
    text = "one two  three\nfour five six seven"
    spans = chunk_spans(text, max_words=3)
    assert [text[s:e] for s, e in spans] == ["one two  three", "four five six", "seven"]
    assert chunk_spans("", 5) == []


def test_clean_rst_keeps_the_words() -> None:
    raw = ".. _label:\n\nTitle\n=====\n\nUse :func:`asyncio.run` and ``await`` **now**.\n.. note::"
    assert clean_rst(raw) == "Title Use asyncio.run and await now."
    assert query_text('"event loop" AND (cancel OR NOT timeout)') == "event loop cancel timeout"


def test_embeddings_roundtrip_and_layout(
    corpus_index: Index, semantic: Semantic, tmp_path: Path
) -> None:
    emb = semantic.embeddings
    assert emb.vectors.dtype == np.float32 and emb.vectors.shape[1] == 128
    assert np.allclose(np.linalg.norm(emb.vectors, axis=1), 1.0, atol=1e-5)  # unit rows
    assert np.all(np.diff(emb.chunk_doc) >= 0) and emb.first[-1] == len(emb.vectors)
    long_doc = max(range(corpus_index.num_docs), key=corpus_index.doc_length)
    assert emb.first[long_doc + 1] - emb.first[long_doc] > 10  # "filler" × 300: many chunks
    path = tmp_path / "e.npy"
    emb.save(path)
    assert (tmp_path / "e.chunks.npy").is_file()
    meta = json.loads((tmp_path / "e.json").read_text())
    assert meta["num_docs"] == corpus_index.num_docs and meta["model"] == "hashing-test-embedder"
    again = Embeddings.load(path)
    assert np.array_equal(again.vectors, emb.vectors) and np.array_equal(again.first, emb.first)


def test_a_document_scores_its_best_chunk(semantic: Semantic) -> None:
    emb = semantic.embeddings
    q = semantic.embedder.encode(["needle haystack"])[0]
    best, rows = emb.doc_scores(q)
    sims = emb.vectors @ q
    for d in range(emb.num_docs):
        chunk = sims[emb.first[d] : emb.first[d + 1]]
        if chunk.size:
            assert best[d] == chunk.max() and rows[d] == emb.first[d] + int(chunk.argmax())


def test_semantic_rank_finds_the_words_and_points_at_the_chunk(
    corpus_index: Index, semantic: Semantic
) -> None:
    ranked = semantic_rank(
        corpus_index, semantic.embeddings, semantic.embedder, "needle haystack", 3
    )
    by_path = {corpus_index.doc_meta[r.doc_id].path: r.doc_id for r in ranked.results}
    assert {"short.txt", "sub/long.txt"} <= set(by_path)  # the two pages with "needle"
    start, end = ranked.spans[by_path["sub/long.txt"]]
    text = (Path(str(corpus_index.meta["root"])) / "sub/long.txt").read_text()
    assert "needle" in text[start:end] and start > 0


# --- fusion -----------------------------------------------------------------------------------
def test_rrf_rewards_agreement_and_breaks_ties_by_id() -> None:
    fused = rrf([[1, 2, 3], [3, 1, 4]])
    assert [d for d, _ in fused] == [1, 3, 2, 4]  # 1: 1/61+1/62 > 3: 1/63+1/61
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert [d for d, _ in rrf([[5], [6]])] == [5, 6]  # equal scores: lower id first


# --- the modes --------------------------------------------------------------------------------
def test_three_modes(corpus_index: Index, semantic: Semantic) -> None:
    nx = to_numpy(corpus_index)
    lex = run(nx, '"event loop"', mode="lexical", k=3)
    assert lex.total == 2 and lex.terms == {"event", "loop"} and not lex.ranked.spans
    sem = run(nx, "stable sort key", mode="semantic", k=3, semantic=semantic)
    assert nx.doc_meta[sem.ranked.results[0].doc_id].path == "sub/sorting.txt"
    hyb = run(nx, "event loop", mode="hybrid", k=3, semantic=semantic)
    assert nx.doc_meta[hyb.ranked.results[0].doc_id].path == "asyncio.md"  # first in both lists
    parts = snippet(nx, sem.ranked.results[0].doc_id, sem.terms, sem.ranked)
    assert any(hit and text == "stable" for text, hit in parts)


def test_mode_errors(corpus_index: Index, semantic: Semantic) -> None:
    with pytest.raises(SemanticUnavailable):
        run(corpus_index, "loop", mode="semantic")
    with pytest.raises(EmptyQuery):
        run(corpus_index, "AND", mode="semantic", semantic=semantic)  # nothing left to embed
    with pytest.raises(EmptyQuery):
        run(corpus_index, "", mode="lexical")
    other = Semantic(semantic.embeddings, semantic.embedder)
    object.__setattr__(other.embeddings, "num_docs", 99)
    with pytest.raises(ValueError, match="run `findex embed`"):
        other.check(corpus_index)


# --- the web app ------------------------------------------------------------------------------
def client_with(index: Index, semantic: Semantic | None) -> TestClient:
    app = create_app(Settings(index_path=Path("none.pkl")))
    holder = IndexHolder(Path("fixture"), index=index, semantic=semantic)

    async def fixture_holder() -> IndexHolder:
        return holder

    from findex.web.deps import get_index

    app.dependency_overrides[get_holder] = fixture_holder
    app.dependency_overrides[get_index] = lambda: index
    return TestClient(app)


def test_api_and_page_modes(corpus_index: Index, semantic: Semantic) -> None:
    c = client_with(to_numpy(corpus_index), semantic)
    r = c.get("/search", params={"q": "stable sort key", "mode": "semantic", "k": 2})
    assert r.status_code == 200 and r.json()["mode"] == "semantic"
    assert r.json()["results"][0]["path"] == "sub/sorting.txt"
    assert c.get("/search", params={"q": "loop", "mode": "hybrid"}).json()["mode"] == "hybrid"
    assert c.get("/search", params={"q": "loop", "mode": "fuzzy"}).status_code == 422
    assert c.get("/health").json()["semantic"] is True
    assert c.get("/stats").json()["semantic_chunks"] == len(semantic.embeddings.vectors)
    page = c.get("/", params={"q": "stable sort"}).text
    assert 'id="mode"' in page and 'value="hybrid" selected' in page  # hybrid by default


def test_semantic_mode_without_embeddings_is_a_400(corpus_index: Index) -> None:
    c = client_with(corpus_index, None)
    r = c.get("/search", params={"q": "loop", "mode": "semantic"})
    assert r.status_code == 400 and "not available" in r.json()["detail"]
    assert 'id="mode"' not in c.get("/").text  # no toggle when it can't work


def test_lifespan_refuses_a_missing_embeddings_file(corpus_index: Index, tmp_path: Path) -> None:
    from findex.store import save

    save(corpus_index, tmp_path / "ix.pkl")
    app = create_app(Settings(index_path=tmp_path / "ix.pkl", embeddings_path=tmp_path / "e.npy"))
    with pytest.raises(RuntimeError, match="EMBEDDINGS_PATH"), TestClient(app):
        pass


# --- the real model ---------------------------------------------------------------------------
def test_check_model_rejects_other_bytes(tmp_path: Path) -> None:
    for name in MODEL_FILES:
        (tmp_path / name).write_bytes(b"not the model")
    with pytest.raises(ValueError, match="is not the pinned"):
        check_model(tmp_path)
    with pytest.raises(FileNotFoundError):
        check_model(tmp_path / "nowhere")


@pytest.mark.slow
@pytest.mark.skipif(not (MODEL_DIR / "model.onnx").is_file(), reason="model not downloaded")
def test_onnx_model_matches_sentence_transformers() -> None:
    from findex.semantic import OnnxEmbedder

    v = OnnxEmbedder(MODEL_DIR, threads=1).encode(["This is an example sentence"])[0]
    # sentence-transformers' documented output for all-MiniLM-L6-v2
    assert v[:3] == pytest.approx([0.0676569, 0.0634959, 0.0487131], abs=1e-5)
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-6)
