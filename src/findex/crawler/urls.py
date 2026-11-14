"""URL canonicalization: one spelling per page, so ``seen`` catches duplicates.

``canonicalize`` maps every way of writing the same address to one string:

* scheme and host lowercased, default port dropped (``HTTP://Docs.Python.org:80/``
  → ``http://docs.python.org/``);
* the ``#fragment`` dropped — it names a place *inside* a page, not a page;
* the path: empty → ``/``, ``.``/``..`` resolved, and a trailing slash removed
  (``/library/`` and ``/library`` are one entry; the root stays ``/``);
* the query: parameters sorted, so ``?b=2&a=1`` equals ``?a=1&b=2``.

Only ``http``/``https`` URLs survive; ``mailto:``, ``javascript:`` and the like
become ``None``. Relative links are resolved against the page they came from
(the *final* URL after redirects, never the canonical one).
"""

from __future__ import annotations

import posixpath
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

# Links we never fetch: not pages, or not text a search engine indexes.
SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".css", ".js", ".map",
    ".zip", ".gz", ".bz2", ".xz", ".tar", ".tgz", ".pdf", ".epub", ".exe", ".msi",
    ".dmg", ".whl", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".webm", ".json", ".xml",
)  # fmt: skip


def canonicalize(url: str, base: str | None = None) -> str | None:
    """The canonical form of ``url`` (resolved against ``base``), or ``None``."""
    url = url.strip()
    if base is not None:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:  # e.g. "http://[::1" or a non-numeric port
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = parts.path or "/"
    path = posixpath.normpath(path)
    if path.startswith("//"):  # normpath keeps a leading "//"
        path = "/" + path.lstrip("/")
    if path in (".", ""):
        path = "/"
    path = path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    """``scheme://host[:port]`` — the unit of politeness (robots.txt, limits)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def in_domains(url: str, domains: frozenset[str]) -> bool:
    """Is the host one of ``domains`` or a subdomain of one?"""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def looks_like_page(url: str) -> bool:
    """Cheap filter before a request: skip images, archives, scripts, …"""
    return not urlsplit(url).path.lower().endswith(SKIP_SUFFIXES)
