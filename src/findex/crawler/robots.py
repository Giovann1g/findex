"""``robots.txt``, fetched once per host and cached.

``await robots.allowed(url)`` answers for our User-Agent. The first call for a
host downloads ``scheme://host/robots.txt`` (through :func:`fetch`, so with a
timeout and retries); concurrent callers for the same host wait on one lock
instead of all downloading it. What the answer means, following Google's
rules for crawlers:

* 200 → parse it with :class:`urllib.robotparser.RobotFileParser`;
* 4xx (404: there is no robots.txt) → everything is allowed;
* 5xx, timeout, connection error → nothing is allowed for this host: the
  server can't tell us its rules right now, so we don't guess.

``crawl_delay(url)`` returns the host's ``Crawl-delay`` if it sets one; the
crawler uses it as that host's minimum pause when it is longer than its own.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

import httpx

from findex.crawler.fetch import USER_AGENT, fetch
from findex.crawler.urls import host_of

log = logging.getLogger("findex.crawl")


@dataclass(frozen=True, slots=True)
class _Rules:
    parser: RobotFileParser | None  # None: no parser, use ``allow_all``
    allow_all: bool


class RobotsCache:
    def __init__(
        self, client: httpx.AsyncClient, user_agent: str = USER_AGENT, timeout: float = 10.0
    ) -> None:
        self._client = client
        self._agent = user_agent.split("/", 1)[0]  # robots.txt groups match the product token
        self._timeout = timeout
        self._rules: dict[str, _Rules] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _rules_for(self, url: str) -> _Rules:
        host = host_of(url)
        if (rules := self._rules.get(host)) is not None:
            return rules
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:  # one download per host, however many workers ask
            if (rules := self._rules.get(host)) is not None:
                return rules
            result = await fetch(
                self._client, f"{host}/robots.txt", timeout=self._timeout, retries=1
            )
            if result.status == 200:
                parser = RobotFileParser()
                parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
                rules = _Rules(parser, allow_all=False)
                log.info("robots %s/robots.txt: 200, %d bytes", host, len(result.body))
            elif 400 <= result.status < 500:
                rules = _Rules(None, allow_all=True)
                log.info("robots %s/robots.txt: %d, everything allowed", host, result.status)
            else:
                rules = _Rules(None, allow_all=False)
                log.warning("robots %s/robots.txt unavailable (%s): host skipped", host,
                            result.error or result.status)  # fmt: skip
            self._rules[host] = rules
            return rules

    async def allowed(self, url: str) -> bool:
        rules = await self._rules_for(url)
        if rules.parser is None:
            return rules.allow_all
        return rules.parser.can_fetch(self._agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        rules = await self._rules_for(url)
        if rules.parser is None:
            return None
        delay = rules.parser.crawl_delay(self._agent)
        return float(delay) if delay is not None else None
