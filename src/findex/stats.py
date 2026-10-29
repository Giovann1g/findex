"""Corpus statistics: one lazy pass, plus a deliberately eager twin.

Usage::

    python -m findex.stats data/                  # lazy pipeline (default)
    python -m findex.stats data/ --limit 100      # first 100 documents only
    python -m findex.stats data/ --mode eager     # the list-based version
    python -m findex.stats data/ --no-trace       # time without tracemalloc
"""

from __future__ import annotations

import argparse
import time
import tracemalloc
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from findex.corpus import Document, iter_documents
from findex.tokenize import tokenize


@dataclass
class CorpusStats:
    documents: int
    tokens: int
    counts: Counter[str]

    @property
    def vocabulary(self) -> int:
        return len(self.counts)

    def top(self, n: int = 50) -> list[tuple[str, int]]:
        return self.counts.most_common(n)


# --------------------------------------------------------------------------
# Lazy: generators all the way down. The Counter is the only thing that grows.
# --------------------------------------------------------------------------
def compute_stats(docs: Iterable[Document]) -> CorpusStats:
    """Single pass over a stream of documents."""
    n_docs = 0

    def counted(stream: Iterable[Document]) -> Iterator[Document]:
        nonlocal n_docs
        for doc in stream:
            n_docs += 1
            yield doc

    counts: Counter[str] = Counter(tok for doc in counted(docs) for tok in tokenize(doc.text))
    return CorpusStats(n_docs, counts.total(), counts)


# --------------------------------------------------------------------------
# Eager: the "obvious" version — every stage materialized into a list.
# --------------------------------------------------------------------------
def compute_stats_eager(root: Path, limit: int | None = None) -> CorpusStats:
    """Read all docs into a list, tokenize into a list of lists, then count."""
    docs = list(islice(iter_documents(root), limit))  # every doc's text in RAM
    token_lists = [list(tokenize(d.text)) for d in docs]  # every token in RAM
    counts: Counter[str] = Counter()
    for toks in token_lists:
        counts.update(toks)
    return CorpusStats(len(docs), sum(len(t) for t in token_lists), counts)


def run(root: Path, mode: str, limit: int | None, trace: bool = True):
    """Run one mode, return (stats, elapsed_seconds, peak_bytes | None)."""
    if trace:
        tracemalloc.start()
    t0 = time.perf_counter()
    if mode == "eager":
        stats = compute_stats_eager(root, limit)
    else:
        stats = compute_stats(islice(iter_documents(root), limit))
    elapsed = time.perf_counter() - t0
    peak = None
    if trace:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return stats, elapsed, peak


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError  # unreachable


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.stats", description=__doc__)
    p.add_argument("root", type=Path, help="corpus directory or .jsonl file")
    p.add_argument("--limit", type=int, default=None, help="first N documents")
    p.add_argument("--top", type=int, default=50, help="how many top terms")
    p.add_argument("--mode", choices=["lazy", "eager"], default="lazy")
    p.add_argument("--no-trace", action="store_true", help="skip tracemalloc")
    args = p.parse_args(argv)

    stats, elapsed, peak = run(args.root, args.mode, args.limit, not args.no_trace)

    print(f"mode:            {args.mode}")
    print(f"documents:       {stats.documents:,}")
    print(f"tokens:          {stats.tokens:,}")
    print(f"vocabulary:      {stats.vocabulary:,}")
    print(f"elapsed:         {elapsed:.2f} s")
    print(f"peak memory:     {fmt_bytes(peak) if peak is not None else 'n/a'}")
    print(f"\ntop-{args.top} terms:")
    for rank, (term, count) in enumerate(stats.top(args.top), start=1):
        print(f"{rank:>4}. {term:<20} {count:>9,}")


if __name__ == "__main__":
    main()
