"""One polite HTTP GET: ``await fetch(client, url)`` → :class:`FetchResult`.

* Every attempt runs under ``asyncio.timeout`` (and httpx's own socket
  timeouts) — a stalled server can't hold a worker forever.
* **Retries only for transient failures**: 429, 5xx and timeouts, at most
  ``retries`` (3) times. The wait is exponential backoff with jitter,
  ``min(cap, base * 2**attempt) + uniform(0, base)``, so twenty workers that
  failed together don't come back together.
* A ``Retry-After`` header (seconds or an HTTP date) on 429/503 *replaces* the
  computed backoff when it asks for longer; a wait above ``max_retry_after``
  means "not today" and we give up instead of sleeping for an hour.
* Any other 4xx (404, 403, 410…) is final at once; so is a redirect loop
  (``httpx.TooManyRedirects``) or a refused connection.
* Bodies are streamed and cut at ``max_bytes``; only ``text/html`` is read.

Nothing here blocks: the only waits are ``await`` on the socket and
``await sleep(...)`` (``asyncio.sleep`` — injectable, so tests don't wait).
Every attempt is logged on ``findex.crawl``; the CLI writes that to ``crawl.log``.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple

import httpx

from findex import __version__

log = logging.getLogger("findex.crawl")

CONTACT = "https://github.com/Giovann1g/findex"
USER_AGENT = f"findex/{__version__} (student search-engine crawler; +{CONTACT})"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

type Sleep = Callable[[float], Awaitable[None]]
# called before a retry with (url, seconds): lets the crawler slow the whole host
type OnRetry = Callable[[str, float], None]


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str  # what was asked for
    final_url: str  # after redirects
    status: int  # HTTP status; 0 = no response (timeout, connection error)
    content_type: str
    body: bytes
    elapsed: float  # seconds, all attempts and waits included
    attempts: int
    error: str | None = None  # why it failed, for the log

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return self.content_type.startswith(("text/html", "application/xhtml"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """``Retry-After`` as seconds: ``"120"`` or ``"Wed, 21 Oct 2026 07:28:00 GMT"``."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


def backoff(attempt: int, base: float, cap: float, rng: Callable[[], float]) -> float:
    """Exponential backoff with jitter: ``min(cap, base·2^attempt) + U(0, base)``."""
    return min(cap, base * 2**attempt) + rng() * base


class _Reply(NamedTuple):
    status: int
    final_url: str
    content_type: str
    retry_after: str | None
    body: bytes


async def _get_once(client: httpx.AsyncClient, url: str, max_bytes: int) -> _Reply:
    """One request; the body is read only for text (HTML, robots.txt), up to ``max_bytes``."""
    async with client.stream("GET", url) as resp:
        ctype = resp.headers.get("content-type", "")
        chunks: list[bytes] = []
        if resp.status_code < 300 and ctype.startswith(("text/", "application/xhtml")):
            size = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    log.warning("%s: body cut at %d bytes", url, max_bytes)
                    break
        return _Reply(
            resp.status_code,
            str(resp.url),
            ctype,
            resp.headers.get("retry-after"),
            b"".join(chunks),
        )


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 10.0,
    retries: int = 3,
    backoff_base: float = 0.5,
    backoff_cap: float = 30.0,
    max_retry_after: float = 60.0,
    max_bytes: int = 5 * 2**20,
    sleep: Sleep = asyncio.sleep,
    rng: Callable[[], float] = random.random,
    on_retry: OnRetry | None = None,
) -> FetchResult:
    """GET ``url`` with a timeout per attempt and bounded, jittered retries."""
    t0 = time.perf_counter()
    attempt = 0
    while True:
        attempt += 1
        status, wait, error = 0, None, None
        final, ctype, body = url, "", b""
        try:
            async with asyncio.timeout(timeout):
                reply = await _get_once(client, url, max_bytes)
            status, final, ctype, body = (
                reply.status,
                reply.final_url,
                reply.content_type,
                reply.body,
            )
            if status in RETRY_STATUSES:
                error = f"HTTP {status}"
                wait = retry_after_seconds(reply.retry_after)
            elif status >= 400:
                error = f"HTTP {status}"  # 404 & co: final, never retried
        except TimeoutError:
            error = f"timeout after {timeout:g} s"
            status = 0
        except httpx.TimeoutException as exc:  # httpx's own connect/read timeout
            error = f"timeout ({type(exc).__name__})"
            status = 0
        except httpx.TooManyRedirects:
            error = "redirect loop (too many redirects)"
        except httpx.HTTPError as exc:  # connection refused/reset, protocol errors
            error = f"{type(exc).__name__}: {exc}"

        transient = status in RETRY_STATUSES or error is not None and error.startswith("timeout")
        if not transient or attempt > retries:
            if error and transient:
                error += f", gave up after {attempt} attempts"
            elapsed = time.perf_counter() - t0
            return FetchResult(url, final, status, ctype, body, elapsed, attempt, error)

        delay = backoff(attempt - 1, backoff_base, backoff_cap, rng)
        if wait is not None:
            if wait > max_retry_after:
                error = f"{error}: Retry-After {wait:.0f} s is too long, giving up"
                elapsed = time.perf_counter() - t0
                return FetchResult(url, final, status, ctype, body, elapsed, attempt, error)
            delay = max(delay, wait)  # the server's number wins when it asks for longer
        log.info(
            "retry %d/%d %s in %.2f s: %s%s",
            attempt, retries, url, delay, error,
            f" (Retry-After: {wait:g})" if wait is not None else "",
        )  # fmt: skip
        if on_retry is not None:
            on_retry(url, delay)
        await sleep(delay)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
