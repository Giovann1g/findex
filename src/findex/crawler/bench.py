"""Lab 6 benchmark: ``python -m findex.crawler.bench MIRROR_DIR --out docs/lab06``.

Starts the local mirror (:mod:`findex.crawler.mirror`) in its own process —
so its threads and memory don't count against the crawler — then, for every
configuration, runs **a fresh crawler process** ``--repeat`` times from the
same seed with the same ``--max-pages`` and keeps the median:

* wall time, pages, pages/s;
* errors (final failures) and retries;
* CPU time of the crawler process (user + system) — how much of the wall
  time the one thread actually computed;
* peak RSS of the crawler process (``ru_maxrss``).

Rows: asyncio at concurrency 1, 5, 20 (per-host limit = concurrency, no
pause: the mirror is ours, so this measures the mechanism), then the cost of
politeness (per-host 2, with and without a 0.25 s pause). Then, with the
mirror's faults off, the *same URL list* (what concurrency 20 collected)
fetched three ways: asyncio with 20 in flight, Lab 5's processes (one per
core, each fetching its share in a loop) and 20 threads with a blocking client.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict, cast

_ASYNC_CHILD = r"""
import asyncio, json, logging, os, resource, sys, time
from pathlib import Path
from findex.crawler.run import crawl_to_jsonl
seed, out, log_path = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
c, per_host, delay, max_pages, timeout = map(float, sys.argv[4:9])
handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
lg = logging.getLogger("findex.crawl"); lg.setLevel(logging.INFO); lg.addHandler(handler)
t0, c0 = time.perf_counter(), os.times()
stats = asyncio.run(crawl_to_jsonl([seed], out, max_pages=int(max_pages), concurrency=int(c),
                    per_host=int(per_host), min_delay=delay, timeout=timeout))
