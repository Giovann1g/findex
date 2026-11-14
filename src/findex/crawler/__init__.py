"""Lab 6: a polite asyncio crawler that feeds the corpus (``findex crawl``).

* :mod:`~findex.crawler.fetch`   — one GET with timeout, backoff + jitter, Retry-After
* :mod:`~findex.crawler.robots`  — ``RobotsCache``: robots.txt per host, cached
* :mod:`~findex.crawler.urls`    — canonical URLs, domain allowlist
* :mod:`~findex.crawler.extract` — title, text, links (selectolax)
* :mod:`~findex.crawler.crawl`   — ``crawl()``: Queue frontier, TaskGroup workers
* :mod:`~findex.crawler.run`     — stream pages to JSON lines + ``crawl.log``
* :mod:`~findex.crawler.mirror`  — a local static server with latency and faults
* :mod:`~findex.crawler.bench`   — the concurrency 1 / 5 / 20 benchmark
* :mod:`~findex.crawler.inline_demo` — indexing inside the loop, caught by debug mode
"""

from findex.crawler.crawl import CrawlStats, Page, crawl
from findex.crawler.fetch import USER_AGENT, FetchResult, fetch
from findex.crawler.robots import RobotsCache
from findex.crawler.urls import canonicalize

__all__ = [
    "USER_AGENT",
    "CrawlStats",
    "FetchResult",
    "Page",
    "RobotsCache",
    "canonicalize",
    "crawl",
    "fetch",
]
