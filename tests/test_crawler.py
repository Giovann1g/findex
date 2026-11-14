"""Lab 6: fetch, robots.txt, canonical URLs and the crawler — no live network.

HTTP goes through ``httpx.MockTransport`` (a function that answers requests in
memory); the CLI tests talk to ``findex.crawler.mirror`` on 127.0.0.1. Async
code runs under ``asyncio.run`` inside ordinary tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from findex.cli import app
from findex.corpus import iter_documents
from findex.crawler.crawl import CrawlStats, Page, crawl
from findex.crawler.fetch import USER_AGENT, backoff, fetch, retry_after_seconds
from findex.crawler.mirror import MirrorState, serve
from findex.crawler.robots import RobotsCache
from findex.crawler.run import crawl_to_jsonl
from findex.crawler.urls import canonicalize, in_domains, looks_like_page
from findex.index import build_index
from findex.snippets import document_text

runner = CliRunner()
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def client_for(handler: Handler, **kwargs: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        max_redirects=5,
        **kwargs,  # type: ignore[arg-type]
    )


def html(title: str, *links: str, body: str = "") -> httpx.Response:
    anchors = "".join(f'<a href="{href}">{href}</a> ' for href in links)
    page = f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
    page += f"<p>{body or title + ' text'}</p>{anchors}</main></body></html>"
    return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})


class Sleeps:
    """A fake ``asyncio.sleep`` that records waits instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def no_jitter() -> float:
    return 0.0


# --- fetch -----------------------------------------------------------------------
def test_fetch_ok_sends_an_honest_user_agent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return html("Hello")

    async def go() -> None:
        async with client_for(handler) as client:
            r = await fetch(client, "https://example.org/")
        assert r.ok and r.status == 200 and r.attempts == 1 and r.is_html
        assert "<title>Hello</title>" in r.text and r.elapsed >= 0

    asyncio.run(go())
    assert seen == [USER_AGENT] and "+https://github.com/" in USER_AGENT


def test_fetch_429_waits_for_retry_after() -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": "2"}), html("ok")])
    sleeps, pushed = Sleeps(), list[tuple[str, float]]()

    async def go() -> None:
        async with client_for(lambda _: next(answers)) as client:
            r = await fetch(client, "https://a.org/x", sleep=sleeps, rng=no_jitter,
                            on_retry=lambda url, s: pushed.append((url, s)))  # fmt: skip
        assert r.ok and r.attempts == 2

    asyncio.run(go())
    assert sleeps.calls == [2.0]  # the server's 2 s beat the 0.5 s backoff
    assert pushed == [("https://a.org/x", 2.0)]  # the crawler hears about it


def test_fetch_5xx_backs_off_exponentially_then_gives_up() -> None:
    calls = Counter[str]()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    sleeps = Sleeps()

    async def go() -> None:
        async with client_for(handler) as client:
            r = await fetch(client, "https://a.org/", sleep=sleeps, rng=no_jitter)
        assert not r.ok and r.status == 503 and r.attempts == 4
        assert r.error is not None and "gave up after 4 attempts" in r.error

    asyncio.run(go())
    assert calls["n"] == 4  # 1 try + at most 3 retries
    assert sleeps.calls == [0.5, 1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_fetch_never_retries_other_4xx(status: int) -> None:
    sleeps = Sleeps()

    async def go() -> None:
        async with client_for(lambda _: httpx.Response(status)) as client:
            r = await fetch(client, "https://a.org/missing", sleep=sleeps)
        assert r.status == status and r.attempts == 1 and r.error == f"HTTP {status}"

    asyncio.run(go())
    assert sleeps.calls == []


def test_fetch_timeout_is_retried() -> None:
    answers = iter(["slow", "fast"])

    async def handler(request: httpx.Request) -> httpx.Response:
        if next(answers) == "slow":
            await asyncio.sleep(5)  # longer than the timeout: cancelled
        return html("finally")

    sleeps = Sleeps()

    async def go() -> None:
        async with client_for(handler) as client:
            t0 = time.perf_counter()
            r = await fetch(client, "https://a.org/", timeout=0.05, sleep=sleeps, rng=no_jitter)
            assert time.perf_counter() - t0 < 2
        assert r.ok and r.attempts == 2 and sleeps.calls == [0.5]

    asyncio.run(go())


def test_fetch_gives_up_on_a_huge_retry_after() -> None:
    async def go() -> None:
        reply = httpx.Response(429, headers={"Retry-After": "3600"})
        async with client_for(lambda _: reply) as client:
            r = await fetch(client, "https://a.org/", sleep=Sleeps())
        assert r.attempts == 1 and r.error is not None and "too long" in r.error

    asyncio.run(go())


def test_fetch_reports_a_redirect_loop_without_retrying() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        nxt = "/b" if request.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"Location": nxt})

    async def go() -> None:
        async with client_for(handler) as client:
            r = await fetch(client, "https://a.org/a", sleep=Sleeps())
        assert r.status == 0 and r.attempts == 1 and r.error is not None
        assert "redirect loop" in r.error

    asyncio.run(go())