t1, c1 = time.perf_counter(), os.times()
print(json.dumps({"wall": t1 - t0, "cpu": (c1.user - c0.user) + (c1.system - c0.system),
    "pages": stats.pages, "errors": stats.errors, "retries": stats.retries,
    "requests": stats.requests, "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
"""

# The same URLs, fetched the Lab 5 way: no event loop, a blocking client.
_SYNC_CHILD = r"""
import json, os, resource, sys, time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import httpx
from findex.crawler.extract import extract
from findex.crawler.fetch import USER_AGENT
mode, n, paths_file = sys.argv[1], int(sys.argv[2]), sys.argv[3]
timeout = float(sys.argv[4])
urls = [sys.argv[5] + path for path in json.load(open(paths_file))]

def get_all(chunk):
    ok = err = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout,
                      follow_redirects=True) as client:
        for url in chunk:
            try:
                r = client.get(url)
                extract(r.text); ok += r.status_code == 200; err += r.status_code != 200
            except httpx.HTTPError:
                err += 1
    return ok, err

if __name__ == "__main__":
    t0, c0 = time.perf_counter(), os.times()
    chunks = [urls[i::n] for i in range(n)]
    with (ProcessPoolExecutor if mode == "processes" else ThreadPoolExecutor)(n) as pool:
        results = list(pool.map(get_all, chunks))
    t1, c1 = time.perf_counter(), os.times()
    cpu = sum(getattr(c1, f) - getattr(c0, f)
              for f in ("user", "system", "children_user", "children_system"))
    print(json.dumps({"wall": t1 - t0, "cpu": cpu, "pages": sum(r[0] for r in results),
        "errors": sum(r[1] for r in results), "retries": 0, "requests": len(urls),
        "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
"""

# The same URL list with asyncio but no crawler around it, for a fair comparison.
_ASYNC_LIST_CHILD = r"""
import asyncio, json, os, resource, sys, time
import httpx
from findex.crawler.extract import extract
from findex.crawler.fetch import USER_AGENT
n, paths_file, timeout = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
urls = [sys.argv[4] + path for path in json.load(open(paths_file))]

async def main():
    sem, ok, err = asyncio.Semaphore(n), 0, 0
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=timeout,
                                 follow_redirects=True) as client:
        async def one(url):
            nonlocal ok, err
            async with sem:
                try:
                    r = await client.get(url)
                    extract(r.text); ok += r.status_code == 200; err += r.status_code != 200
                except httpx.HTTPError:
                    err += 1
        async with asyncio.TaskGroup() as tg:
            for url in urls:
                tg.create_task(one(url))
    return ok, err

t0, c0 = time.perf_counter(), os.times()
ok, err = asyncio.run(main())
t1, c1 = time.perf_counter(), os.times()
print(json.dumps({"wall": t1 - t0, "cpu": (c1.user - c0.user) + (c1.system - c0.system),
    "pages": ok, "errors": err, "retries": 0, "requests": len(urls),
    "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
"""


class Run(TypedDict):
    wall: float
    cpu: float
    pages: int
    errors: int
    retries: int
    requests: int
    rss_kib: int


@dataclass
class Row:
    label: str
    concurrency: int
    per_host: int
    delay: float
    pages: int
    wall: float
    wall_min: float
    wall_max: float
    pages_per_s: float
    errors: int
    retries: int
    cpu: float
    rss_mib: float


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"mirror did not start on port {port}")


def child(code: str, *args: object) -> Run:
    out = subprocess.run(
        [sys.executable, "-c", code, *map(str, args)], check=True, capture_output=True, text=True
    ).stdout
    return cast(Run, json.loads(out.strip().splitlines()[-1]))


def summarize(label: str, c: int, per_host: int, delay: float, runs: list[Run]) -> Row:
    walls = [r["wall"] for r in runs]
    wall = statistics.median(walls)
    pages = int(statistics.median(r["pages"] for r in runs))
    row = Row(
        label, c, per_host, delay, pages, wall, min(walls), max(walls), pages / wall,
        int(statistics.median(r["errors"] for r in runs)),
        int(statistics.median(r["retries"] for r in runs)),
        statistics.median(r["cpu"] for r in runs),
        statistics.median(r["rss_kib"] for r in runs) / 1024,
    )  # fmt: skip
    print(
        f"{label:28} {row.pages} pages, wall {wall:.2f} s [{min(walls):.2f}..{max(walls):.2f}], "
        f"{row.pages_per_s:.1f} p/s, errors {row.errors}, retries {row.retries}, "
        f"cpu {row.cpu:.2f} s, rss {row.rss_mib:.0f} MiB",
        file=sys.stderr,
        flush=True,
    )
    return row


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.crawler.bench")
    p.add_argument("mirror_root", type=Path)
    p.add_argument("--out", type=Path, default=Path("docs/lab06"))
    p.add_argument("--max-pages", type=int, default=300)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--latency", type=float, default=80.0)
    p.add_argument("--jitter", type=float, default=40.0)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--hang", type=float, default=8.0)
    args = p.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    work = out / "runs"
    work.mkdir(exist_ok=True)

    mirror_args = [str(args.mirror_root), "--latency", str(args.latency),
                   "--jitter", str(args.jitter), "--hang", str(args.hang)]  # fmt: skip

    def with_mirror(code: str, *child_args: object, faults: bool = True) -> Run:
        """A fresh mirror per run: every run meets the same faults, once each."""
        port = free_port()
        cmd = [sys.executable, "-m", "findex.crawler.mirror", *mirror_args, "--port", str(port)]
        if not faults:
            cmd.append("--no-faults")
        mirror = subprocess.Popen(cmd, stdout=subprocess.DEVNULL)
        try:
            wait_for(port)
            seed = f"http://127.0.0.1:{port}/"
            return child(code, *(a.replace("{seed}", seed) if isinstance(a, str) else a
                                 for a in child_args))  # fmt: skip
        finally:
            mirror.terminate()
            mirror.wait()

    rows: list[Row] = []
    configs = [
        ("asyncio, concurrency 1", 1, 1, 0.0),
        ("asyncio, concurrency 5", 5, 5, 0.0),
        ("asyncio, concurrency 20", 20, 20, 0.0),
        ("asyncio 20, per-host 2", 20, 2, 0.0),
        ("asyncio 20, per-host 2, 0.25 s", 20, 2, 0.25),
    ]
    for label, c, ph, delay in configs:
        name = f"c{c}-h{ph}-d{delay:g}"
        jsonl, log = str(work / f"{name}.jsonl"), str(work / f"{name}.log")
        runs = [
            with_mirror(
                _ASYNC_CHILD, "{seed}", jsonl, log, c, ph, delay, args.max_pages, args.timeout
            )  # fmt: skip
            for _ in range(args.repeat)
        ]
        rows.append(summarize(label, c, ph, delay, runs))

    # the same pages for the blocking baselines: the paths concurrency 20 fetched
    paths = [
        json.loads(line)["url"].split("/", 3)[3] for line in (work / "c20-h20-d0.jsonl").open()
    ]
    paths_file = work / "paths.json"
    paths_file.write_text(json.dumps(paths))
    cores = os.cpu_count() or 1
    baselines: list[Row] = []
    for label, code, n, extra in (
        ("asyncio × 20 (one thread)", _ASYNC_LIST_CHILD, 20, ()),
        (f"Lab 5: processes × {cores}", _SYNC_CHILD, cores, ("processes",)),
        ("threads × 20, blocking client", _SYNC_CHILD, 20, ("threads",)),
    ):
        child_args = (*extra, n, paths_file, args.timeout, "{seed}")
        runs = [with_mirror(code, *child_args, faults=False) for _ in range(args.repeat)]
        baselines.append(summarize(label, n, n, 0.0, runs))

    payload = {
        "seed": "local mirror of docs.python.org (Sphinx build of CPython Doc/)",
        "max_pages": args.max_pages,
        "repeat": args.repeat,
        "latency_ms": args.latency,
        "jitter_ms": args.jitter,
        "timeout_s": args.timeout,
        "cores": os.cpu_count(),
        "rows": [asdict(r) for r in rows],
        "same_urls_no_faults": [asdict(r) for r in baselines],
    }
    (out / "crawl_bench.json").write_text(json.dumps(payload, indent=2) + "\n")
    cols = [
        "Run",
        "Pages",
        "Wall (median)",
        "Spread",
        "Pages/s",
        "Errors",
        "Retries",
        "CPU",
        "Peak RSS",
    ]
    print("| " + " | ".join(cols) + " |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in [*rows, *baselines]:
        print(
            f"| {r.label} | {r.pages} | {r.wall:.2f} s | {r.wall_min:.2f}–{r.wall_max:.2f} | "
            f"{r.pages_per_s:.1f} | {r.errors} | {r.retries} | {r.cpu:.2f} s | "
            f"{r.rss_mib:.0f} MiB |"
        )


if __name__ == "__main__":
    main()
