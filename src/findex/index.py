"""The inverted index: term -> doc-id-sorted postings, built in one streaming pass.

Build one with ``findex index data/ --out index.pkl --positions`` (see
``findex.cli``) or from Python with :func:`build_index`.

Three interchangeable postings *layouts* (the subject of the Lab 2 memory study):

* ``plain`` — ``list[PlainPosting]``, a ``@dataclass(frozen=True)`` without slots;
* ``slots`` — ``list[Posting]``, ``@dataclass(frozen=True, slots=True)`` (default);
* ``array`` — a pair of ``array('I')`` per term: ``(doc_ids, tfs)``, no objects.

doc_ids are dense ints ``0..N-1`` assigned in stream order. Each document's
postings are appended *after* all earlier documents', so every postings list
is sorted by doc_id by construction — no sort step needed.
"""

from __future__ import annotations

import logging
import re
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from findex.corpus import Document
from findex.merge import MERGE, Engine
from findex.timing import timed
from findex.tokenize import tokenize

if TYPE_CHECKING:
    from findex.query import Node
    from findex.rank import SearchResult

log = logging.getLogger(__name__)

# A plain assignment, not a PEP 695 `type` statement: typer reads this annotation
# at runtime to build CLI choices and doesn't understand `TypeAliasType` objects.
Layout = Literal["plain", "slots", "array"]
LAYOUTS: tuple[Layout, ...] = ("plain", "slots", "array")


class PostingLike(Protocol):
    """Anything that reads like a posting. Frozen dataclass fields are read-only,
    so they satisfy these read-only properties — no inheritance needed."""

    @property
    def doc_id(self) -> int: ...
    @property
    def tf(self) -> int: ...
    @property
    def positions(self) -> tuple[int, ...] | None: ...


@dataclass(frozen=True, slots=True)
class Posting:
    """One (document, term) pair. Frozen + slotted -> small and hashable."""

    doc_id: int
    tf: int
    positions: tuple[int, ...] | None = None  # token ordinals, only with --positions


@dataclass(frozen=True)
class PlainPosting:
    """The same record without __slots__ — kept only for the memory study."""

    doc_id: int
    tf: int
    positions: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class DocMeta:
    """What a result line shows: where the document is and what it's called."""

    doc_id: int
    path: str
    title: str


type IdArray = array[int]
type ArrayPostings = tuple[IdArray, IdArray]  # (doc_ids, tfs)
type PostingList = list[Posting] | list[PlainPosting] | ArrayPostings


class CacheStats(Protocol):
    @property
    def hits(self) -> int: ...


class MatchCache(Protocol):
    """The slice of ``functools.lru_cache``'s wrapper that ``Index`` uses."""

    def __call__(self, node: Node, engine: Engine, /) -> tuple[int, ...]: ...
    def cache_info(self) -> CacheStats: ...
    def cache_clear(self) -> None: ...


@runtime_checkable
class Closeable(Protocol):
    """A postings store holding an OS resource (the mmap of a lazy ``.bin``)."""

    def close(self) -> None: ...


