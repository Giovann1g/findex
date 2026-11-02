"""Memory study + persistence benchmark: ``python -m findex.bench_index data/``.

Every number comes from a *fresh* subprocess, so one run's garbage or warm
caches can't leak into the next. Memory = ``tracemalloc`` (peak during the
build, and what is still allocated once the build is done = the index itself).
Times are wall-clock from separate *untraced* runs (best of ``--repeat``),
because tracing slows allocation-heavy code several times.
Prints three Markdown tables.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Literal, cast

from findex.index import Layout, fmt_bytes
from findex.store import Format

_CHILD = r"""
import json, sys, time, tracemalloc, gc
from pathlib import Path
from findex.corpus import iter_documents
from findex.index import build_index
from findex import store

action, root, layout, positions, path, fmt, lazy, trace = sys.argv[1:9]
positions, lazy, trace = positions == "1", lazy == "1", trace == "1"
res = {}
if trace:
    tracemalloc.start()
if action == "build":
    t0 = time.perf_counter()
    idx = build_index(iter_documents(Path(root)), layout, positions)
    res["build"] = time.perf_counter() - t0
    gc.collect()
    if trace:
        res["resident"], res["peak"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    if path != "-":
        t0 = time.perf_counter()
        store.save(idx, Path(path), fmt)
        res["save"] = time.perf_counter() - t0
        res["size"] = Path(path).stat().st_size
elif action == "load":
    t0 = time.perf_counter()
    idx = store.load(Path(path), fmt, lazy=lazy)
    res["load"] = time.perf_counter() - t0
    if trace:
        res["resident"], res["peak"] = tracemalloc.get_traced_memory()
print(json.dumps(res))
"""


# What a child run prints: some of build/save/load (seconds), size/resident/peak (bytes).
type Result = dict[str, float]


def child(
    action: Literal["build", "load"],
    root: Path,
    layout: Layout = "slots",
    positions: bool = False,
    path: Path | str = "-",
    fmt: Format = "pickle",
    lazy: bool = False,
    trace: bool = False,
) -> Result:
    argv = [action, str(root), layout, "01"[positions], str(path), fmt, "01"[lazy], "01"[trace]]
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, *argv], check=True, capture_output=True, text=True
    ).stdout
    return cast(Result, json.loads(out))  # the child prints a flat dict of numbers


def best(repeat: int, key: str, run: Callable[[], Result]) -> Result:
    """The fastest of ``repeat`` runs, judged by the timing ``key``."""
    return min((run() for _ in range(repeat)), key=lambda r: r[key])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.bench_index")
    p.add_argument("root", type=Path)
    p.add_argument("--repeat", type=int, default=3)
    args = p.parse_args(argv)
    root: Path = args.root
    rep: int = args.repeat
    tmp = Path(tempfile.mkdtemp(prefix="findex-bench-"))

    # --- M4: three postings layouts (+ positions), each saved with pickle ---
    rows: list[tuple[str, Layout, bool]] = [
        ("`list[Posting]`, plain `@dataclass`", "plain", False),
        ("`list[Posting]`, `slots=True`", "slots", False),
        ("`array('I')` pairs (doc_ids, tfs)", "array", False),
        ("`list[Posting]`, `slots=True` + positions", "slots", True),
    ]
    print("| Postings representation | Peak memory (build) | Index in RAM | Build | "
          "Pickle file | Save | Load |")  # fmt: skip
    print("|---|---:|---:|---:|---:|---:|---:|")
    for label, layout, pos in rows:
        pkl = tmp / f"{layout}{'-pos' if pos else ''}.pkl"
        mem = child("build", root, layout, pos, trace=True)
        timed = best(rep, "build", partial(child, "build", root, layout, pos, pkl, "pickle"))
        load = best(rep, "load", partial(child, "load", root, path=pkl, fmt="pickle"))
        print(f"| {label} | {fmt_bytes(mem['peak'])} | {fmt_bytes(mem['resident'])} | "
              f"{timed['build']:.2f} s | {fmt_bytes(timed['size'])} | "
              f"{timed['save'] * 1000:.0f} ms | {load['load'] * 1000:.0f} ms |")  # fmt: skip

    # --- M3: formats, same index content ---
    print("\n| Format | Loads into | File size | Save | Load | Index in RAM after load |")
    print("|---|---|---:|---:|---:|---:|")
    fmts: list[tuple[Format, Layout, str, bool, str]] = [
        ("pickle", "slots", ".pkl", False, "`list[Posting]` (slots)"),
        ("pickle", "array", ".pkl", False, "`array` pairs"),
        ("json", "slots", ".json", False, "`list[Posting]` (slots)"),
        ("bin", "slots", ".bin", False, "`array` pairs"),
        ("bin", "slots", ".bin", True, "lazy `mmap`, decoded per term"),
    ]
    for fmt, layout, suffix, lazy, into in fmts:
        path = tmp / f"fmt-{fmt}-{layout}{suffix}"
        saved = best(rep, "save", partial(child, "build", root, layout, False, path, fmt))
        load = best(rep, "load", partial(child, "load", root, path=path, fmt=fmt, lazy=lazy))
        mem = child("load", root, path=path, fmt=fmt, lazy=lazy, trace=True)
        name = f"`{fmt}`" + (" (`--lazy`)" if lazy else "") + ("" if fmt != "pickle" else
                                                                f" of `{layout}`")  # fmt: skip
        print(f"| {name} | {into} | {fmt_bytes(saved['size'])} | "
              f"{saved['save'] * 1000:.0f} ms | {load['load'] * 1000:.1f} ms | "
              f"{fmt_bytes(mem['resident'])} |")  # fmt: skip


if __name__ == "__main__":
    main()
