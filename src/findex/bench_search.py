"""merge vs set on real postings: ``python -m findex.bench_search index.bin``.

Picks the two most frequent terms (largest df) and two of the rarest
(df == 1, alphabetically first) and times AND / OR / AND NOT with both
engines. Only the Boolean operation is timed: the doc-id sequences are fetched
from the index beforehand, so both engines get exactly the same input.
"""

from __future__ import annotations

import argparse
import random
import timeit
from collections.abc import Sequence
from pathlib import Path

from findex import store
from findex.index import Index
from findex.merge import ENGINES, SetOp, intersect_gallop


def pick_terms(index: Index) -> tuple[list[str], list[str]]:
    """Two largest-df terms; two hapax legomena (df == 1, tf == 1) that are
    ordinary words, picked with a fixed seed so the run is reproducible."""
    by_df = sorted(index, key=lambda t: (-index.df(t), t))
    common = by_df[:2]
    hapax = sorted(
        t
        for t in index
        if index.df(t) == 1
        and index.tfs(t)[0] == 1
        and t.isalpha()
        and t.isascii()
        and len(t) >= 7
        and len(set(t)) >= 5
    )
    rare = random.Random(2).sample(hapax, 2)
    return common, rare


def time_op(fn: SetOp, a: Sequence[int], b: Sequence[int], number: int) -> float:
    """Best of 5 repeats, microseconds per call."""
    return min(timeit.repeat(lambda: fn(a, b), number=number, repeat=5)) / number * 1e6


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.bench_search")
    p.add_argument("index", type=Path)
    p.add_argument("--number", type=int, default=2000)
    args = p.parse_args(argv)
    index = store.load(args.index)
    common, rare = pick_terms(index)
    mixed = [common[0], rare[0]]

    print("| Terms | df | Op | Hits | merge, µs | set, µs | Faster | merge+bisect, µs |")
    print("|---|---|---|---:|---:|---:|---|---:|")
    for label, (x, y) in (("2 most common", common), ("2 rarest", rare), ("common + rare", mixed)):
        a, b = list(index.doc_ids(x)), list(index.doc_ids(y))
        for op, attr in (("AND", "and_"), ("OR", "or_"), ("AND NOT", "not_")):
            m = getattr(ENGINES["merge"], attr)
            s = getattr(ENGINES["set"], attr)
            assert m(a, b) == s(a, b)
            tm = time_op(m, a, b, args.number)
            ts = time_op(s, a, b, args.number)
            win = f"set ×{tm / ts:.1f}" if ts < tm else f"merge ×{ts / tm:.1f}"
            gal = "—"
            if op == "AND":
                assert intersect_gallop(a, b) == m(a, b)
                gal = f"{time_op(intersect_gallop, a, b, args.number):.2f}"
            print(f"| {label}: `{x}`, `{y}` | {len(a)}, {len(b)} | {op} | {len(m(a, b))} | "
                  f"{tm:.2f} | {ts:.2f} | {win} | {gal} |")  # fmt: skip


if __name__ == "__main__":
    main()