class Index(Mapping[str, Sequence[PostingLike]]):
    """The inverted index as a read-only mapping ``term -> postings``.

    Inheriting from ``collections.abc.Mapping`` and defining the three abstract
    methods (``__getitem__``, ``__iter__``, ``__len__``) gives ``keys()``,
    ``items()``, ``values()``, ``get()`` and ``==`` for free. ``__contains__``
    is overridden only for speed (one dict lookup instead of ``try/except``).

    ``len(index)`` is the number of *terms*; ``index.num_docs`` the number of
    documents. An index with no terms is falsy (no ``__bool__`` -> ``__len__``).
    """

    def __init__(
        self,
        postings: Mapping[str, PostingList],
        doc_lengths: dict[int, int],
        doc_meta: dict[int, DocMeta],
        layout: Layout = "slots",
        has_positions: bool = False,
        meta: Mapping[str, object] | None = None,
    ) -> None:
        self.postings = postings
        self.doc_lengths = doc_lengths
        self.doc_meta = doc_meta
        self.layout: Layout = layout
        self.has_positions = has_positions
        self.meta: dict[str, object] = dict(meta or {})
        self.closed = False

    # --- the Mapping protocol ------------------------------------------------
    def __getitem__(self, term: str) -> Sequence[PostingLike]:
        plist = self.postings[term]  # KeyError for an unknown term, like a dict
        if isinstance(plist, tuple):  # array layout: rebuild records on demand
            ids, tfs = plist
            return [Posting(d, t) for d, t in zip(ids, tfs, strict=True)]
        return plist

    def __iter__(self) -> Iterator[str]:
        return iter(self.postings)

    def __len__(self) -> int:
        return len(self.postings)

    def __contains__(self, term: object) -> bool:
        return term in self.postings

    def __repr__(self) -> str:
        extra = ", positions" if self.has_positions else ""
        state = ", closed" if self.closed else ""
        return (
            f"Index(terms={len(self):,}, docs={self.num_docs:,}, "
            f"layout={self.layout!r}{extra}{state})"
        )

    # --- document statistics -------------------------------------------------
    @property
    def num_docs(self) -> int:
        return len(self.doc_meta)

    @cached_property
    def avg_doc_length(self) -> float:
        """Computed once on first access, then stored in the instance ``__dict__``
        (a non-data descriptor: the instance attribute shadows it afterwards)."""
        n = self.num_docs
        return sum(self.doc_lengths.values()) / n if n else 0.0

    @property
    def num_tokens(self) -> int:
        return sum(self.doc_lengths.values())

    def doc_length(self, doc_id: int) -> int:
        return self.doc_lengths[doc_id]

    def title(self, doc_id: int) -> str:
        return self.doc_meta[doc_id].title

    def collection_frequency(self, term: str) -> int:
        """Total occurrences of ``term`` in the corpus (sum of tf)."""
        return sum(self.tfs(term))

    # --- query support -------------------------------------------------------
    def match(self, node: Node, engine: Engine = MERGE) -> tuple[int, ...]:
        """Doc ids matching a parsed query (``findex.query`` node), cached.

        The cache is an ``lru_cache`` *per index instance* (see ``_match_cache``),
        keyed by the query tree — equal trees share an entry, so ``a b`` and
        ``a AND b`` hit the same slot. The result is a tuple: a cached list could
        be mutated by one caller and poison every later hit.
        """
        cache = self._match_cache
        hits_before = cache.cache_info().hits
        ids = cache(node, engine)
        outcome = "hit" if cache.cache_info().hits > hits_before else "miss"
        log.debug("match cache %s: %s -> %d docs", outcome, node, len(ids))
        return ids

    @cached_property
    def _match_cache(self) -> MatchCache:
        # Not ``@lru_cache`` on the method itself: that cache would live on the
        # class, key on ``self`` (Index is unhashable — Mapping defines __eq__)
        # and keep every index ever searched alive.
        @lru_cache(maxsize=256)
        def match_ids(node: Node, engine: Engine) -> tuple[int, ...]:
            return tuple(node.evaluate(self, engine))

        return match_ids

    def search(self, query: str | Node, k: int = 10) -> list[SearchResult]:
        """Ranked BM25 search; :func:`findex.rank.search` takes more options."""
        from findex.rank import search  # rank imports this module

        return search(self, query, k=k)

    # --- resources -----------------------------------------------------------
    def close(self) -> None:
        """Release what the index holds open (the mmap of a lazy ``.bin``) and
        drop caches. Idempotent; ``open_index`` calls it on exit."""
        if self.closed:
            return
        if isinstance(self.postings, Closeable):
            self.postings.close()
        cache: MatchCache | None = self.__dict__.pop("_match_cache", None)
        if cache is not None:
            cache.cache_clear()
        self.closed = True

    def __getstate__(self) -> dict[str, object]:
        # Caches are derived data and an lru_cache wrapper can't be pickled.
        state: dict[str, object] = dict(self.__dict__)
        state.pop("_match_cache", None)
        state.pop("avg_doc_length", None)
        return state

    # --- raw access used by the Boolean engines and the Lab 2 benchmarks ------
    def doc_ids(self, term: str) -> Sequence[int]:
        """Doc-id-sorted ids of documents containing ``term`` (empty if none)."""
        plist = self.postings.get(term)
        if plist is None:
            return ()
        if isinstance(plist, tuple):
            return plist[0]
        return [p.doc_id for p in plist]

    def tfs(self, term: str) -> Sequence[int]:
        plist = self.postings.get(term)
        if plist is None:
            return ()
        if isinstance(plist, tuple):
            return plist[1]
        return [p.tf for p in plist]

    def df(self, term: str) -> int:
        plist = self.postings.get(term)
        if plist is None:
            return 0
        return len(plist[0]) if isinstance(plist, tuple) else len(plist)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
