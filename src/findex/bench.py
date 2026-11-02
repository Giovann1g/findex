"""Eager-vs-lazy benchmark: ``python -m findex.bench data/ --limits 100 300 0``.

Each measurement runs in a *fresh* subprocess so one run's garbage can't
inflate the next one's peak. Peak memory comes from a traced run
(``tracemalloc``); elapsed time from a separate untraced run, because
tracing itself slows allocation-heavy code down several times.
Limit ``0`` means "the whole corpus". Prints a Markdown table.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast

from findex.stats import fmt_bytes

_CHILD = """
import json, sys
from pathlib import Path
from findex.stats import run
root, mode, limit, trace = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
stats, elapsed, peak = run(Path(root), mode, limit or None, trace)
print(json.dumps({"docs": stats.documents, "tokens": stats.tokens,
                  "vocab": stats.vocabulary, "elapsed": elapsed, "peak": peak}))
"""


class Measurement(TypedDict):
    docs: int
    tokens: int
    vocab: int
    elapsed: float
    peak: int | None  # None for untraced runs


def measure(root: Path, mode: str, limit: int, trace: bool, repeat: int = 1) -> Measurement:
    best: Measurement | None = None
    for _ in range(repeat):
        out = subprocess.run(
            [sys.executable, "-c", _CHILD, str(root), mode, str(limit), "1" if trace else "0"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        res = cast(Measurement, json.loads(out))  # the child prints exactly this shape
        if best is None or res["elapsed"] < best["elapsed"]:
            best = res
    assert best is not None
    return best


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.bench")
    p.add_argument("root", type=Path)
    p.add_argument("--limits", type=int, nargs="+", default=[0])
    p.add_argument("--repeat", type=int, default=3, help="untraced timing runs (best of)")
    args = p.parse_args(argv)

    print("| Version | Documents | Tokens | Peak memory | Elapsed | Elapsed (traced) |")
    print("|---|---:|---:|---:|---:|---:|")
    for limit in args.limits:
        for mode, label in (("eager", "eager (lists)"), ("lazy", "lazy (generators)")):
            traced = measure(args.root, mode, limit, trace=True)
            timed = measure(args.root, mode, limit, trace=False, repeat=args.repeat)
            print(
                f"| {label} | {traced['docs']:,} | {traced['tokens']:,} | "
                f"{fmt_bytes(traced['peak'] or 0)} | {timed['elapsed']:.2f} s | "
                f"{traced['elapsed']:.2f} s |"
            )


if __name__ == "__main__":
    main()
