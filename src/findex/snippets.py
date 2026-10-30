"""Snippets: a ±80-character window around the best match, with terms marked.

Index positions are token *ordinals*, not character offsets, and the indexed
tokens are normalized (NFC, casefold, ...), so the original text is scanned
again: :func:`iter_spans` finds word-like spans in the *original* text and
normalizes each one with the corpus tokenizer. A span matches when its
normalized token is a query term — so ``STRASSE`` in the text lights up for
the query ``Straße``.

The *best* match is the one whose ±width neighbourhood contains the most
distinct query terms (then the most hits, then the earliest) — found in one
sliding-window pass over the hits.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from findex.corpus import _read_text
from findex.tokenize import tokenize

if TYPE_CHECKING:
    from findex.index import Index

# A word as the tokenizer sees it, but on *raw* text: letters/digits joined by
# an inner apostrophe or underscore, and possibly carrying combining marks.
SPAN_RE = re.compile(r"[^\W_]+(?:[̀-ͯ]+[^\W_]*|['’‘ʼ_][^\W_]+)*")

MARKS = {
    "ansi": ("\x1b[1;33m", "\x1b[0m"),
    "md": ("**", "**"),
    "brackets": ("[", "]"),
    "none": ("", ""),
}


def iter_spans(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield ``(normalized_token, start, end)`` with offsets into ``text``."""
    for m in SPAN_RE.finditer(text):
        for token in tokenize(m.group()):
            yield token, m.start(), m.end()


def _best_hit(hits: list[tuple[int, int, str]], width: int) -> int:
    counts: Counter[str] = Counter()
    lo = hi = 0
    best_i, best_key = 0, (-1, -1)
    for i, (start, _end, _term) in enumerate(hits):
        while hi < len(hits) and hits[hi][0] <= start + width:
            counts[hits[hi][2]] += 1
            hi += 1
        while hits[lo][0] < start - width:
            counts[hits[lo][2]] -= 1
            if not counts[hits[lo][2]]:
                del counts[hits[lo][2]]
            lo += 1
        key = (len(counts), hi - lo)  # distinct terms, then all hits nearby
        if key > best_key:  # strict: ties keep the earliest
            best_i, best_key = i, key
    return best_i


def make_snippet(text: str, terms: Iterable[str], width: int = 80, mark: str = "md") -> str:
    """One line of context around the best match, query terms marked."""
    terms = frozenset(terms)
    open_, close = MARKS[mark]
    hits = [(s, e, tok) for tok, s, e in iter_spans(text) if tok in terms]
    if not hits:  # e.g. a NOT-only query: show the beginning of the document
        head = " ".join(text[: 2 * width].split())
        return head + (" …" if len(text) > 2 * width else "")
    anchor_start, anchor_end, _ = hits[_best_hit(hits, width)]
    lo = max(0, anchor_start - width)
    hi = min(len(text), anchor_end + width)
    # don't cut a word in half (but don't wander off on huge "words" either)
    for _ in range(20):
        if lo == 0 or text[lo - 1].isspace():
            break
        lo -= 1
    for _ in range(20):
        if hi == len(text) or text[hi].isspace():
            break
        hi += 1
    parts, pos = [], lo
    for s, e, _ in hits:
        if s < lo or e > hi:
            continue
        parts += [text[pos:s], open_, text[s:e], close]
        pos = e
    parts.append(text[pos:hi])
    body = " ".join("".join(parts).split())  # newlines/indentation -> single spaces
    return ("… " if lo > 0 else "") + body + (" …" if hi < len(text) else "")


def document_text(index: Index, doc_id: int, root: str | Path | None = None) -> str | None:
    """Re-read a document for its snippet; ``None`` if the corpus isn't there."""
    root = root if root is not None else index.meta.get("root")
    if root is None:
        return None
    path = Path(root) / index.doc_meta[doc_id].path
    return _read_text(path) if path.is_file() else None