_UNDERLINE = set("=-*~^#+`'\".:_")
# :mod:`!secrets`  /  :func:`~os.path <path>`  ->  secrets / ~os.path
_ROLE_RE = re.compile(r":[\w:.-]+:`[!~]?([^`<]+?)(?:\s*<[^>]*>)?`")


def guess_title(text: str, fallback: str) -> str:
    """First reST section title (a line followed by an underline), else fallback."""
    lines = text.split("\n", 80)[:80]  # only the head of the document
    for line, under in zip(lines, lines[1:], strict=False):
        title = line.strip()
        bar = under.strip()
        if (
            title
            and len(bar) >= 3
            and set(bar) <= _UNDERLINE
            and len(set(bar)) == 1
            and not title.startswith("..")
            and not set(title) <= _UNDERLINE
        ):
            return _ROLE_RE.sub(r"\1", title).replace("``", "").strip("*` ")
    return fallback


@timed
def build_index(
    docs: Iterable[Document], layout: Layout = "slots", positions: bool = False
) -> Index:
    """One pass over a *stream* of documents; nothing but the index is retained."""
    if layout == "array" and positions:
        raise ValueError("positions are only kept in the object layouts (plain/slots)")

    doc_lengths: dict[int, int] = {}
    doc_meta: dict[int, DocMeta] = {}
    arrays: defaultdict[str, ArrayPostings] = defaultdict(lambda: (array("I"), array("I")))
    slotted: defaultdict[str, list[Posting]] = defaultdict(list)
    plain: defaultdict[str, list[PlainPosting]] = defaultdict(list)

    for doc_id, doc in enumerate(docs):
        # Per-document accumulator, flushed into the global postings at the end
        # of this document and then dropped.
        if positions:
            where: defaultdict[str, list[int]] = defaultdict(list)
            n = -1
            for n, tok in enumerate(tokenize(doc.text)):
                where[tok].append(n)
            doc_lengths[doc_id] = n + 1
            for term, pos in where.items():
                if layout == "slots":
                    slotted[term].append(Posting(doc_id, len(pos), tuple(pos)))
                else:
                    plain[term].append(PlainPosting(doc_id, len(pos), tuple(pos)))
        else:
            counts = Counter(tokenize(doc.text))
            doc_lengths[doc_id] = counts.total()
            for term, tf in counts.items():
                if layout == "array":
                    ids, tfs = arrays[term]
                    ids.append(doc_id)
                    tfs.append(tf)
                elif layout == "slots":
                    slotted[term].append(Posting(doc_id, tf))
                else:
                    plain[term].append(PlainPosting(doc_id, tf))
        fallback = Path(doc.doc_id).stem
        doc_meta[doc_id] = DocMeta(doc_id, doc.doc_id, guess_title(doc.text, fallback))
        if (doc_id + 1) % 100 == 0:
            log.info("indexed %d documents", doc_id + 1)

    postings: dict[str, PostingList]
    if layout == "array":
        postings = dict(arrays)
    elif layout == "slots":
        postings = dict(slotted)
    else:
        postings = dict(plain)
    log.info("index built: %d documents, %d terms", len(doc_meta), len(postings))
    return Index(postings, doc_lengths, doc_meta, layout, positions)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError  # unreachable
