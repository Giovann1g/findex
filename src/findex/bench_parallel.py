"""Lab 5 benchmark: ``python -m findex.bench_parallel data/ --out docs/lab05``.

Every measurement is a fresh subprocess running ``build_parallel`` once:

* wall time  — ``time.perf_counter`` around build + merge;
* CPU time   — ``os.times()`` user + system of the process **and of its
  waited-for children**, so worker processes' CPU is counted too (a pool's
  workers are joined at shutdown, so their time is in ``children_*``);
* peak RSS   — ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` of the parent;
* merge time — reported by ``build_parallel`` itself.

Each configuration runs ``--repeat`` times (median + min..max kept); one
discarded serial run first warms the OS page cache. ``--ft PYTHON`` adds
rows from a free-threaded interpreter (3.13t). Results go to
``parallel.json``; ``--plot`` draws ``speedup.png`` from that file.
"""

# matplotlib's own annotations declare `**kwargs` without a type, which strict
# pyright reports at every Axes call in plot(); nothing else here is affected.
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import argparse
import json
import os
import pickle
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict, cast

_CHILD = r"""
import json, os, resource, sys, time
from pathlib import Path
from findex.parallel import build_parallel
root, executor, workers = sys.argv[1], sys.argv[2], int(sys.argv[3])
t0, c0 = time.perf_counter(), os.times()
index, stats = build_parallel(Path(root), executor=executor, workers=workers)
t1, c1 = time.perf_counter(), os.times()
cpu = sum(getattr(c1, f) - getattr(c0, f)
          for f in ("user", "system", "children_user", "children_system"))
gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
print(json.dumps({"wall": t1 - t0, "cpu": cpu, "merge": stats.merge_s,
                  "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  "docs": index.num_docs, "chunks": stats.chunks, "gil": gil}))
"""


class Run(TypedDict):
    wall: float
    cpu: float
    merge: float
    rss_kib: int
    docs: int
    chunks: int
    gil: bool


@dataclass
class Row:
    python: str  # "3.13" or "3.13t"
    executor: str
    workers: int
    gil: bool
    chunks: int
    wall: float  # medians
    wall_min: float
    wall_max: float
    cpu: float
    rss_mib: float
    merge: float
    speedup: float = 0.0  # vs the GIL serial median, filled in at the end


def list_paths(root: Path) -> list[Path]:
    from findex.corpus import list_document_paths

    return list_document_paths(root)[1]


