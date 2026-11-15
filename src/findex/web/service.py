"""The web layer's view of the engine: plain functions over the Lab 2–3 objects.

Everything here is synchronous, CPU-bound and framework-free — it takes an
:class:`~findex.index.Index`, returns dataclasses, and raises ``ValueError``
subclasses for bad input. The routes (``api.py``, ``ui.py``) turn those into
Pydantic models, HTML or HTTP errors; tests can call these functions directly.

:class:`IndexHolder` is the one piece of process state: the loaded index,
when it was loaded, and what went wrong if it didn't.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from findex.index import Index
from findex.modes import EmptyQuery, Semantic, SemanticUnavailable, run, snippet
from findex.query import Node, PhraseNeedsPositions, QuerySyntaxError, parse
from findex.rank import ScorerName, SearchResult
from findex.semantic import Embeddings, Mode, OnnxEmbedder
from findex.snippets import Part, document_text, iter_spans
from findex.store import load

log = logging.getLogger(__name__)

MAX_RESULTS = 1000  # page × k ceiling: deep pages cost a bigger top-k heap


class BadQuery(ValueError):
    """The query can't be run: syntax error, empty, or needs positions."""


@dataclass(slots=True)
class IndexHolder:
    """The index of this process, loaded once (in a thread, at startup)."""

    path: Path
    index: Index | None = None
    loaded_at: datetime | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    embeddings_path: Path | None = None
    model_dir: Path | None = None
    semantic: Semantic | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def load(self) -> None:
        t0 = time.perf_counter()
        try:
            index = load(self.path)
            semantic = self._load_semantic(index)
        except Exception as exc:  # reported by /health; the process keeps serving 503
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("could not load index %s", self.path)
            return
        with self._lock:
            self.index, self.semantic, self.loaded_at = index, semantic, datetime.now(UTC)
        log.info(
            "index %s loaded in %.2f s: %d documents, %d terms, semantic: %s",
            self.path, time.perf_counter() - t0, index.num_docs, len(index),
            f"{len(semantic.embeddings.vectors)} chunks" if semantic else "off",
        )  # fmt: skip

    def _load_semantic(self, index: Index) -> Semantic | None:
        if self.embeddings_path is None:
            return None
        emb = Embeddings.load(self.embeddings_path)
        # one thread: a query is ~10 tokens, and a small container has a fraction of a core
        model = OnnxEmbedder(self.model_dir or Path("models/all-MiniLM-L6-v2"), threads=1)
        semantic = Semantic(emb, model)
        semantic.check(index)
        return semantic

    @property
    def status(self) -> str:
        if self.index is not None:
            return "ok"
        return "failed" if self.error else "loading"

    def close(self) -> None:
        if self.index is not None:
            self.index.close()


def pretty_title(title: str) -> str:
    """reST titles for people: ``contextlib --- Utilities for with\\ -statement``."""
    return title.replace("\\ ", "").replace(" --- ", " — ").strip()


@dataclass(frozen=True, slots=True)
class Hit:
    rank: int
    result: SearchResult  # the Lab 3 dataclass, unchanged
    path: str
    snippet: list[Part]

    @property
    def title(self) -> str:
        return pretty_title(self.result.title)


@dataclass(frozen=True, slots=True)
class SearchPage:
    query: str
    mode: Mode
    scorer: ScorerName
    page: int
    k: int
    total: int
    took_ms: float
    hits: list[Hit]
    terms: frozenset[str]

    @property
    def pages(self) -> int:
        return math.ceil(self.total / self.k)


def parse_query(q: str) -> Node:
    try:
        node = parse(q)
    except QuerySyntaxError as exc:
        raise BadQuery(f"bad query: {exc.message} (at column {exc.pos + 1})") from None
    if node is None:
        raise BadQuery("empty query: no words to search for")
    return node


def run_search(
    index: Index,
    q: str,
    *,
    k: int = 10,
    page: int = 1,
    scorer: ScorerName = "bm25",
    mode: Mode = "lexical",
    semantic: Semantic | None = None,
    root: Path | None = None,
    snippets: bool = True,
) -> SearchPage:
    """Page ``page`` (1-based) of ``k`` results for ``q``, with snippets."""
    t0 = time.perf_counter()
    if not q.strip():
        raise BadQuery("empty query: no words to search for")
    window = min(page * k, MAX_RESULTS)
    try:
        outcome = run(index, q, mode=mode, scorer=scorer, k=window, semantic=semantic)
    except QuerySyntaxError as exc:
        raise BadQuery(f"bad query: {exc.message} (at column {exc.pos + 1})") from None
    except (EmptyQuery, SemanticUnavailable, PhraseNeedsPositions) as exc:
        raise BadQuery(str(exc)) from None
    hits: list[Hit] = []
    first = (page - 1) * k
    top = outcome.ranked.results
    for i, result in enumerate(top[first : first + k], start=first + 1):
        parts: list[Part] = []
        if snippets:
            parts = snippet(index, result.doc_id, outcome.terms, outcome.ranked, root)
        hits.append(Hit(i, result, index.doc_meta[result.doc_id].path, parts))
    took = (time.perf_counter() - t0) * 1000
    return SearchPage(q, mode, scorer, page, k, outcome.total, took, hits, outcome.terms)


@dataclass(frozen=True, slots=True)
class DocView:
    doc_id: int
    title: str
    path: str
    length: int
    text: str | None


def get_document(index: Index, doc_id: int, root: Path | None = None) -> DocView | None:
    meta = index.doc_meta.get(doc_id)
    if meta is None:
        return None
    text = document_text(index, doc_id, root)
    return DocView(doc_id, pretty_title(meta.title), meta.path, index.doc_length(doc_id), text)


def highlight(text: str, terms: frozenset[str]) -> list[Part]:
    """The whole text as ``(text, is_match)`` parts — for the document page."""
    if not terms:
        return [(text, False)]
    parts: list[Part] = []
    pos = 0
    for token, start, end in iter_spans(text):
        if token in terms and start >= pos:
            parts += [(text[pos:start], False), (text[start:end], True)]
            pos = end
    parts.append((text[pos:], False))
    return [p for p in parts if p[0]]
