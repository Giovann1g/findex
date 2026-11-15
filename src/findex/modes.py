"""One entry point for the three search modes, shared by the CLI and the web app.

* ``lexical``  — the Boolean query decides which documents match, BM25 or
  TF-IDF ranks them (NumPy scorer for the ``numpy`` layout, Lab 3's otherwise);
* ``semantic`` — the query's words are embedded and every document is ranked
  by its best chunk's cosine; Boolean syntax is ignored;
* ``hybrid``   — both rankings (top 100 each) fused with Reciprocal Rank Fusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from findex.index import Index
from findex.merge import MERGE
from findex.npindex import NUMPY
from findex.query import Node, QuerySyntaxError, parse
from findex.rank import ScorerName, SearchResult, make_scorer, search
from findex.semantic import Embedder, Embeddings, Mode, Ranked, hybrid_rank, semantic_rank
from findex.snippets import Part, document_text, fast_snippet, snippet_parts, tidy_rst
from findex.vecrank import search_np

FUSION_DEPTH = 100  # how deep each list goes into RRF


class SemanticUnavailable(ValueError):
    """mode=semantic/hybrid, but no embeddings or model are loaded."""


class EmptyQuery(ValueError):
    """Nothing to search for."""


@dataclass(frozen=True, slots=True)
class Semantic:
    """What semantic search needs at query time: the matrix and the model."""

    embeddings: Embeddings
    embedder: Embedder

    def check(self, index: Index) -> None:
        if self.embeddings.num_docs != index.num_docs:
            raise ValueError(
                f"embeddings are for {self.embeddings.num_docs} documents, the index has "
                f"{index.num_docs}: run `findex embed` on this index"
            )


@dataclass(frozen=True, slots=True)
class Outcome:
    ranked: Ranked
    total: int  # documents that qualify (lexical: Boolean matches; else: candidates)
    terms: frozenset[str]  # words to highlight


def lexical(index: Index, node: Node, scorer: ScorerName, k: int) -> list[SearchResult]:
    if index.layout == "numpy":
        return search_np(index, node, scorer=scorer, k=k)
    return search(index, node, scorer=make_scorer(scorer), k=k)


def run(
    index: Index,
    query: str,
    *,
    mode: Mode = "lexical",
    scorer: ScorerName = "bm25",
    k: int = 10,
    semantic: Semantic | None = None,
) -> Outcome:
    """Rank ``query`` in ``mode``; raises ``QuerySyntaxError``, ``EmptyQuery``,
    ``SemanticUnavailable`` or ``PhraseNeedsPositions`` for bad requests."""
    if mode == "lexical":
        node = parse(query)
        if node is None:
            raise EmptyQuery("empty query: no words to search for")
        total = len(index.match(node, NUMPY if index.layout == "numpy" else MERGE))
        return Outcome(Ranked(lexical(index, node, scorer, k), {}), total, node.terms())

    if semantic is None:
        raise SemanticUnavailable("semantic search is not available: no embeddings loaded")
    try:
        node = parse(query)
    except QuerySyntaxError:
        node = None  # half-typed syntax is fine for the model: it only reads the words
    terms = node.terms() if node is not None else frozenset[str]()
    if mode == "semantic":
        ranked = semantic_rank(index, semantic.embeddings, semantic.embedder, query, k)
        if not ranked.results:
            raise EmptyQuery("empty query: no words to search for")
        return Outcome(ranked, semantic.embeddings.num_docs, terms)

    depth = max(k, FUSION_DEPTH)
    sem = semantic_rank(index, semantic.embeddings, semantic.embedder, query, depth)
    lex = lexical(index, node, scorer, depth) if node is not None else []
    if not sem.results and not lex:
        raise EmptyQuery("empty query: no words to search for")
    fused = hybrid_rank(index, lex, sem, depth)
    return Outcome(Ranked(fused.results[:k], fused.spans), len(fused.results), terms)


def snippet(
    index: Index, doc_id: int, terms: frozenset[str], ranked: Ranked, root: Path | None = None,
    *, tidy: bool = True,
) -> list[Part]:  # fmt: skip
    """The snippet for a hit: around the query's words, or — for a semantic hit
    whose best chunk has none of them — the start of that best chunk."""
    text = document_text(index, doc_id, root)
    if text is None:
        return []
    span = ranked.spans.get(doc_id)
    if span is not None:
        piece = text[span[0] : span[1]]
        parts = fast_snippet(piece, terms) if terms else snippet_parts(piece, terms)
        if span[0] > 0 and parts[:1] != [("… ", False)]:
            parts.insert(0, ("… ", False))
    else:
        parts = fast_snippet(text, terms)
    return tidy_rst(parts) if tidy else parts
