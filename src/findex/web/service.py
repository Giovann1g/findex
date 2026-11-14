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
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from findex.index import Index
from findex.merge import MERGE
from findex.query import Node, PhraseNeedsPositions, QuerySyntaxError, parse
from findex.rank import ScorerName, SearchResult, make_scorer, search
from findex.snippets import Part, document_text, iter_spans, snippet_parts
from findex.store import load

log = logging.getLogger(__name__)

MAX_RESULTS = 1000  # page × k ceiling: deep pages cost a bigger top-k heap


# reStructuredText markup that reads as noise in a one-line snippet
_RST_NOISE = [
    (re.compile(r"\.\. [\w:-]+::|\.\. _[\w.-]+:"), " "),  # .. directive::  /  .. _label:
    (re.compile(r"\.\. _?$"), " "),  # a label cut in two by a highlighted word
    (re.compile(r":[A-Za-z][\w:+.-]*:(?=`)"), ""),  # :func:`x`  ->  `x`
    (re.compile(r"`+_{0,2}"), ""),  # `x`  ``x``  `link`_
    (re.compile(r"\*\*|(?<![\w*])\*(?=\w)|(?<=\w)\*(?![\w*])"), ""),  # **bold**  *em*
    (re.compile(r"([=~^\-#*])\1{2,}"), " "),  # ===== section underlines
]


def _join_plain(parts: list[Part]) -> list[Part]:
    """Merge neighbouring non-match parts, so markup split across them is whole."""
    out: list[Part] = []
    for text, hit in parts:
        if out and not hit and not out[-1][1]:
            out[-1] = (out[-1][0] + text, False)
        else:
            out.append((text, hit))
    return out


def tidy_rst(parts: list[Part]) -> list[Part]:
    """Snippet parts without reST markup, for people rather than parsers."""
    # a matched word that is a role name, ":class:`x`", is markup, not a match
    unrolled: list[Part] = []
    for i, (text, hit) in enumerate(parts):
        nxt = parts[i + 1][0] if i + 1 < len(parts) else ""
        if hit and unrolled and unrolled[-1][0].endswith(":") and re.match(r":`", nxt):
            prev, _ = unrolled.pop()
            unrolled.append((prev + text, False))  # removed below with the rest of the role
            continue
        unrolled.append((text, hit))
    out: list[Part] = []
    for text, hit in _join_plain(unrolled):
        for pattern, repl in _RST_NOISE:
            text = pattern.sub(repl, text)
        text = re.sub(r"\s+", " ", text)
        if text.strip() or (text and out):
            out.append((text, hit))
    if out:
        out[0] = (out[0][0].lstrip(), out[0][1])
        out[-1] = (out[-1][0].rstrip(), out[-1][1])
    merged: list[Part] = []
    for text, hit in out:  # "… " + "  text" → one space
        if merged and merged[-1][0].endswith(" ") and text.startswith(" "):
            text = text.lstrip()
        if text:
            merged.append((text, hit))
    return merged


def fast_snippet(text: str, terms: frozenset[str], width: int = 80) -> list[Part]:
    """``snippet_parts``, but without tokenizing the whole document first.

    ``snippet_parts`` normalizes every word of the text to find the matches —
    about 99 % of a search request's CPU on long pages (a 200 KB page: 20 ms).
    Here one case-insensitive regex finds the candidate places, the densest
    window (most distinct terms within ``width``) is cut out, and only that
    window goes through ``snippet_parts``. When the regex finds nothing (a
    match only after Unicode normalization, or a NOT-only query) it falls
    back to the exact, slow path.
    """
    if not terms:
        return snippet_parts(text, terms, width)
    alternatives = "|".join(map(re.escape, sorted(terms, key=len, reverse=True)))
    # not inside a reST role name (":class:`…`"): those "matches" read as noise
    pattern = re.compile(rf"(?<![\w:])(?:{alternatives})(?![\w:])", re.IGNORECASE)
    hits = [(m.start(), m.end(), m.group().casefold()) for m in pattern.finditer(text)]
    if not hits:
        return snippet_parts(text, terms, width)
    best, best_key, lo_i = hits[0], (0, 0), 0
    for i, (start, _end, _term) in enumerate(hits):  # sliding window over the hits
        while hits[lo_i][0] < start - width:
            lo_i += 1
        hi_i = i
        while hi_i + 1 < len(hits) and hits[hi_i + 1][0] <= start + width:
            hi_i += 1
        key = (len({t for _, _, t in hits[lo_i : hi_i + 1]}), hi_i - lo_i)
        if key > best_key:
            best, best_key = hits[i], key
    lo = max(0, best[0] - 3 * width)
    hi = min(len(text), best[1] + 3 * width)
    parts = snippet_parts(text[lo:hi], terms, width)
    if lo > 0 and parts[:1] != [("… ", False)]:
        parts.insert(0, ("… ", False))
    if hi < len(text) and parts[-1:] != [(" …", False)]:
        parts.append((" …", False))
    return parts


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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def load(self) -> None:
        t0 = time.perf_counter()
        try:
            index = load(self.path)
        except Exception as exc:  # reported by /health; the process keeps serving 503
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("could not load index %s", self.path)
            return
        with self._lock:
            self.index, self.loaded_at = index, datetime.now(UTC)
        log.info(
            "index %s loaded in %.2f s: %d documents, %d terms",
            self.path, time.perf_counter() - t0, index.num_docs, len(index),
        )  # fmt: skip

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
    root: Path | None = None,
    snippets: bool = True,
) -> SearchPage:
    """Page ``page`` (1-based) of ``k`` results for ``q``, with snippets."""
    t0 = time.perf_counter()
    node = parse_query(q)
    window = min(page * k, MAX_RESULTS)
    try:
        total = len(index.match(node, MERGE))
        top = search(index, node, scorer=make_scorer(scorer), k=window)
    except PhraseNeedsPositions as exc:
        raise BadQuery(str(exc)) from None
    terms = frozenset(node.terms())
    hits: list[Hit] = []
    first = (page - 1) * k
    for i, result in enumerate(top[first : first + k], start=first + 1):
        parts: list[Part] = []
        if snippets:
            text = document_text(index, result.doc_id, root)
            parts = tidy_rst(fast_snippet(text, terms)) if text is not None else []
        hits.append(Hit(i, result, index.doc_meta[result.doc_id].path, parts))
    took = (time.perf_counter() - t0) * 1000
    return SearchPage(q, scorer, page, k, total, took, hits, terms)


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
