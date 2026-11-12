"""Parallel indexing: ``build_partial`` per chunk of files, then one ``merge``.

The work is split by **file paths**, never by loaded texts: a worker gets a
list of paths, reads and tokenizes them itself, and sends back a compact
:class:`PartialIndex` (``array('I')`` postings, which pickle as raw bytes).
Shipping the texts instead would pickle the whole corpus through a pipe.

Every chunk gets its ``doc_id`` range *up front* (``first_doc_id`` = number
of files before it), so ids are unique across the corpus without any
coordination, and :func:`merge` only has to concatenate per-term postings in
chunk order — they come out sorted by doc id with no sort step.

The same ``build_partial`` and ``merge`` run under three executors::

    serial     a plain loop in this thread          (the baseline)
    threads    ThreadPoolExecutor(workers)          (shares memory; the GIL decides)
    processes  ProcessPoolExecutor(workers, spawn)  (one interpreter per worker)

``merge([build_partial(all_paths)])`` produces exactly the index
:func:`findex.index.build_index` does (tested), so all paths are equivalent.

Spawn safety: worker processes are started with the ``spawn`` method (a fresh
interpreter that *imports* this module), so the submitted function must be a
module-level name — a lambda or a closure can't be pickled — and nothing at
import time may start processes. The ``findex`` entry point and
``python -m findex`` only run ``app()`` from the console-script / ``__main__``
guard, so a spawned child importing ``findex.cli`` doesn't re-run the CLI.

A worker that raises fails the whole build: ``future.result()`` re-raises the
exception in the parent (for processes with the worker's own traceback chained
as ``__cause__``), pending chunks are cancelled, and nothing waits forever.
"""

from __future__ import annotations

import logging
import multiprocessing
import time
from array import array
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import as_completed as _as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from findex.corpus import list_document_paths, read_text
from findex.index import ArrayPostings, DocMeta, Index, Posting, PostingList, guess_title
from findex.timing import timed
from findex.tokenize import tokenize

log = logging.getLogger(__name__)

# Plain assignments (not PEP 695 `type`): typer reads these at runtime.
ExecutorName = Literal["serial", "threads", "processes"]
MergeLayout = Literal["slots", "array"]
EXECUTORS: tuple[ExecutorName, ...] = ("serial", "threads", "processes")


@dataclass(frozen=True, slots=True)
class Chunk:
    """A contiguous run of files and the doc id its first file gets."""

    paths: tuple[Path, ...]
    first_doc_id: int
    base: Path  # doc ids (stored paths) are relative to this
    positions: bool = False


@dataclass(slots=True)
class PartialIndex:
    """What one worker returns: postings for its own doc-id range only.

    Compact on purpose — it crosses a process boundary by pickle: postings are
    two ``array('I')`` per term (pickled as raw bytes), not Posting objects.
    """

    first_doc_id: int
    postings: dict[str, ArrayPostings] = field(default_factory=dict[str, ArrayPostings])
    # term -> one positions tuple per posting (aligned with postings), or None
    positions: dict[str, list[tuple[int, ...]]] | None = None
    doc_lengths: array[int] = field(default_factory=lambda: array("I"))
    doc_meta: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # (path, title)

    @property
    def num_docs(self) -> int:
        return len(self.doc_meta)


def build_partial(
    doc_paths: Sequence[Path],
    *,
    base: Path,
    first_doc_id: int = 0,
    positions: bool = False,
) -> PartialIndex:
    """Index ``doc_paths`` as documents ``first_doc_id, first_doc_id + 1, …``.

    Module-level and side-effect free, so a process pool can pickle a reference
    to it. An unreadable file becomes an empty document (``read_text`` logs a
    warning): skipping it would leave a hole in the pre-assigned id range.
    """
    part = PartialIndex(first_doc_id, positions={} if positions else None)
    arrays: defaultdict[str, ArrayPostings] = defaultdict(lambda: (array("I"), array("I")))
    where_all: defaultdict[str, list[tuple[int, ...]]] = defaultdict(list)
    for i, path in enumerate(doc_paths):
        doc_id = first_doc_id + i
        text = read_text(path) or ""
        rel = path.relative_to(base).as_posix()
        if positions:
            where: defaultdict[str, list[int]] = defaultdict(list)
            n = -1
            for n, tok in enumerate(tokenize(text)):
                where[tok].append(n)
            part.doc_lengths.append(n + 1)
            for term, pos in where.items():
                ids, tfs = arrays[term]
                ids.append(doc_id)
                tfs.append(len(pos))
                where_all[term].append(tuple(pos))
        else:
            counts = Counter(tokenize(text))
            part.doc_lengths.append(counts.total())
            for term, tf in counts.items():
                ids, tfs = arrays[term]
                ids.append(doc_id)
                tfs.append(tf)
        part.doc_meta.append((rel, guess_title(text, Path(rel).stem)))
    part.postings = dict(arrays)
    if positions:
        part.positions = dict(where_all)
    return part


def build_chunk(chunk: Chunk) -> PartialIndex:
    """One-argument wrapper for executors (module level, so it pickles)."""
    return build_partial(
        chunk.paths, base=chunk.base, first_doc_id=chunk.first_doc_id, positions=chunk.positions
    )