def run_once(python: str, root: Path, executor: str, workers: int) -> Run:
    out = subprocess.run(
        [python, "-c", _CHILD, str(root), executor, str(workers)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return cast(Run, json.loads(out))  # the child prints exactly this shape


def measure(label: str, python: str, root: Path, executor: str, workers: int, repeat: int) -> Row:
    runs = [run_once(python, root, executor, workers) for _ in range(repeat)]
    walls = [r["wall"] for r in runs]
    row = Row(
        python=label,
        executor=executor,
        workers=workers,
        gil=runs[0]["gil"],
        chunks=runs[0]["chunks"],
        wall=statistics.median(walls),
        wall_min=min(walls),
        wall_max=max(walls),
        cpu=statistics.median(r["cpu"] for r in runs),
        rss_mib=statistics.median(r["rss_kib"] for r in runs) / 1024,
        merge=statistics.median(r["merge"] for r in runs),
    )
    print(
        f"{label:6} {executor:9} x{workers}: wall {row.wall:.2f} s "
        f"[{row.wall_min:.2f}..{row.wall_max:.2f}], cpu {row.cpu:.2f} s, "
        f"rss {row.rss_mib:.0f} MiB, merge {row.merge:.2f} s",
        file=sys.stderr,
    )
    return row


def ipc_cost(root: Path, chunks: int) -> dict[str, float]:
    """What crosses the process boundary: partial indexes vs. raw texts."""
    from findex.corpus import list_document_paths
    from findex.parallel import build_chunk, make_chunks

    base, paths = list_document_paths(root)
    partials = [build_chunk(c) for c in make_chunks(paths, chunks, base=base)]
    t0 = time.perf_counter()
    blobs = [pickle.dumps(p, protocol=pickle.HIGHEST_PROTOCOL) for p in partials]
    t1 = time.perf_counter()
    for b in blobs:
        pickle.loads(b)
    t2 = time.perf_counter()
    texts = sum(len(pickle.dumps(p.read_text("utf-8"))) for p in paths)
    return {
        "partials_mib": sum(map(len, blobs)) / 2**20,
        "dumps_ms": (t1 - t0) * 1000,
        "loads_ms": (t2 - t1) * 1000,
        "texts_mib": texts / 2**20,
        "paths_kib": len(pickle.dumps(paths)) / 1024,
    }


def _fetch(path: Path, latency: float) -> int:
    time.sleep(latency)  # stands in for a network round trip: the GIL is released
    return len(path.read_bytes())


def io_demo(root: Path, latency_ms: float, workers: list[int]) -> list[tuple[int, float]]:
    """I/O-bound counterpart: "download" every document, threads under the GIL."""
    from concurrent.futures import ThreadPoolExecutor

    from findex.corpus import list_document_paths

    _, paths = list_document_paths(root)
    latency = latency_ms / 1000
    out: list[tuple[int, float]] = []
    for w in workers:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(w) as pool:
            total = sum(pool.map(_fetch, paths, [latency] * len(paths)))
        out.append((w, time.perf_counter() - t0))
        print(f"io x{w}: {out[-1][1]:.2f} s ({total / 2**20:.1f} MiB)", file=sys.stderr)
    return out


def machine() -> dict[str, str | int]:
    model = platform.processor() or "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"cpu": model, "cores": os.cpu_count() or 1, "os": platform.platform()}


def plot(results: Path, png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = json.loads(results.read_text())
    rows = [Row(**r) for r in data["rows"]]
    cores = int(data["machine"]["cores"])
    fig, ax = plt.subplots(figsize=(7.5, 4.6), dpi=130)
    xs = sorted({r.workers for r in rows})
    ax.plot(xs, xs, linestyle="--", color="#999999", linewidth=1.2, label="ideal (linear)")
    ax.plot(xs, [min(x, cores) for x in xs], linestyle=":", color="#999999", linewidth=1.2,
            label=f"ideal on {cores} cores")  # fmt: skip
    styles = {
        ("3.13", "processes"): ("#1f6feb", "o", "processes (GIL build)"),
        ("3.13", "threads"): ("#8c8c8c", "s", "threads (GIL build)"),
        ("3.13t", "threads"): ("#d97706", "^", "threads (3.13t, no GIL)"),
    }
    for (py, ex), (color, marker, label) in styles.items():
        sel = sorted(
            (r for r in rows if r.python == py and r.executor == ex), key=lambda r: r.workers
        )
        if sel:
            ax.plot([r.workers for r in sel], [r.speedup for r in sel], marker=marker,
                    color=color, linewidth=2, label=label)  # fmt: skip
    top = max(cores + 0.5, max(r.speedup for r in rows) * 1.2)
    ax.axvline(cores, color="#cccccc", linewidth=1)
    ax.text(cores * 1.04, top * 0.04, f"{cores} cores", color="#666666", fontsize=9)
    ax.axhline(1.0, color="#444444", linewidth=0.8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs, [str(x) for x in xs])
    ax.set_ylim(0, top)
    ax.set_xlabel("workers")
    ax.set_ylabel("speedup vs serial (GIL build)")
    ax.set_title(f"findex index, {data['docs']} documents — {data['machine']['cpu']}", fontsize=10)
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    ax.grid(axis="y", color="#eeeeee")
    fig.tight_layout()
    fig.savefig(png)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.bench_parallel")
    p.add_argument("root", type=Path)
    p.add_argument("--out", type=Path, default=Path("docs/lab05"))
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    p.add_argument("--ft", default=None, help="free-threaded python (3.13t) for extra rows")
    p.add_argument("--plot", action="store_true", help="only redraw the PNG from the JSON")
    p.add_argument("--io", type=float, metavar="MS", help="only the I/O-bound demo, MS per file")
    args = p.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    if args.io is not None:
        rows_io = io_demo(args.root, args.io, [1, 2, 4, 8, 16, 32])
        print(f"| Threads | Wall ({len(list_paths(args.root))} files × {args.io:g} ms) | Speedup |")
        print("|---:|---:|---:|")
        for w, wall in rows_io:
            print(f"| {w} | {wall:.2f} s | {rows_io[0][1] / wall:.1f}× |")
        return
    if args.plot:
        plot(out / "parallel.json", out / "speedup.png")
        return

    root: Path = args.root
    workers: list[int] = sorted(set(args.workers) | {os.cpu_count() or 1})
    gil_py = sys.executable
    run_once(gil_py, root, "serial", 1)  # warm the page cache; discarded
    rows = [measure("3.13", gil_py, root, "serial", 1, args.repeat)]
    for ex in ("threads", "processes"):
        rows += [measure("3.13", gil_py, root, ex, w, args.repeat) for w in workers]
    if args.ft:
        rows.append(measure("3.13t", args.ft, root, "serial", 1, args.repeat))
        rows += [measure("3.13t", args.ft, root, "threads", w, args.repeat) for w in workers]
    base = rows[0].wall
    for r in rows:
        r.speedup = base / r.wall
    payload = {
        "machine": machine(),
        "docs": run_once(gil_py, root, "serial", 1)["docs"],
        "repeat": args.repeat,
        "ipc": ipc_cost(root, 4 * (os.cpu_count() or 1)),
        "rows": [asdict(r) for r in rows],
    }
    (out / "parallel.json").write_text(json.dumps(payload, indent=2) + "\n")
    plot(out / "parallel.json", out / "speedup.png")

    cols = ["Python", "Executor", "Workers", "Wall (median)", "Spread", "CPU", "Peak RSS", "Merge"]
    print("| " + " | ".join([*cols, "Speedup"]) + " |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r.python} | {r.executor} | {r.workers} | {r.wall:.2f} s | "
            f"{r.wall_min:.2f}–{r.wall_max:.2f} | {r.cpu:.2f} s | {r.rss_mib:.0f} MiB | "
            f"{r.merge:.2f} s | {r.speedup:.2f}× |"
        )


if __name__ == "__main__":
    main()
