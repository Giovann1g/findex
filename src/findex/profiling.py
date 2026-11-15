"""Lab 8 profiling targets, so a profiler has something steady to watch.

``python -m findex.profiling search INDEX "asyncio event loop" --repeat 300``
loads the index once and runs the same query ``--repeat`` times — the part of
``findex search`` that is *not* startup. ``index CORPUS`` builds an index in
this process (no pool), like ``findex index --executor serial``. Run either
under ``python -m cProfile -o …``, ``py-spy record -o flame.svg -- …`` or
``scalene --cli …`` (README, "Профілювання").
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from findex import store
from findex.corpus import iter_documents
from findex.index import LAYOUTS, Layout, build_index


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.profiling")
    sub = p.add_subparsers(dest="what", required=True)
    s = sub.add_parser("search")
    s.add_argument("index", type=Path)
    s.add_argument("query")
    s.add_argument("--repeat", type=int, default=300)
    s.add_argument("--scorer", choices=["bm25", "tfidf"], default="bm25")
    s.add_argument("--numpy", action="store_true", help="the Lab 8 vectorized scorer")
    i = sub.add_parser("index")
    i.add_argument("corpus", type=Path)
    i.add_argument("--layout", choices=LAYOUTS, default="slots")
    i.add_argument("--positions", action="store_true")
    args = p.parse_args(argv)

    if args.what == "index":
        layout: Layout = args.layout
        t0 = time.perf_counter()
        ix = build_index(iter_documents(args.corpus), layout, args.positions)
        print(f"{ix!r} in {time.perf_counter() - t0:.2f} s")
        return

    ix = store.load(args.index)
    if args.numpy:
        from findex.vecrank import search_np

        def run() -> int:
            return len(search_np(ix, args.query, scorer=args.scorer, k=10))
    else:
        from findex.rank import make_scorer, search

        scorer = make_scorer(args.scorer)

        def run() -> int:
            return len(search(ix, args.query, scorer=scorer, k=10))

    t0 = time.perf_counter()
    for _ in range(args.repeat):
        run()
    dt = (time.perf_counter() - t0) / args.repeat
    print(f"{args.query!r}: {dt * 1000:.3f} ms per search ({args.repeat} runs)")


if __name__ == "__main__":
    main()