def test_fetch_connection_error_is_final() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def go() -> None:
        async with client_for(handler) as client:
            r = await fetch(client, "https://a.org/", sleep=Sleeps())
        assert r.status == 0 and r.attempts == 1 and r.error is not None
        assert r.error.startswith("ConnectError")

    asyncio.run(go())


def test_retry_after_parsing_and_backoff_bounds() -> None:
    assert retry_after_seconds("120") == 120
    assert retry_after_seconds(None) is None and retry_after_seconds("soon") is None
    when = "Wed, 21 Oct 2026 07:28:00 GMT"
    now = retry_after_seconds(when, now=0.0)
    assert now is not None and now > 1e9  # an absolute date, far after the epoch
    assert retry_after_seconds(when, now=4e9) == 0.0  # already past: retry now
    for attempt in range(8):
        d = backoff(attempt, 0.5, 30.0, lambda: 0.999)
        assert min(30.0, 0.5 * 2**attempt) <= d < min(30.0, 0.5 * 2**attempt) + 0.5


# --- robots.txt ------------------------------------------------------------------
ROBOTS = {
    "a.org": "User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n",
    # a group naming us wins over "*": here /private/ is fine, /no-findex/ is not
    "b.org": "User-agent: *\nDisallow: /private/\n\nUser-agent: findex\nDisallow: /no-findex/\n",
}


def test_robots_cache_fetches_once_per_host_and_obeys() -> None:
    hits = Counter[str]()

    async def handler(request: httpx.Request) -> httpx.Response:
        hits[f"{request.url.host}{request.url.path}"] += 1
        await asyncio.sleep(0.01)  # everyone asks while the first download is in flight
        text = ROBOTS[request.url.host]
        return httpx.Response(200, text=text, headers={"content-type": "text/plain"})

    async def go() -> None:
        async with client_for(handler) as client:
            robots = RobotsCache(client)
            answers = await asyncio.gather(
                *(robots.allowed(f"https://a.org/page{i}") for i in range(10))
            )
            assert all(answers)
            assert not await robots.allowed("https://a.org/private/x")
            assert await robots.crawl_delay("https://a.org/") == 2.0
            assert await robots.allowed("https://b.org/private/x")
            assert not await robots.allowed("https://b.org/no-findex/x")
            assert await robots.crawl_delay("https://b.org/") is None

    asyncio.run(go())
    assert hits == {"a.org/robots.txt": 1, "b.org/robots.txt": 1}


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (410, True), (500, False)])
def test_robots_missing_allows_but_broken_denies(status: int, allowed: bool) -> None:
    async def go() -> None:
        reply = httpx.Response(status)
        async with client_for(lambda _: reply) as client:
            robots = RobotsCache(client)
            robots._timeout = 1  # pyright: ignore[reportPrivateUsage]
            assert await robots.allowed("https://a.org/x") is allowed

    asyncio.run(go())


