"""The inverted index: term -> doc-id-sorted postings, built in one streaming pass.

Usage::

    python -m findex.index data/ --out index.bin              # build + save
    python -m findex.index data/ --out index.pkl --positions  # keep token positions
    python -m findex.index data/ --out index.pkl --layout array

Three interchangeable postings *layouts* (the subject of the memory study):

* ``plain`` — ``list[PlainPosting]``, a ``@dataclass(frozen=True)`` without slots;
* ``slots`` — ``list[Posting]``, ``@dataclass(frozen=True, slots=True)`` (default);
* ``array`` — a pair of ``array('I')`` per term: ``(doc_ids, tfs)``, no objects.

doc_ids are dense ints ``0..N-1`` assigned in stream order. Each document's
postings are appended *after* all earlier documents', so every postings list
is sorted by doc_id by construction — no sort step needed.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import tracemalloc
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property, lru_cache
from itertools import islice
from pathlib import Path
from typing import Literal

from findex.corpus import Document, iter_documents
from findex.merge import MERGE, Engine
from findex.timing import timed
from findex.tokenize import tokenize

log = logging.getLogger(__name__)

Layout = Literal["plain", "slots", "array"]
LAYOUTS: tuple[Layout, ...] = ("plain", "slots", "array")


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


class Index(Mapping[str, Sequence[Posting]]):
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
        postings: Mapping[str, object],  # list[Posting] | list[PlainPosting] | (array, array)
        doc_lengths: dict[int, int],
        doc_meta: dict[int, DocMeta],
        layout: Layout = "slots",
        has_positions: bool = False,
        meta: dict[str, object] | None = None,
    ) -> None:
        self.postings = postings
        self.doc_lengths = doc_lengths
        self.doc_meta = doc_meta
        self.layout = layout
        self.has_positions = has_positions
        self.meta: dict[str, object] = dict(meta or {})
        self.closed = False

    # --- the Mapping protocol ------------------------------------------------
    def __getitem__(self, term: str) -> Sequence[Posting]:
        plist = self.postings[term]  # KeyError for an unknown term, like a dict
        if self.layout == "array":  # rebuild records on demand
            ids, tfs = plist  # type: ignore[misc]
            return [Posting(d, t) for d, t in zip(ids, tfs, strict=True)]
        return plist  # type: ignore[return-value]

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

    def doc_length(self, doc_id: int) -> int:
        return self.doc_lengths[doc_id]

    def title(self, doc_id: int) -> str:
        return self.doc_meta[doc_id].title

    # --- query support -------------------------------------------------------
    def match(self, node, engine: Engine = MERGE) -> tuple[int, ...]:
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
        log.info("match cache %s: %s -> %d docs", outcome, node, len(ids))
        return ids

    @cached_property
    def _match_cache(self):
        # Not ``@lru_cache`` on the method itself: that cache would live on the
        # class, key on ``self`` (Index is unhashable — Mapping defines __eq__)
        # and keep every index ever searched alive.
        @lru_cache(maxsize=256)
        def match_ids(node, engine: Engine) -> tuple[int, ...]:
            return tuple(node.evaluate(self, engine))

        return match_ids

    def search(self, query, **kwargs):
        """Ranked search; see :func:`findex.rank.search` for the arguments."""
        from findex.rank import search  # rank imports this module

        return search(self, query, **kwargs)

    # --- resources -----------------------------------------------------------
    def close(self) -> None:
        """Release what the index holds open (the mmap of a lazy ``.bin``) and
        drop caches. Idempotent; ``open_index`` calls it on exit."""
        if self.closed:
            return
        closer = getattr(self.postings, "close", None)
        if closer is not None:
            closer()
        cache = self.__dict__.pop("_match_cache", None)
        if cache is not None:
            cache.cache_clear()
        self.closed = True

    def __getstate__(self) -> dict:
        # Caches are derived data and an lru_cache wrapper can't be pickled.
        state = dict(self.__dict__)
        state.pop("_match_cache", None)
        state.pop("avg_doc_length", None)
        return state

    # --- raw access used by the Boolean engines and the Lab 2 benchmarks ------
    def doc_ids(self, term: str) -> Sequence[int]:
        """Doc-id-sorted ids of documents containing ``term`` (empty if none)."""
        plist = self.postings.get(term)
        if plist is None:
            return ()
        if self.layout == "array":
            return plist[0]  # type: ignore[index]
        return [p.doc_id for p in plist]  # type: ignore[attr-defined]

    def tfs(self, term: str) -> Sequence[int]:
        plist = self.postings.get(term)
        if plist is None:
            return ()
        if self.layout == "array":
            return plist[1]  # type: ignore[index]
        return [p.tf for p in plist]  # type: ignore[attr-defined]

    def df(self, term: str) -> int:
        plist = self.postings.get(term)
        if plist is None:
            return 0
        return len(plist[0] if self.layout == "array" else plist)  # type: ignore[arg-type,index]


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
_UNDERLINE = set("=-*~^#+`'\".:_")


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


# :mod:`!secrets`  /  :func:`~os.path <path>`  ->  secrets / ~os.path
_ROLE_RE = re.compile(r":[\w:.-]+:`[!~]?([^`<]+?)(?:\s*<[^>]*>)?`")


@timed
def build_index(
    docs: Iterable[Document], layout: Layout = "slots", positions: bool = False
) -> Index:
    """One pass over a *stream* of documents; nothing but the index is retained."""
    if layout == "array" and positions:
        raise ValueError("positions are only kept in the object layouts (plain/slots)")

    doc_lengths: dict[int, int] = {}
    doc_meta: dict[int, DocMeta] = {}

    if layout == "array":
        arrays: defaultdict[str, tuple[array, array]] = defaultdict(
            lambda: (array("I"), array("I"))
        )
    else:
        lists: defaultdict[str, list] = defaultdict(list)
        cls = Posting if layout == "slots" else PlainPosting

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
                lists[term].append(cls(doc_id, len(pos), tuple(pos)))
        else:
            counts = Counter(tokenize(doc.text))
            doc_lengths[doc_id] = counts.total()
            if layout == "array":
                for term, tf in counts.items():
                    ids, tfs = arrays[term]
                    ids.append(doc_id)
                    tfs.append(tf)
            else:
                for term, tf in counts.items():
                    lists[term].append(cls(doc_id, tf))
        fallback = Path(doc.doc_id).stem
        doc_meta[doc_id] = DocMeta(doc_id, doc.doc_id, guess_title(doc.text, fallback))

    postings = dict(arrays) if layout == "array" else dict(lists)
    return Index(postings, doc_lengths, doc_meta, layout, positions)


def setup_logging(quiet: bool = False) -> None:
    """CLI logging: ``@timed`` and cache lines go to stderr, results to stdout."""
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="[%(name)s] %(message)s",
    )


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError  # unreachable


def main(argv: list[str] | None = None) -> None:
    from findex import store  # local import: store imports this module

    p = argparse.ArgumentParser(prog="python -m findex.index", description=__doc__)
    p.add_argument("root", type=Path, help="corpus directory or .jsonl file")
    p.add_argument("--out", type=Path, required=True, help="index file (.bin/.pkl/.json)")
    p.add_argument("--layout", choices=LAYOUTS, default="slots")
    p.add_argument("--positions", action="store_true", help="store token positions")
    p.add_argument("--format", choices=store.FORMATS, default=None, help="default: by suffix")
    p.add_argument("--limit", type=int, default=None, help="first N documents only")
    p.add_argument("-q", "--quiet", action="store_true", help="no @timed log lines")
    args = p.parse_args(argv)
    setup_logging(args.quiet)

    tracemalloc.start()
    t0 = time.perf_counter()
    index = build_index(islice(iter_documents(args.root), args.limit), args.layout, args.positions)
    index.meta["root"] = str(args.root)  # lets search find the texts again for snippets
    t_build = time.perf_counter() - t0
    _, peak_build = tracemalloc.get_traced_memory()

    t0 = time.perf_counter()
    fmt = store.save(index, args.out, args.format)
    t_save = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    n_postings = sum(index.df(t) for t in index)
    print(repr(index))
    print(f"documents:     {index.num_docs:,}")
    print(f"terms:         {len(index):,}")
    print(f"postings:      {n_postings:,}")
    print(f"layout:        {index.layout}{' + positions' if index.has_positions else ''}")
    print(f"build:         {t_build:.2f} s   (peak {fmt_bytes(peak_build)})")
    print(f"save [{fmt}]:  {t_save:.2f} s -> {args.out} ({fmt_bytes(args.out.stat().st_size)})")
    print(f"peak memory:   {fmt_bytes(peak)}")


if __name__ == "__main__":
    # `python -m findex.index` runs this file as the module `__main__`, so the
    # classes defined here would be `__main__.Index`, `__main__.Posting` — and a
    # pickle written now couldn't be loaded by any other program. Run the
    # importable module's main() instead, so pickles say `findex.index.Index`.
    from findex import index as _importable

    _importable.main()
