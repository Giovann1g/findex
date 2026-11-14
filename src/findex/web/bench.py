"""Lab 7 measurements: ``python -m findex.web.bench INDEX --out docs/lab07``.

1. **Twenty at once** — the M2 decision. For three versions of the search
   route (``def``; ``async def`` doing the work on the loop; ``async def``
   with ``await asyncio.to_thread``) send 20 concurrent ``/search`` requests
   and, *during* that burst, ``/health`` probes. Recorded: wall time for the
   20, their p50/max latency, and how long ``/health`` waited — the event
   loop's responsiveness.
2. **oha** — 20 s at concurrency 50 with a random query per request
   (``--rand-regex-url``): 1 worker ``async def``, 1 worker ``def``, 4 workers.

Each server is a fresh ``uvicorn`` process running :func:`bench_app` (the real
app plus the two alternative routes, which exist only here — the app itself
keeps the ``def`` route).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from findex.rank import ScorerName
from findex.web.api import K, PageNo, Q, search_page
from findex.web.app import create_app
from findex.web.deps import IndexDep, SettingsDep
from findex.web.models import SearchResponse

QUERIES = ["asyncio", "thread", "unicode", "dict", "socket", "import", "class", "loop",
           "file", "string", "json", "error", '"event loop"', "list AND sort"]  # fmt: skip
ORACLE = r"(asyncio|thread|unicode|dict|socket|import|class|loop|file|string|json|error)"


def bench_app() -> FastAPI:
    """The app, plus the two variants of ``/search`` that lost the comparison."""
    app = create_app()

    @app.get("/search-async", response_model=SearchResponse, include_in_schema=False)
    async def search_on_the_loop(  # pyright: ignore[reportUnusedFunction]
        index: IndexDep, settings: SettingsDep, q: Q, k: K = 10, page: PageNo = 1,
        scorer: ScorerName = "bm25",
    ) -> SearchResponse:  # fmt: skip
        return search_page(index, settings, q, k, page, scorer)  # CPU work *on* the event loop

    @app.get("/search-to-thread", response_model=SearchResponse, include_in_schema=False)
    async def search_in_a_thread(  # pyright: ignore[reportUnusedFunction]
        index: IndexDep, settings: SettingsDep, q: Q, k: K = 10, page: PageNo = 1,
        scorer: ScorerName = "bm25",
    ) -> SearchResponse:  # fmt: skip
        return await asyncio.to_thread(search_page, index, settings, q, k, page, scorer)

    return app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    def __init__(self, index: Path, workers: int) -> None:
        self.port = free_port()
        env = {**os.environ, "INDEX_PATH": str(index), "LOG_LEVEL": "WARNING"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "findex.web.bench:bench_app", "--factory",
             "--port", str(self.port), "--workers", str(workers), "--log-level", "warning",
             "--no-access-log"],
            env=env,
        )  # fmt: skip
        self.base = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Server:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base}/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("server did not become healthy")

    def __exit__(self, *exc: object) -> None:
        self.proc.terminate()
        self.proc.wait()


async def twenty_at_once(base: str, route: str, n: int = 20) -> dict[str, float]:
    """``n`` concurrent searches, with ``/health`` probed every 10 ms meanwhile."""
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=httpx.Limits(
            max_connections=n + 5)) as client:  # fmt: skip
        for q in QUERIES:  # warm: the per-index query cache and the OS page cache
            await client.get(route, params={"q": q})
        health: list[float] = []
        done = asyncio.Event()

        async def probe() -> None:
            while not done.is_set():
                t = time.perf_counter()
                await client.get("/health")
                health.append(time.perf_counter() - t)
                await asyncio.sleep(0.01)

        async def one(i: int) -> float:
            t = time.perf_counter()
            r = await client.get(route, params={"q": QUERIES[i % len(QUERIES)], "k": 10})
            r.raise_for_status()
            return time.perf_counter() - t

        prober = asyncio.create_task(probe())
        t0 = time.perf_counter()
        lat = await asyncio.gather(*(one(i) for i in range(n)))
        wall = time.perf_counter() - t0
        done.set()
        await prober
    return {
        "wall_ms": wall * 1000,
        "p50_ms": statistics.median(lat) * 1000,
        "max_ms": max(lat) * 1000,
        "health_max_ms": max(health) * 1000,
        "health_p50_ms": statistics.median(health) * 1000,
    }


def run_oha(url: str, concurrency: int, seconds: int) -> dict[str, float]:
    oha = shutil.which("oha") or str(Path.home() / ".cargo/bin/oha")
    out = subprocess.run(
        [oha, "-z", f"{seconds}s", "-c", str(concurrency), "--no-tui", "--output-format", "json",
         "--rand-regex-url", url],
        check=True, capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},  # local target
    ).stdout  # fmt: skip
    data: dict[str, Any] = json.loads(out)
    pct = data["latencyPercentiles"]
    codes: dict[str, int] = data.get("statusCodeDistribution", {})
    return {
        "rps": float(data["summary"]["requestsPerSec"]),
        "p50_ms": float(pct["p50"]) * 1000,
        "p95_ms": float(pct["p95"]) * 1000,
        "p99_ms": float(pct["p99"]) * 1000,
        "ok": int(codes.get("200", 0)),
        "other": sum(v for k, v in codes.items() if k != "200"),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.web.bench")
    p.add_argument("index", type=Path)
    p.add_argument("--out", type=Path, default=Path("docs/lab07"))
    p.add_argument("--seconds", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--rounds", type=int, default=5)
    args = p.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"cpu": os.cpu_count(), "twenty": {}, "oha": {}}

    with Server(args.index, workers=1) as srv:
        for route in ("/search", "/search-async", "/search-to-thread"):
            rounds = [asyncio.run(twenty_at_once(srv.base, route)) for _ in range(args.rounds)]
            med = {k: statistics.median(r[k] for r in rounds) for k in rounds[0]}
            results["twenty"][route] = med
            print(f"20 at once {route:18} " + ", ".join(f"{k} {v:.1f}" for k, v in med.items()),
                  file=sys.stderr, flush=True)  # fmt: skip

    def url(base: str, route: str) -> str:  # a regex: oha draws a random query per request
        # "localhost", not 127.0.0.1: oha's URL regex mangles "\." (and "." is fine only by luck)
        return base.replace("127.0.0.1", "localhost") + route + r"\?q=" + ORACLE + "&k=10"

    for label, workers, route in (
        ("1 worker, async def", 1, "/search-async"),
        ("1 worker, def", 1, "/search"),
        ("4 workers, def", 4, "/search"),
    ):
        with Server(args.index, workers=workers) as srv:
            row = run_oha(url(srv.base, route), args.concurrency, args.seconds)
        results["oha"][label] = row
        print(f"oha {label:22} {row}", file=sys.stderr, flush=True)

    (args.out / "load.json").write_text(json.dumps(results, indent=2) + "\n")
    print("| Route variant | 20 at once: wall | p50 | slowest | /health waited (max) |")
    print("|---|---:|---:|---:|---:|")
    for route, r in results["twenty"].items():
        cells = [r["wall_ms"], r["p50_ms"], r["max_ms"], r["health_max_ms"]]
        print(f"| `{route}` | " + " | ".join(f"{c:.0f} ms" for c in cells) + " |")
    print()
    print("| Target | Concurrency | RPS | p50 | p95 | p99 |")
    print("|---|---:|---:|---:|---:|---:|")
    for label, r in results["oha"].items():
        print(f"| local, {label} | {args.concurrency} | {r['rps']:.0f} | {r['p50_ms']:.0f} ms | "
              f"{r['p95_ms']:.0f} ms | {r['p99_ms']:.0f} ms |")  # fmt: skip


if __name__ == "__main__":
    main()
