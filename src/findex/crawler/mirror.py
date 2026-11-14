"""A local mirror to crawl: ``python -m findex.crawler.mirror DIR --port 8000``.

Serves a directory of static HTML (here: the CPython documentation, built
with Sphinx — the same pages as docs.python.org) the way a real server on the
internet would behave, so the crawler can be benchmarked and broken on purpose
without bothering anyone:

* **latency**: every response waits ``--latency`` ms ± ``--jitter`` ms first
  (a thread per connection, so waits overlap — like a real server's);
* **robots.txt** that disallows the parts a search engine shouldn't index
  (``/_sources/``, ``/_static/``, the generated ``genindex`` pages, search);
* **faults** (``--faults``, on by default), each hit once per path, chosen by a
  hash of the path so every run meets the same ones:
  2 % of pages answer the first request with ``429`` + ``Retry-After: 1``,
  2 % with ``503``, 1 % stall for ``--hang`` s (longer than the crawler's
  timeout); and the root page gets one extra link into a redirect loop
  (``/loop/a`` → ``/loop/b`` → ``/loop/a`` …).

Standard library only (``http.server.ThreadingHTTPServer``); HTTP/1.1 with
keep-alive, so the crawler's connection pool is exercised too.
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROBOTS_TXT = b"""# findex local mirror
User-agent: *
Disallow: /_sources/
Disallow: /_static/
Disallow: /_images/
Disallow: /_downloads/
Disallow: /genindex
Disallow: /search.html
Disallow: /py-modindex.html
"""

LOOP_LINK = b'<p><a href="/loop/a">(test link: a redirect loop)</a></p>'


class MirrorState:
    def __init__(self, latency: float, jitter: float, faults: bool, hang: float) -> None:
        self.latency = latency
        self.jitter = jitter
        self.faults = faults
        self.hang = hang
        self.hit: set[tuple[str, str]] = set()  # (fault, path) already served once
        self.lock = threading.Lock()
        self.rng = random.Random(7)
        self.requests = 0

    def first_time(self, fault: str, path: str) -> bool:
        with self.lock:  # handler threads run truly concurrently with this
            if (fault, path) in self.hit:
                return False
            self.hit.add((fault, path))
            return True

    def fault_for(self, path: str) -> str | None:
        if not self.faults or not path.endswith(".html"):
            return None
        bucket = zlib.crc32(path.encode()) % 100
        if bucket < 2:
            return "429"
        if bucket < 4:
            return "503"
        if bucket < 5:
            return "hang"
        return None

    def delay(self) -> float:
        with self.lock:
            j = self.rng.uniform(-self.jitter, self.jitter)
        return max(0.0, self.latency + j) / 1000


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    state: MirrorState

    def __init__(self, *args: Any, state: MirrorState, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib's name
        pass  # quiet: the crawler keeps its own log

    def _send(
        self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        st = self.state
        with st.lock:
            st.requests += 1
        time.sleep(st.delay())  # the "network": a handler thread waits, others don't
        path = self.path.split("?", 1)[0]
        if path == "/robots.txt":
            return self._send(200, ROBOTS_TXT, "text/plain; charset=utf-8")
        if path in ("/loop/a", "/loop/b"):
            target = "/loop/b" if path == "/loop/a" else "/loop/a"
            return self._send(302, b"", "text/plain", {"Location": target})
        fault = st.fault_for(path)
        if fault and st.first_time(fault, path):
            if fault == "429":
                body = b"slow down"
                return self._send(429, body, "text/plain", {"Retry-After": "1"})
            if fault == "503":
                return self._send(503, b"try again", "text/plain")
            time.sleep(st.hang)  # "hang": the client gives up first
        if st.faults and path in ("/", "/index.html"):
            index = Path(self.translate_path("/index.html"))
            html = index.read_bytes().replace(b"</body>", LOOP_LINK + b"</body>", 1)
            return self._send(200, html, "text/html; charset=utf-8")
        return super().do_GET()


class MirrorServer(ThreadingHTTPServer):
    # listen() backlog: the default 5 drops a burst of 20 simultaneous connects,
    # and the client's SYN retry costs a whole second. Real servers use ~128+.
    request_queue_size = 128
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that hung up mid-response (it timed out, or the crawl hit
        # its page budget and cancelled) is normal here, not a server error.
        exc = sys.exc_info()[1]
        if not isinstance(exc, ConnectionError):
            super().handle_error(request, client_address)


def serve(root: Path, port: int, state: MirrorState) -> ThreadingHTTPServer:
    handler = partial(Handler, directory=str(root), state=state)
    server = MirrorServer(("127.0.0.1", port), handler)
    return server


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.crawler.mirror")
    p.add_argument("root", type=Path, help="directory of HTML pages (e.g. a Sphinx build)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--latency", type=float, default=80.0, help="ms per response")
    p.add_argument("--jitter", type=float, default=40.0, help="± ms")
    p.add_argument("--hang", type=float, default=15.0, help="seconds a 'hang' fault stalls")
    p.add_argument("--no-faults", dest="faults", action="store_false")
    args = p.parse_args(argv)
    state = MirrorState(args.latency, args.jitter, args.faults, args.hang)
    server = serve(args.root, args.port, state)
    print(
        f"mirror of {args.root} on http://127.0.0.1:{args.port}/ — latency "
        f"{args.latency:g}±{args.jitter:g} ms, faults {'on' if args.faults else 'off'}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
