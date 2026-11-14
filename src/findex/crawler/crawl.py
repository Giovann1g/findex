"""The crawler: ``async for page in crawl(seeds, max_pages=…): …``.

Shape (one thread, one event loop)::

    frontier: asyncio.Queue[(url, depth)]      seen: set[canonical url]
           │  get                    ▲ put new links (not seen, allowed domain)
           ▼                         │
    ┌─ TaskGroup ──────────────────────────────────────────────┐
    │  worker × concurrency:                                   │
    │    robots.allowed(url)?                                  │
    │    async with host_sem[host]:   ≤ per_host to one server │
    │      async with global_sem:     ≤ concurrency in flight  │
    │        await host pause         ≥ min_delay apart        │
    │        await fetch(...)         timeout, retries         │
    │    extract title/text/links  → results.put(Page)         │
    │  drained: await frontier.join(); cancel the idle workers │
    └──────────────────────────────────────────────────────────┘
           │  results: asyncio.Queue[Page] (bounded: back-pressure)
           ▼
    ``crawl()`` yields each Page to its caller as it arrives

``crawl`` stops after ``max_pages`` pages, or when the frontier is empty and
every worker is idle. Either way it cancels the workers (``CancelledError``
lands at whatever they were awaiting), the TaskGroup waits for them, and the
HTTP client is closed — also when the caller breaks out of its ``async for``
early or an exception escapes. A failure on one URL is logged and counted,
never fatal: the worker moves on to the next URL.

The workers share ``seen``, the stats and the per-host schedule without
locks: between two ``await``s a coroutine runs alone, and each of those
updates happens between awaits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin

import httpx

from findex.crawler.extract import extract
from findex.crawler.fetch import USER_AGENT, FetchResult, fetch, now_iso
from findex.crawler.robots import RobotsCache
from findex.crawler.urls import canonicalize, host_of, in_domains, looks_like_page

log = logging.getLogger("findex.crawl")


@dataclass(frozen=True, slots=True)
class Page:
    url: str  # canonical URL of the page as served (after redirects)
    title: str
    text: str
    fetched_at: str  # ISO 8601, UTC
    status: int = 200
    depth: int = 0  # links followed from a seed
    bytes: int = 0
    elapsed: float = 0.0


@dataclass(slots=True)
class CrawlStats:
    """Live counters, read by the CLI's progress display while the crawl runs."""

    started: float = field(default_factory=time.perf_counter)
    pages: int = 0  # pages yielded
    requests: int = 0  # page fetches finished (any outcome)
    in_flight: int = 0  # fetches waiting for a response right now
    errors: int = 0  # final failures: 4xx/5xx after retries, timeouts, loops
    retries: int = 0
    robots_blocked: int = 0
    skipped: int = 0  # fetched fine but not HTML, duplicates after redirect, …
    queued: int = 0  # frontier depth
    bytes: int = 0

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def pages_per_s(self) -> float:
        return self.pages / self.elapsed if self.elapsed > 0 else 0.0


@dataclass(slots=True)
class _HostState:
    sem: asyncio.Semaphore
    next_at: float = 0.0  # loop time before which no new request may start


