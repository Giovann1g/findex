"""A race that the GIL hides and free-threading exposes: ``python -m findex.race``.

A naive threaded term counter where every thread updates **one shared**
``Counter``::

    counts[token] += 1      # read, add, write back — three steps, not one

Under the GIL a thread switch can only happen between bytecodes at the
interpreter's checkpoints, so in practice the read-add-write rarely gets
interleaved. On a free-threaded build (3.13t) two threads really run that line
at the same time on two cores: both read 7, both write 8, one update is lost.

The fixes, both measured below:

* ``lock``      — serialize the read-modify-write with a ``threading.Lock``;
* ``no-sharing``— each thread counts into its own ``Counter``, merged at the
  end. This is what ``findex.parallel`` does: ``build_partial`` touches no
  shared state, so it is correct on both builds and needs no lock.

Run with each interpreter and compare; ``--dis`` shows the bytecode.
"""

from __future__ import annotations

import argparse
import dis
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from findex.corpus import list_document_paths, read_text
from findex.tokenize import tokenize

Mode = Literal["racy", "lock", "no-sharing"]


def _shards(tokens: list[str], n: int) -> list[list[str]]:
    return [tokens[i::n] for i in range(n)]


def count_shared(tokens: list[str], workers: int, mode: Mode) -> Counter[str]:
    shared: Counter[str] = Counter()
    lock = threading.Lock()

    def racy(shard: list[str]) -> None:
        for tok in shard:
            shared[tok] += 1  # BUG on free-threaded builds: not atomic

    def locked(shard: list[str]) -> None:
        for tok in shard:
            with lock:
                shared[tok] += 1

    def private(shard: list[str]) -> Counter[str]:
        return Counter(shard)  # nothing shared while threads run

    with ThreadPoolExecutor(workers) as pool:
        if mode == "no-sharing":
            for part in pool.map(private, _shards(tokens, workers)):
                shared.update(part)  # merged by one thread, after the fact
        else:
            body: Callable[[list[str]], None] = racy if mode == "racy" else locked
            list(pool.map(body, _shards(tokens, workers)))
    return shared


def increment(counts: Counter[str], key: str) -> None:
    counts[key] += 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.race")
    p.add_argument("root", type=Path, nargs="?", default=Path("data"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--dis", action="store_true", help="show the bytecode of counts[key] += 1")
    args = p.parse_args(argv)
    if args.dis:
        dis.dis(increment)
        return

    _, paths = list_document_paths(args.root)
    tokens = [t for path in paths for t in tokenize(read_text(path) or "")]
    expected = len(tokens)
    gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True  # pyright: ignore[reportPrivateUsage]  # the documented way to ask
    print(f"Python {sys.version.split()[0]}, sys._is_gil_enabled() = {gil}")
    print(f"{expected:,} tokens, {args.workers} threads, {args.rounds} rounds\n")
    print("| mode | lost updates per round | wall (median) |")
    print("|---|---|---:|")
    for mode in ("racy", "lock", "no-sharing"):
        lost: list[int] = []
        walls: list[float] = []
        for _ in range(args.rounds):
            t0 = time.perf_counter()
            counts = count_shared(tokens, args.workers, mode)
            walls.append(time.perf_counter() - t0)
            lost.append(expected - counts.total())
        walls.sort()
        print(f"| {mode} | {', '.join(f'{n:,}' for n in lost)} | {walls[len(walls) // 2]:.2f} s |")


if __name__ == "__main__":
    main()