# --- canonical URLs --------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("HTTP://Docs.Python.org:80/3/Library/", "http://docs.python.org/3/Library"),
        ("https://a.org:443", "https://a.org/"),
        ("https://a.org/x/#section", "https://a.org/x"),
        ("https://a.org/a/./b/../c", "https://a.org/a/c"),
        ("https://a.org/s?b=2&a=1&a=0", "https://a.org/s?a=0&a=1&b=2"),
        ("https://a.org:8080//x", "https://a.org:8080/x"),
        ("mailto:me@a.org", None),
        ("javascript:void(0)", None),
        ("https://[::1]:99/", "https://[::1]:99/"),
        ("http://a.org:bad/", None),
    ],
)
def test_canonicalize(raw: str, canonical: str | None) -> None:
    assert canonicalize(raw) == canonical


def test_canonicalize_resolves_relative_links_and_filters() -> None:
    assert canonicalize("../b.html#x", "https://a.org/dir/page.html") == "https://a.org/b.html"
    assert in_domains("https://docs.a.org/x", frozenset({"a.org"}))
    assert not in_domains("https://evil-a.org/x", frozenset({"a.org"}))
    assert not looks_like_page("https://a.org/logo.PNG") and looks_like_page("https://a.org/x")


# --- the crawler -----------------------------------------------------------------
def site_handler(
    pages: dict[str, Callable[[], httpx.Response]], log: Counter[str] | None = None
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log[f"{request.url.host}{request.url.raw_path.decode()}"] += 1
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /secret\n")
        make = pages.get(request.url.path)
        return make() if make else httpx.Response(404)

    return handler


def small_site() -> dict[str, Callable[[], httpx.Response]]:
    return {
        "/": lambda: html(
            "Home",
            "/a",
            "/a/",
            "/a#top",
            "/b?y=2&x=1",
            "/b?x=1&y=2",
            "HTTPS://SITE.ORG/c",
            "/secret/plans",
            "https://elsewhere.org/",
            "/logo.png",
            "mailto:x@site.org",
        ),
        "/a": lambda: html("A", "/", "/b?x=1&y=2", "/missing"),
        "/b": lambda: html("B", "/a", "/c"),
        "/c": lambda: html("C", "/c/d"),
        "/c/d": lambda: httpx.Response(200, text="plain", headers={"content-type": "text/plain"}),
    }


async def collect(seed: str, handler: Handler, **kwargs: object) -> tuple[list[Page], CrawlStats]:
    stats = CrawlStats()
    async with client_for(handler) as client:
        pages = [
            p
            async for p in crawl(
                [seed],
                client=client,
                stats=stats,
                min_delay=0,
                **kwargs,  # type: ignore[arg-type]
            )
        ]
    return pages, stats


def test_crawl_fetches_each_page_once_and_stays_in_bounds() -> None:
    log = Counter[str]()
    pages, stats = asyncio.run(
        collect("https://site.org/", site_handler(small_site(), log), concurrency=4)
    )
    assert sorted(p.title for p in pages) == ["A", "B", "C", "Home"]
    assert sorted(p.url for p in pages) == [
        "https://site.org/",
        "https://site.org/a",
        "https://site.org/b?x=1&y=2",
        "https://site.org/c",
    ]
    fetched = {k: v for k, v in log.items() if not k.endswith("robots.txt")}
    assert all(n == 1 for n in fetched.values()), fetched  # no URL twice
    assert not any("secret" in k or "elsewhere" in k or "logo" in k for k in log)
    assert stats.robots_blocked == 1 and stats.errors == 1  # /missing → 404
    assert stats.skipped == 1  # /c/d is text/plain
    home = next(p for p in pages if p.title == "Home")
    assert home.depth == 0 and "Home text" in home.text and home.fetched_at.endswith("+00:00")


def infinite_site() -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        n = int(request.url.path.strip("/") or 0)
        return html(f"page {n}", *(f"/{n * 3 + k}" for k in (1, 2, 3)))

    return handler


def test_crawl_stops_at_max_pages_and_cleans_up() -> None:
    async def go() -> None:
        pages, stats = await collect("https://x.org/", infinite_site(), max_pages=25, concurrency=8)
        assert len(pages) == 25 == stats.pages and len({p.url for p in pages}) == 25
        assert stats.requests == 25  # the budget covers fetches under way: none wasted
        assert asyncio.all_tasks() == {asyncio.current_task()}  # no worker left behind

    asyncio.run(go())


def test_breaking_out_early_cancels_workers() -> None:
    async def go() -> None:
        async with client_for(infinite_site()) as client:
            agen = crawl(["https://x.org/"], client=client, min_delay=0, concurrency=5)
            async for _ in agen:
                break
            await agen.aclose()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(go())


def test_crawl_ends_when_the_frontier_is_empty() -> None:
    pages, _ = asyncio.run(collect("https://site.org/", site_handler(small_site()), max_pages=1000))
    assert len(pages) == 4  # finished by exhaustion, long before the budget


def concurrency_probe(delay: float = 0.02) -> tuple[Handler, dict[str, int]]:
    state = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(delay)
        state["now"] -= 1
        n = int(request.url.path.strip("/") or 0)
        return html(f"p{n}", *(f"/{n * 4 + k}" for k in (1, 2, 3, 4)))

    return handler, state


@pytest.mark.parametrize(
    ("concurrency", "per_host", "limit"), [(10, 2, 2), (10, 10, 10), (3, 10, 3)]
)
def test_semaphores_bound_requests_in_flight(concurrency: int, per_host: int, limit: int) -> None:
    handler, state = concurrency_probe()
    asyncio.run(
        collect("https://x.org/", handler, max_pages=40, concurrency=concurrency, per_host=per_host)
    )
    assert state["peak"] == limit  # reached, never exceeded


def test_min_delay_spaces_requests_to_one_host() -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        starts.append(time.perf_counter())
        n = int(request.url.path.strip("/") or 0)
        return html(f"p{n}", f"/{n + 1}", f"/{n + 2}")

    async def go() -> None:
        async with client_for(handler) as client:
            agen = crawl(
                ["https://x.org/"], client=client, min_delay=0.05, concurrency=5, per_host=5,
                max_pages=6,
            )  # fmt: skip
            assert len([p async for p in agen]) == 6

    asyncio.run(go())
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert gaps and min(gaps) >= 0.045


def test_a_429_holds_back_the_whole_host() -> None:
    starts: dict[str, float] = {}
    first = {"done": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/1" and not first["done"]:
            first["done"] = True
            return httpx.Response(429, headers={"Retry-After": "1"})
        starts.setdefault(path, time.perf_counter())
        if path == "/":
            return html("root", "/1", "/2", "/3")
        return html(path)

    t0 = time.perf_counter()
    pages, stats = asyncio.run(
        collect("https://x.org/", handler, concurrency=1, per_host=1, max_pages=4)
    )
    assert len(pages) == 4 and stats.retries == 1
    assert starts["/1"] - t0 >= 0.9  # waited out Retry-After before trying again


def test_jsonl_from_a_crawl_is_a_corpus(tmp_path: Path) -> None:
    out = tmp_path / "crawl.jsonl"

    async def go() -> CrawlStats:
        async with client_for(site_handler(small_site())) as client:
            # crawl_to_jsonl opens its own client; drive crawl() the same way here
            stats = CrawlStats()
            with out.open("w", encoding="utf-8") as f:
                async for page in crawl(["https://site.org/"], client=client, min_delay=0,
                                        stats=stats):  # fmt: skip
                    f.write(json.dumps({"id": page.url, "title": page.title, "text": page.text}))
                    f.write("\n")
            return stats

    asyncio.run(go())
    docs = list(iter_documents(out))
    assert {d.title for d in docs} == {"Home", "A", "B", "C"}
    ix = build_index(iter_documents(out))
    assert {m.title for m in ix.doc_meta.values()} == {"Home", "A", "B", "C"}
    ix.meta["root"] = str(out)
    b = next(i for i, m in ix.doc_meta.items() if m.title == "B")
    text = document_text(ix, b)
    assert text is not None and text.startswith("B\nB text")


# --- mirror + CLI (real sockets, but only to 127.0.0.1) ---------------------------
@pytest.fixture
def mirror(tmp_path: Path) -> Iterator[str]:
    site = tmp_path / "site"
    (site / "lib").mkdir(parents=True)
    links = "".join(f'<a href="lib/p{i}.html">p{i}</a>' for i in range(12))
    (site / "index.html").write_text(
        f"<html><head><title>Mirror home</title></head><body><main>{links}</main></body></html>"
    )
    for i in range(12):
        body = f"Page {i} talks about the event loop and coroutine number {i}."
        nxt = f'<a href="p{(i + 1) % 12}.html#x">next</a>'
        (site / "lib" / f"p{i}.html").write_text(
            f"<html><head><title>Page {i}</title></head><body><main><h1>Page {i}</h1>"
            f"<p>{body}</p>{nxt}</main></body></html>"
        )
    state = MirrorState(latency=5, jitter=2, faults=True, hang=3)
    server = serve(site, 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def test_crawl_to_jsonl_against_the_mirror(mirror: str, tmp_path: Path) -> None:
    out = tmp_path / "c.jsonl"
    stats = asyncio.run(
        crawl_to_jsonl(
            [mirror], out, max_pages=50, concurrency=5, per_host=5, min_delay=0, timeout=2.0
        )  # fmt: skip
    )
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert stats.pages == len(lines) == 13  # home + 12 pages, each once
    assert {line["title"] for line in lines} >= {"Mirror home", "Page 0", "Page 11"}
    assert stats.errors == 1  # the redirect-loop link the mirror adds to its home page


@pytest.mark.slow
def test_cli_crawl_index_search(mirror: str, tmp_path: Path) -> None:
    out, log, ix = tmp_path / "crawl.jsonl", tmp_path / "crawl.log", tmp_path / "ix.pkl"
    argv = ["crawl", mirror, "-o", str(out), "--log", str(log), "--max-pages", "8"]
    result = runner.invoke(app, [*argv, "-c", "4", "--per-host", "4", "--delay", "0", "--debug"])
    assert result.exit_code == 0, result.output
    stderr = " ".join(result.stderr.split())
    assert "8 pages" in stderr and "0 callbacks blocked the loop" in stderr
    assert len(out.read_text().splitlines()) == 8
    text = log.read_text()
    assert "user-agent: findex/" in text and "robots.txt: 200" in text and " 200 http" in text

    assert runner.invoke(app, ["index", str(out), "-o", str(ix), "--positions"]).exit_code == 0
    found = runner.invoke(app, ["search", str(ix), '"event loop"', "--json"])
    assert found.exit_code == 0, found.output
    rows = [json.loads(line) for line in found.stdout.splitlines()]
    assert rows and rows[0]["title"].startswith("Page") and "event loop" in rows[0]["snippet"]


def test_cli_crawl_rejects_bad_input(tmp_path: Path) -> None:
    bad = runner.invoke(app, ["crawl", "ftp://x.org/", "-o", str(tmp_path / "x.jsonl")])
    assert bad.exit_code == 1 and "not an http(s) URL" in bad.stderr
    nodir = runner.invoke(app, ["crawl", "http://x.org/", "-o", str(tmp_path / "no" / "x.jsonl")])
    assert nodir.exit_code == 1 and "output directory does not exist" in nodir.stderr


def test_an_unexpected_error_on_one_page_is_logged_not_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/bad":
            raise ValueError("a bug in somebody's transport")
        return html("home", "/bad", "/good") if request.url.path == "/" else html("good")

    pages, stats = asyncio.run(collect("https://x.org/", handler))
    assert sorted(p.title for p in pages) == ["good", "home"] and stats.errors == 1