class Crawler:
    """State of one crawl. Use :func:`crawl`; this class only holds the parts."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_pages: int,
        concurrency: int,
        per_host: int,
        min_delay: float,
        allowed_domains: frozenset[str],
        max_depth: int | None,
        timeout: float,
        stats: CrawlStats,
    ) -> None:
        self.client = client
        self.max_pages = max_pages
        self.concurrency = concurrency
        self.per_host = per_host
        self.min_delay = min_delay
        self.allowed = allowed_domains
        self.max_depth = max_depth
        self.timeout = timeout
        self.stats = stats
        self.robots = RobotsCache(client, timeout=timeout)
        self.frontier: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.results: asyncio.Queue[Page] = asyncio.Queue(maxsize=concurrency)
        self.seen: set[str] = set()
        self.global_sem = asyncio.Semaphore(concurrency)
        self.hosts: dict[str, _HostState] = {}
        # page budget: pages delivered + fetches under way never exceed max_pages,
        # so the crawl doesn't fetch pages it will throw away
        self.claimed = 0
        self.freed = asyncio.Event()

    # -- frontier ---------------------------------------------------------------
    def enqueue(self, url: str, depth: int) -> bool:
        """Queue ``url`` unless seen, off-domain or not a page. No await: atomic."""
        key = canonicalize(url)
        if key is None or key in self.seen:
            return False
        if not in_domains(key, self.allowed) or not looks_like_page(key):
            return False
        if self.max_depth is not None and depth > self.max_depth:
            return False
        self.seen.add(key)
        self.frontier.put_nowait((urldefrag(url).url, depth))
        self.stats.queued = self.frontier.qsize()
        return True

    # -- politeness -------------------------------------------------------------
    def host(self, url: str) -> _HostState:
        h = host_of(url)
        if h not in self.hosts:
            self.hosts[h] = _HostState(asyncio.Semaphore(self.per_host))
        return self.hosts[h]

    def push_back(self, url: str, delay: float) -> None:
        """A 429/503 told us to wait: no worker starts a request to that host sooner."""
        state = self.host(url)
        loop = asyncio.get_running_loop()
        state.next_at = max(state.next_at, loop.time() + delay)
        self.stats.retries += 1

    async def polite_pause(self, url: str) -> None:
        """Keep requests to one host ``min_delay`` (or its Crawl-delay) apart."""
        delay = max(self.min_delay, await self.robots.crawl_delay(url) or 0.0)
        state = self.host(url)
        loop = asyncio.get_running_loop()
        now = loop.time()
        start = max(now, state.next_at)
        state.next_at = start + delay  # reserve the slot before awaiting: no lock needed
        if start > now:
            await asyncio.sleep(start - now)

    # -- one URL ------------------------------------------------------------------
    async def visit(self, url: str, depth: int) -> Page | None:
        if not await self.robots.allowed(url):
            self.stats.robots_blocked += 1
            log.info("robots  %s: disallowed by robots.txt", url)
            return None
        state = self.host(url)
        async with state.sem, self.global_sem:  # per-host first: don't hog a global slot
            await self.polite_pause(url)
            self.stats.in_flight += 1
            try:
                result = await fetch(
                    self.client, url, timeout=self.timeout, on_retry=self.push_back
                )
            finally:
                self.stats.in_flight -= 1
        self.stats.requests += 1
        self.stats.bytes += len(result.body)
        return self.handle(result, depth)

    def handle(self, result: FetchResult, depth: int) -> Page | None:
        """Log the outcome; for an HTML page, queue its links and build the Page."""
        line = f"{result.status:3d} {result.url} {len(result.body)} B {result.elapsed:.2f} s"
        if result.attempts > 1:
            line += f" ({result.attempts} attempts)"
        if not result.ok:
            self.stats.errors += 1
            log.warning("%s: %s", line, result.error)
            return None
        final = canonicalize(result.final_url)
        if final is None or not in_domains(final, self.allowed):
            self.stats.skipped += 1
            log.info("%s: redirected off the allowed domains to %s", line, result.final_url)
            return None
        if final != canonicalize(result.url):
            if final in self.seen:  # /a redirected to /b, and /b is already ours
                self.stats.skipped += 1
                log.info("%s: redirect to %s, already seen", line, final)
                return None
            self.seen.add(final)
        if not result.is_html:
            self.stats.skipped += 1
            log.info("%s: skipped, %s", line, result.content_type or "no content type")
            return None
        page = extract(result.text)
        if not page.nofollow:
            for href in page.links:
                # resolve against where the page really is, not the canonical form
                self.enqueue(urljoin(result.final_url, href), depth + 1)
        log.info("%s %r, %d links", line, page.title[:60], len(page.links))
        return Page(
            final,
            page.title or final,
            page.text,
            now_iso(),
            result.status,
            depth,
            len(result.body),
            result.elapsed,
        )

    # -- budget ---------------------------------------------------------------------
    async def claim(self) -> None:
        """Wait until a page slot is free and take it."""
        while self.claimed >= self.max_pages:
            self.freed.clear()
            await self.freed.wait()  # a fetch under way failed: its slot came back
        self.claimed += 1

    def release(self) -> None:
        self.claimed -= 1
        self.freed.set()

    # -- tasks ----------------------------------------------------------------------
    async def worker(self) -> None:
        while True:
            url, depth = await self.frontier.get()
            self.stats.queued = self.frontier.qsize()
            try:
                await self.claim()
                page = None
                try:
                    page = await self.visit(url, depth)
                except Exception:  # one bad page must not stop the crawl; CancelledError passes
                    self.stats.errors += 1
                    log.exception("error %s: unexpected failure", url)
                finally:
                    if page is None:
                        self.release()
                if page is not None:
                    await self.results.put(page)
            finally:
                self.frontier.task_done()

    async def run(self) -> None:
        """The workers' scope: a TaskGroup that ends when the frontier drains.

        ``drained`` waits until every queued URL has been processed with no
        worker busy (``Queue.join``), then cancels the idle workers; cancelled
        children end a TaskGroup normally. A worker that *crashed* would
        cancel its siblings and surface here as an ``ExceptionGroup``.
        """
        async with asyncio.TaskGroup() as tg:
            workers = [tg.create_task(self.worker()) for _ in range(self.concurrency)]

            async def drained() -> None:
                await self.frontier.join()
                for w in workers:
                    w.cancel()

            tg.create_task(drained())


async def crawl(
    seeds: Iterable[str],
    *,
    max_pages: int = 500,
    concurrency: int = 10,
    per_host: int = 2,
    min_delay: float = 0.5,
    allowed_domains: Iterable[str] | None = None,
    max_depth: int | None = None,
    timeout: float = 10.0,
    user_agent: str = USER_AGENT,
    client: httpx.AsyncClient | None = None,
    stats: CrawlStats | None = None,
) -> AsyncIterator[Page]:
    """Yield up to ``max_pages`` pages reachable from ``seeds``, politely.

    ``allowed_domains`` defaults to the seeds' hosts (subdomains included).
    Pass ``client`` to reuse one (tests pass one with an ``httpx.MockTransport``);
    otherwise one ``AsyncClient`` is opened for the whole crawl — connection
    reuse is a large part of the speed — and closed at the end.
    """
    seeds = list(seeds)
    domains = frozenset(
        d.lower()
        for d in (allowed_domains or (httpx.URL(s).host for s in seeds))
        if d
    )  # fmt: skip
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )
    stats = stats if stats is not None else CrawlStats()
    crawler = Crawler(
        client,
        max_pages=max_pages,
        concurrency=concurrency,
        per_host=per_host,
        min_delay=min_delay,
        allowed_domains=domains,
        max_depth=max_depth,
        timeout=timeout,
        stats=stats,
    )
    for seed in seeds:
        crawler.enqueue(seed, 0)
    # The TaskGroup runs in its own task, not around our ``yield``: a generator
    # suspended at ``yield`` inside a TaskGroup can't be cancelled or closed
    # cleanly (PEP 789) — the group would see GeneratorExit as an error.
    pool = asyncio.create_task(crawler.run(), name="crawl-workers")
    try:
        while stats.pages < max_pages:
            if pool.done() and crawler.results.empty():
                pool.result()  # re-raises a crashed worker's error; else the frontier drained
                break
            getter = asyncio.ensure_future(crawler.results.get())
            await asyncio.wait({getter, pool}, return_when=asyncio.FIRST_COMPLETED)
            if not getter.done():
                getter.cancel()
                continue
            stats.pages += 1
            yield getter.result()
        log.info(
            "done: %d pages, %d requests, %d errors, %d left in the frontier, %.1f s",
            stats.pages, stats.requests, stats.errors, crawler.frontier.qsize(), stats.elapsed,
        )  # fmt: skip
    finally:  # budget reached, frontier empty, caller gone, or an error
        pool.cancel()
        await asyncio.wait({pool})  # the TaskGroup cancels and awaits every worker
        if own_client:
            await client.aclose()
