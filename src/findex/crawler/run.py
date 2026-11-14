"""Crawl to a JSON-lines file, one line per page *as it arrives*.

Each line is ``{"id": url, "title": …, "text": …, "url": …, "fetched_at": …,
"status": …, "depth": …}`` — the format :func:`findex.corpus.iter_documents`
reads, so ``findex index data/crawl.jsonl`` indexes it afterwards, *outside*
the event loop. Nothing is buffered: a page is written (and flushed) and then
dropped, so memory stays flat however many pages the crawl collects.

A small write per page is the only file I/O inside the loop; it costs
microseconds (the OS buffers it), well under anything that would stall the
other tasks. Anything bigger would go through ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from findex.crawler.crawl import CrawlStats, Page, crawl

log = logging.getLogger("findex.crawl")


def page_record(page: Page) -> dict[str, object]:
    return {
        "id": page.url,
        "title": page.title,
        "text": page.text,
        "url": page.url,
        "fetched_at": page.fetched_at,
        "status": page.status,
        "depth": page.depth,
    }


async def crawl_to_jsonl(
    seeds: Iterable[str],
    out: Path,
    *,
    stats: CrawlStats | None = None,
    on_page: Callable[[Page], None] | None = None,
    max_pages: int = 500,
    concurrency: int = 10,
    per_host: int = 2,
    min_delay: float = 0.5,
    allowed_domains: Iterable[str] | None = None,
    max_depth: int | None = None,
    timeout: float = 10.0,
) -> CrawlStats:
    stats = stats if stats is not None else CrawlStats()
    with out.open("w", encoding="utf-8") as f:
        async for page in crawl(
            seeds,
            max_pages=max_pages,
            concurrency=concurrency,
            per_host=per_host,
            min_delay=min_delay,
            allowed_domains=allowed_domains,
            max_depth=max_depth,
            timeout=timeout,
            stats=stats,
        ):
            f.write(json.dumps(page_record(page), ensure_ascii=False) + "\n")
            f.flush()  # visible to `tail -f` and safe on Ctrl-C
            if on_page is not None:
                on_page(page)
    return stats
