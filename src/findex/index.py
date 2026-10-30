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
import re
import time
import tracemalloc
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Literal

from findex.corpus import Document, iter_documents
from findex.tokenize import tokenize

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


@dataclass
class Index:
    postings: Mapping[str, object]  # list[Posting] | list[PlainPosting] | (array, array)
    doc_lengths: dict[int, int]
    doc_meta: dict[int, DocMeta]
    layout: Layout = "slots"
    has_positions: bool = False
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def n_docs(self) -> int:
        return len(self.doc_meta)

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
    args = p.parse_args(argv)

    tracemalloc.start()
    t0 = time.perf_counter()
    index = build_index(islice(iter_documents(args.root), args.limit), args.layout, args.positions)
    t_build = time.perf_counter() - t0
    _, peak_build = tracemalloc.get_traced_memory()

    t0 = time.perf_counter()
    fmt = store.save(index, args.out, args.format)
    t_save = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    n_postings = sum(index.df(t) for t in index.postings)
    print(f"documents:     {index.n_docs:,}")
    print(f"terms:         {len(index.postings):,}")
    print(f"postings:      {n_postings:,}")
    print(f"layout:        {index.layout}{' + positions' if index.has_positions else ''}")
    print(f"build:         {t_build:.2f} s   (peak {fmt_bytes(peak_build)})")
    print(f"save [{fmt}]:  {t_save:.2f} s -> {args.out} ({fmt_bytes(args.out.stat().st_size)})")
    print(f"peak memory:   {fmt_bytes(peak)}")


if __name__ == "__main__":
    main()
