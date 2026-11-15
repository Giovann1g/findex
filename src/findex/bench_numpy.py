"""Lab 8 table: Lab 3's scorer vs the NumPy one, ``python -m findex.bench_numpy index.pkl``.

For a frequent term, a rare term and a three-term query, time one ranked
search (k = 10) with the Boolean match already cached — what is left is the
scoring and the top-k — once with :func:`findex.rank.search` on the ``slots``
index and once with :func:`findex.vecrank.search_np` on the same index in the
``numpy`` layout. Median of ``--repeat`` timings of ``--number`` calls each.

Also measured: loading each index from its pickle, its size and the RSS it
adds — the first hotspot of the "before" profile.
"""

from __future__ import annotations

import argparse
import gc
import json
import pickle
import statistics
import subprocess
import sys
import tempfile
import timeit
from functools import partial
from pathlib import Path

from findex import store
from findex.index import Index
from findex.npindex import to_numpy
from findex.query import parse as parse_query
from findex.rank import BM25, ScorerName, TfIdf, search
from findex.vecrank import search_np


def pick(index: Index) -> dict[str, str]:
    by_df = sorted(index, key=lambda t: (-index.df(t), t))
    rare = next(t for t in sorted(index) if index.df(t) == 2 and t.isalpha() and len(t) > 6)
    return {
        f"frequent term `{by_df[0]}` (df {index.df(by_df[0])})": by_df[0],
        f"rare term `{rare}` (df 2)": rare,
        "three terms `asyncio event loop`": "asyncio event loop",
    }


def ms(fn: object, number: int, repeat: int) -> float:
    t = timeit.repeat(fn, number=number, repeat=repeat)  # type: ignore[arg-type]
    return statistics.median(t) / number * 1000


_LOAD = r"""
import sys, time
from findex import store
def rss():
    with open("/proc/self/status") as f:
        return next(int(l.split()[1]) for l in f if l.startswith("VmRSS")) / 1024
r0 = rss()
t = time.perf_counter(); ix = store.load(sys.argv[1]); dt = time.perf_counter() - t
print(dt, rss() - r0)
"""


def load_cost(path: Path, runs: int = 3) -> tuple[float, float]:
    cmd = [sys.executable, "-c", _LOAD, str(path)]
    out = [
        subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.split()
        for _ in range(runs)
    ]
    return statistics.median(float(o[0]) for o in out), statistics.median(float(o[1]) for o in out)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.bench_numpy")
    p.add_argument("index", type=Path, help="a slots index with positions (Lab 4)")
    p.add_argument("--out", type=Path, default=Path("docs/lab08/numpy.json"))
    p.add_argument("--number", type=int, default=200)
    p.add_argument("--repeat", type=int, default=7)
    args = p.parse_args(argv)
    slots = store.load(args.index)
    numpy_ix = to_numpy(slots)
    rows: list[dict[str, object]] = []
    for label, q in pick(slots).items():
        cases: list[tuple[str, BM25 | TfIdf, ScorerName]] = [
            ("BM25", BM25(), "bm25"),
            ("TF-IDF", TfIdf(), "tfidf"),
        ]
        for scorer_name, lab3, vec in cases:
            node = parse_query(q)
            assert node is not None
            search(slots, q, scorer=lab3)  # warm both match caches
            search_np(numpy_ix, q, scorer=vec)
            gc.disable()
            a = ms(partial(search, slots, q, scorer=lab3, k=10), args.number, args.repeat)
            b = ms(partial(search_np, numpy_ix, q, scorer=vec, k=10), args.number, args.repeat)
            gc.enable()
            row: dict[str, object] = {
                "query": label,
                "scorer": scorer_name,
                "matched": len(slots.match(node)),
                "lab3_ms": a,
                "numpy_ms": b,
                "speedup": a / b,
            }
            rows.append(row)
            print(
                f"{label:45} {scorer_name:6} {a:8.3f} ms {b:8.3f} ms {a / b:6.1f}x", file=sys.stderr
            )

    with tempfile.TemporaryDirectory() as tmp:
        npath = Path(tmp) / "numpy.pkl"
        with npath.open("wb") as f:
            pickle.dump(numpy_ix, f, protocol=pickle.HIGHEST_PROTOCOL)
        load_slots, rss_slots = load_cost(args.index)
        load_np, rss_np = load_cost(npath)
        sizes = {"slots": args.index.stat().st_size, "numpy": npath.stat().st_size}
    loads = {
        "slots": {"load_s": load_slots, "rss_mib": rss_slots, "bytes": sizes["slots"]},
        "numpy": {"load_s": load_np, "rss_mib": rss_np, "bytes": sizes["numpy"]},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"rows": rows, "load": loads}, indent=2) + "\n")

    print("| Query | Scorer | Docs matched | Lab 3 scorer (ms) | NumPy scorer (ms) | Speedup |")
    print("|---|---|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['query']} | {r['scorer']} | {r['matched']} | {r['lab3_ms']:.3f} | "
              f"{r['numpy_ms']:.3f} | {r['speedup']:.1f}× |")  # fmt: skip
    print()
    print("| Layout | File | Load (pickle) | RSS added |")
    print("|---|---:|---:|---:|")
    for name, cost in loads.items():
        print(f"| {name} | {cost['bytes'] / 2**20:.1f} MiB | {cost['load_s'] * 1000:.0f} ms | "
              f"{cost['rss_mib']:.0f} MiB |")  # fmt: skip


if __name__ == "__main__":
    main()
