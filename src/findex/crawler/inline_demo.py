"""Lab 6 experiment: what indexing *inside* the event loop does.

``python -m findex.crawler.inline_demo URL stream|inline`` crawls 300 pages
at concurrency 20 under ``asyncio.run(debug=True)``:

* ``stream`` — as ``findex crawl`` does: pages leave the coroutine at once;
* ``inline`` — keep the pages and build + pickle the index in the coroutine.

Debug mode reports any step that held the loop for more than 100 ms
(``Executing <Task …> took N seconds``) on the ``asyncio`` logger.
"""

from __future__ import annotations

import asyncio
import logging
import pickle
import sys
import time
from pathlib import Path

from findex.corpus import Document
from findex.crawler.crawl import CrawlStats, crawl
from findex.index import build_index


async def run(seed: str, mode: str) -> None:
    stats, docs = CrawlStats(), list[Document]()
    t0 = time.perf_counter()
    async for page in crawl(
        [seed], max_pages=300, concurrency=20, per_host=20, min_delay=0, stats=stats
    ):
        if mode == "inline":
            docs.append(Document(page.url, Path(page.url), page.text, page.title))
    t1 = time.perf_counter()
    if mode == "inline":
        pickle.dumps(build_index(docs, positions=True))  # CPU work on the loop's thread
    t2 = time.perf_counter()
    print(f"{mode}: crawl {t1 - t0:.2f} s, index inside the loop {t2 - t1:.2f} s", file=sys.stderr)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
    seed, mode = sys.argv[1], sys.argv[2]
    asyncio.run(run(seed, mode), debug=True)


if __name__ == "__main__":
    main()
