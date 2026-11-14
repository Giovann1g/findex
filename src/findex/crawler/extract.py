"""Title, text and links out of an HTML page, with selectolax (lexbor, in C).

Parsing is CPU work done *inside* the event loop, so it has to be short:
selectolax handles a 174 KB documentation page in ~6 ms (``html.parser``:
~17 ms) and the 2 MB ``genindex-all`` in ~56 ms (``html.parser``: ~158 ms,
over asyncio's 100 ms "slow callback" threshold that ``findex crawl --debug``
watches). At 20 pages in flight, every one of those milliseconds is a
millisecond in which no socket is serviced.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from selectolax.lexbor import LexborHTMLParser

# Not page text: code that runs, styling, navigation chrome repeated on every page.
_DROP = "script, style, noscript, template, svg, nav, header, footer, form"
# Where the article lives on most sites (Sphinx: div[role=main] / div.body).
_MAIN = ("main", "article", "[role=main]", "div.body", "#content", "body")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


class Extracted(NamedTuple):
    title: str
    text: str
    links: list[str]  # raw href values, in page order; resolve them yourself
    nofollow: bool  # <meta name="robots" content="nofollow">


def extract(html: str) -> Extracted:
    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    title = " ".join(title_node.text().split()) if title_node is not None else ""
    robots_meta = tree.css_first('meta[name="robots"]')
    directives = (robots_meta.attributes.get("content") or "") if robots_meta is not None else ""
    nofollow = "nofollow" in directives.lower()

    links = [href for a in tree.css("a[href]") if (href := a.attributes.get("href"))]

    root = next((n for sel in _MAIN if (n := tree.css_first(sel)) is not None), None)
    text = ""
    if root is not None:
        for node in root.css(_DROP):
            node.decompose()
        text = root.text(separator="\n")
        text = _SPACES.sub(" ", text)
        text = _BLANK_LINES.sub("\n\n", "\n".join(line.strip() for line in text.split("\n")))
    return Extracted(title, text.strip(), links, nofollow)