@timed
def merge(partials: Iterable[PartialIndex], layout: MergeLayout = "slots") -> Index:
    """One :class:`Index` from partial indexes, in any arrival order.

    Partials are put in doc-id order first; then each term's postings are the
    concatenation of its per-chunk postings, which is already doc-id-sorted
    because the chunks' id ranges are increasing and disjoint. Terms keep their
    first-seen order, so the result equals a single serial pass byte for byte.
    """
    ordered = sorted(partials, key=lambda p: p.first_doc_id)
    expected = 0
    for p in ordered:  # ranges must tile 0..N-1 exactly
        if p.first_doc_id != expected:
            raise ValueError(f"doc id gap or overlap at {expected} (chunk starts {p.first_doc_id})")
        expected += p.num_docs
    with_positions = bool(ordered) and all(p.positions is not None for p in ordered)
    if with_positions and layout == "array":
        raise ValueError("positions need layout='slots'")

    doc_lengths: dict[int, int] = {}
    doc_meta: dict[int, DocMeta] = {}
    postings: dict[str, PostingList]
    if layout == "array":
        arrays: dict[str, ArrayPostings] = {}
        for p in ordered:
            for term, (ids, tfs) in p.postings.items():
                if term in arrays:
                    arrays[term][0].extend(ids)
                    arrays[term][1].extend(tfs)
                else:  # copy: the partial's arrays must not alias the index's
                    arrays[term] = (array("I", ids), array("I", tfs))
        postings = dict(arrays)
    else:
        lists: dict[str, list[Posting]] = {}
        for p in ordered:
            for term, (ids, tfs) in p.postings.items():
                plist = lists.setdefault(term, [])
                if p.positions is not None and with_positions:
                    plist.extend(map(Posting, ids, tfs, p.positions[term]))
                else:
                    plist.extend(map(Posting, ids, tfs))
        postings = dict(lists)
    for p in ordered:
        for i, (path, title) in enumerate(p.doc_meta):
            doc_id = p.first_doc_id + i
            doc_lengths[doc_id] = p.doc_lengths[i]
            doc_meta[doc_id] = DocMeta(doc_id, path, title)
    return Index(postings, doc_lengths, doc_meta, layout, with_positions)


def make_chunks(
    paths: Sequence[Path], n_chunks: int, *, base: Path, positions: bool = False
) -> list[Chunk]:
    """Split ``paths`` into ≤ ``n_chunks`` contiguous runs of similar total size.

    Contiguous, so each chunk's ids are one range; balanced by bytes, not by
    file count, because one 260 KB page costs as much as fifty small ones.
    """
    if not paths:
        return []
    n_chunks = max(1, n_chunks)
    sizes = [p.stat().st_size + 1 for p in paths]
    target = sum(sizes) / n_chunks
    max_files = -(-len(paths) // n_chunks)  # ceil: no chunk hoards the small files
    chunks: list[Chunk] = []
    start, acc = 0, 0
    for i, size in enumerate(sizes):
        count = i - start
        # Close the current chunk *before* file i if it would overflow the byte
        # target or already holds its share of files — so a big file at the end
        # gets its own chunk instead of dragging every small file into one.
        if count and len(chunks) < n_chunks - 1 and (acc + size > target or count >= max_files):
            chunks.append(Chunk(tuple(paths[start:i]), start, base, positions))
            start, acc = i, 0
        acc += size
    chunks.append(Chunk(tuple(paths[start:]), start, base, positions))
    return chunks


def _make_executor(executor: ExecutorName, workers: int) -> Executor:
    if executor == "threads":
        return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="findex")
    # spawn everywhere: no inherited locks or threads from the parent (the
    # fork hazard), and the same behaviour on Linux, macOS and Windows.
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def run_chunks(
    chunks: Sequence[Chunk],
    executor: ExecutorName = "serial",
    workers: int = 1,
    *,
    fn: Callable[[Chunk], PartialIndex] = build_chunk,
    on_done: Callable[[PartialIndex], None] | None = None,
) -> list[PartialIndex]:
    """Run ``fn`` over ``chunks``; results in completion order.

    The first exception from any worker cancels the chunks that haven't
    started, waits for the running ones, and propagates — never a hang.
    """
    if executor == "serial":
        out: list[PartialIndex] = []
        for c in chunks:
            out.append(fn(c))
            if on_done:
                on_done(out[-1])
        return out
    pool = _make_executor(executor, workers)
    futures: list[Future[PartialIndex]] = [pool.submit(fn, c) for c in chunks]
    results: list[PartialIndex] = []
    try:
        for fut in _as_completed(futures):
            part = fut.result()  # re-raises a worker's exception here
            results.append(part)
            if on_done:
                on_done(part)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


@dataclass(frozen=True, slots=True)
class BuildStats:
    executor: ExecutorName
    workers: int
    chunks: int
    documents: int
    partial_s: float  # wall time until every partial index is back
    merge_s: float  # wall time of merge() alone — the serial part


def build_parallel(
    root: Path,
    *,
    executor: ExecutorName = "serial",
    workers: int = 1,
    positions: bool = False,
    limit: int | None = None,
    layout: MergeLayout = "slots",
    chunks_per_worker: int = 4,
    on_done: Callable[[PartialIndex], None] | None = None,
) -> tuple[Index, BuildStats]:
    """Index a directory of text files with ``workers`` workers of one kind.

    ``chunks_per_worker`` > 1 gives the pool slack to balance uneven files;
    the serial executor uses a single chunk (no merge cost to speak of).
    """
    base, paths = list_document_paths(root)
    if limit is not None:
        paths = paths[:limit]
    n_chunks = 1 if executor == "serial" else max(1, workers * chunks_per_worker)
    chunks = make_chunks(paths, n_chunks, base=base, positions=positions)
    log.info("%d files in %d chunks, %s × %d", len(paths), len(chunks), executor, workers)
    t0 = time.perf_counter()
    partials = run_chunks(chunks, executor, workers, on_done=on_done)
    t1 = time.perf_counter()
    index = merge(partials, layout)
    t2 = time.perf_counter()
    stats = BuildStats(executor, workers, len(chunks), index.num_docs, t1 - t0, t2 - t1)
    log.info("partials %.2f s, merge %.2f s", stats.partial_s, stats.merge_s)
    return index, stats
