"""Boolean search over a saved index.

Usage::

    python -m findex.search index.bin "dict hash"            # implicit AND
    python -m findex.search index.bin "asyncio OR threading"
    python -m findex.search index.bin "generator NOT async" --engine set

Query language (evaluated **left to right**, no precedence, no parentheses)::

    query   := operand (op operand)*
    op      := "AND" | "OR" | <nothing>      # nothing = AND
    operand := ["NOT"] word

Operators must be written in upper case; lower-case ``and``/``or``/``not`` are
ordinary search words. Each word goes through the same ``tokenize`` as the
corpus, so ``"Straße"`` finds ``strasse``; a word that tokenizes into several
tokens (``state-of-the-art``) means the AND of them.

Two engines give identical results:

* ``merge`` — two-pointer merges over the doc-id-sorted postings,
  O(len(a) + len(b)), no hashing, streams naturally;
* ``set``   — ``set(a) & set(b)`` / ``|`` / ``-``, then ``sorted``.
"""

from __future__ import annotations

import argparse
import time
import tracemalloc
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from findex.index import Index, fmt_bytes
from findex.tokenize import tokenize

OPERATORS = {"AND", "OR", "NOT"}


# ---------------------------------------------------------------------------
# Two-pointer merges. Inputs: ascending sequences of unique ints.
# ---------------------------------------------------------------------------
def intersect(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def intersect_gallop(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """AND for skewed lengths: binary-search each id of the short list in the
    long one, O(m log n) instead of O(m + n) — what real engines do with skip
    pointers. Used by the benchmark to explain when merging can win."""
    if len(a) > len(b):
        a, b = b, a
    out: list[int] = []
    lo, nb = 0, len(b)
    for x in a:
        lo = bisect_left(b, x, lo)
        if lo == nb:
            break
        if b[lo] == x:
            out.append(x)
    return out


def union(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """a AND NOT b."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na:
        if j == nb:
            out.extend(a[i:])
            break
        x, y = a[i], b[j]
        if x == y:
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            j += 1
    return out


@dataclass(frozen=True, slots=True)
class Engine:
    name: str
    and_: Callable[[Sequence[int], Sequence[int]], list[int]]
    or_: Callable[[Sequence[int], Sequence[int]], list[int]]
    not_: Callable[[Sequence[int], Sequence[int]], list[int]]


MERGE = Engine("merge", intersect, union, difference)
SET = Engine(
    "set",
    lambda a, b: sorted(set(a) & set(b)),
    lambda a, b: sorted(set(a) | set(b)),
    lambda a, b: sorted(set(a) - set(b)),
)
ENGINES = {"merge": MERGE, "set": SET}


# ---------------------------------------------------------------------------
# Parsing + evaluation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Clause:
    op: str  # "AND" | "OR"  (how this clause combines with what came before)
    negated: bool
    terms: tuple[str, ...]  # normalized tokens of one query word (AND-ed together)


def parse(query: str) -> list[Clause]:
    clauses: list[Clause] = []
    op, negated = "AND", False
    for word in query.split():
        if word in OPERATORS:
            if word == "NOT":
                negated = not negated
            else:
                op = word
            continue
        terms = tuple(tokenize(word))
        if not terms:  # pure punctuation
            continue
        clauses.append(Clause(op, negated, terms))
        op, negated = "AND", False
    return clauses


def _word_docs(index: Index, terms: tuple[str, ...], engine: Engine) -> Sequence[int]:
    docs = index.doc_ids(terms[0])
    for t in terms[1:]:
        docs = engine.and_(docs, index.doc_ids(t))
    return docs


def evaluate(index: Index, query: str | list[Clause], engine: Engine = MERGE) -> list[int]:
    clauses = parse(query) if isinstance(query, str) else query
    if not clauses:
        return []
    everything = range(index.n_docs)  # doc_ids are dense 0..N-1
    result: Sequence[int] | None = None
    for c in clauses:
        docs = _word_docs(index, c.terms, engine)
        if result is None:  # first operand
            result = engine.not_(everything, docs) if c.negated else docs
        elif c.op == "OR":
            result = engine.or_(result, engine.not_(everything, docs) if c.negated else docs)
        else:  # AND
            result = engine.not_(result, docs) if c.negated else engine.and_(result, docs)
    return list(result or ())


def main(argv: list[str] | None = None) -> None:
    from findex import store

    p = argparse.ArgumentParser(prog="python -m findex.search", description=__doc__)
    p.add_argument("index", type=Path, help="file written by python -m findex.index")
    p.add_argument("query")
    p.add_argument("--engine", choices=list(ENGINES), default="merge")
    p.add_argument("--format", choices=store.FORMATS, default=None)
    p.add_argument("--lazy", action="store_true", help=".bin only: mmap, decode on demand")
    p.add_argument("--show", type=int, default=10, help="how many results to print")
    args = p.parse_args(argv)

    tracemalloc.start()
    t0 = time.perf_counter()
    index = store.load(args.index, args.format, lazy=args.lazy)
    t_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    hits = evaluate(index, args.query, ENGINES[args.engine])
    t_query = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"query:   {args.query!r}  -> {[c.terms for c in parse(args.query)]}")
    print(f"hits:    {len(hits):,} of {index.n_docs:,} documents   [engine={args.engine}]")
    for doc_id in hits[: args.show]:
        m = index.doc_meta[doc_id]
        print(f"  {doc_id:>5}  {m.title[:60]:<60}  {m.path}")
    if len(hits) > args.show:
        print(f"  ... and {len(hits) - args.show:,} more")
    print(f"load:    {t_load * 1000:.1f} ms")
    print(f"query:   {t_query * 1000:.3f} ms")
    print(f"peak memory: {fmt_bytes(peak)}")


if __name__ == "__main__":
    main()
