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

:func:`snippet_parts` returns ``(text, is_match)`` segments, so the same
snippet can be rendered as rich markup (CLI table), ``**md**`` or plain text.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from findex.corpus import find_jsonl_text, read_text
from findex.tokenize import tokenize

if TYPE_CHECKING:
    from findex.index import Index

# A word as the tokenizer sees it, but on *raw* text: letters/digits joined by
# an inner apostrophe or underscore, and possibly carrying combining marks.
SPAN_RE = re.compile(r"[^\W_]+(?:[̀-ͯ]+[^\W_]*|['’‘ʼ_][^\W_]+)*")

type Mark = Literal["ansi", "md", "brackets", "none"]
MARKS: dict[Mark, tuple[str, str]] = {
    "ansi": ("\x1b[1;33m", "\x1b[0m"),
    "md": ("**", "**"),
    "brackets": ("[", "]"),
    "none": ("", ""),
}

type Part = tuple[str, bool]  # (text, is_match)


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


def _squash(parts: list[Part]) -> list[Part]:
    """Collapse runs of whitespace across part boundaries into single spaces."""
    out: list[Part] = []
    prev_space = True  # also strips leading whitespace
    for text, hit in parts:
        squashed = re.sub(r"\s+", " ", text)
        if prev_space:
            squashed = squashed.lstrip()
        if squashed:
            out.append((squashed, hit))
            prev_space = squashed.endswith(" ")
    if out and out[-1][0].endswith(" "):
        text, hit = out[-1]
        out[-1] = (text.rstrip(), hit)
    return out


def snippet_parts(text: str, terms: Iterable[str], width: int = 80) -> list[Part]:
    """One line of context around the best match as ``(text, is_match)`` parts."""
    wanted = frozenset(terms)
    hits = [(s, e, tok) for tok, s, e in iter_spans(text) if tok in wanted]
    if not hits:  # e.g. a NOT-only query: show the beginning of the document
        head = " ".join(text[: 2 * width].split())
        return [(head + (" …" if len(text) > 2 * width else ""), False)]
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
    parts: list[Part] = [("… ", False)] if lo > 0 else []
    pos = lo
    body: list[Part] = []
    for s, e, _ in hits:
        if s < lo or e > hi:
            continue
        body += [(text[pos:s], False), (text[s:e], True)]
        pos = e
    body.append((text[pos:hi], False))
    parts += _squash(body)
    if hi < len(text):
        parts.append((" …", False))
    return parts


def make_snippet(text: str, terms: Iterable[str], width: int = 80, mark: Mark = "md") -> str:
    """The snippet as one string, matches wrapped in the chosen markers."""
    open_, close = MARKS[mark]
    return "".join(
        f"{open_}{t}{close}" if hit else t for t, hit in snippet_parts(text, terms, width)
    )


def document_text(index: Index, doc_id: int, root: str | Path | None = None) -> str | None:
    """Re-read a document for its snippet; ``None`` if the corpus isn't there."""
    if root is None:
        stored = index.meta.get("root")
        if not isinstance(stored, str):  # narrowing: meta values are `object`
            return None
        root = stored
    root = Path(root)
    stored_path = index.doc_meta[doc_id].path
    if root.is_file() and root.name.endswith((".jsonl", ".jsonl.gz")):
        return find_jsonl_text(root, stored_path)  # crawled pages: the id is the URL
    path = root / stored_path
    return read_text(path) if path.is_file() else None


# --- Lab 7–8: snippets for people (moved here from findex.web.service) ---------------
# reStructuredText markup that reads as noise in a one-line snippet
_RST_NOISE = [
    (re.compile(r"\.\. [\w:-]+::|\.\. _[\w.-]+:"), " "),  # .. directive::  /  .. _label:
    (re.compile(r"\.\. _?$"), " "),  # a label cut in two by a highlighted word
    (re.compile(r":[A-Za-z][\w:+.-]*:(?=`)"), ""),  # :func:`x`  ->  `x`
    (re.compile(r"`+_{0,2}"), ""),  # `x`  ``x``  `link`_
    (re.compile(r"\*\*|(?<![\w*])\*(?=\w)|(?<=\w)\*(?![\w*])"), ""),  # **bold**  *em*
    (re.compile(r"([=~^\-#*])\1{2,}"), " "),  # ===== section underlines
]


def _join_plain(parts: list[Part]) -> list[Part]:
    """Merge neighbouring non-match parts, so markup split across them is whole."""
    out: list[Part] = []
    for text, hit in parts:
        if out and not hit and not out[-1][1]:
            out[-1] = (out[-1][0] + text, False)
        else:
            out.append((text, hit))
    return out


def tidy_rst(parts: list[Part]) -> list[Part]:
    """Snippet parts without reST markup, for people rather than parsers."""
    # a matched word that is a role name, ":class:`x`", is markup, not a match
    unrolled: list[Part] = []
    for i, (text, hit) in enumerate(parts):
        nxt = parts[i + 1][0] if i + 1 < len(parts) else ""
        if hit and unrolled and unrolled[-1][0].endswith(":") and re.match(r":`", nxt):
            prev, _ = unrolled.pop()
            unrolled.append((prev + text, False))  # removed below with the rest of the role
            continue
        unrolled.append((text, hit))
    out: list[Part] = []
    for text, hit in _join_plain(unrolled):
        for pattern, repl in _RST_NOISE:
            text = pattern.sub(repl, text)
        text = re.sub(r"\s+", " ", text)
        if text.strip() or (text and out):
            out.append((text, hit))
    if out:
        out[0] = (out[0][0].lstrip(), out[0][1])
        out[-1] = (out[-1][0].rstrip(), out[-1][1])
    merged: list[Part] = []
    for text, hit in out:  # "… " + "  text" → one space
        if merged and merged[-1][0].endswith(" ") and text.startswith(" "):
            text = text.lstrip()
        if text:
            merged.append((text, hit))
    return merged


def fast_snippet(text: str, terms: frozenset[str], width: int = 80) -> list[Part]:
    """``snippet_parts``, but without tokenizing the whole document first.

    ``snippet_parts`` normalizes every word of the text to find the matches —
    about 99 % of a search request's CPU on long pages (a 200 KB page: 20 ms).
    Here one case-insensitive regex finds the candidate places, the densest
    window (most distinct terms within ``width``) is cut out, and only that
    window goes through ``snippet_parts``. When the regex finds nothing (a
    match only after Unicode normalization, or a NOT-only query) it falls
    back to the exact, slow path.
    """
    if not terms:
        return snippet_parts(text, terms, width)
    alternatives = "|".join(map(re.escape, sorted(terms, key=len, reverse=True)))
    # not inside a reST role name (":class:`…`"): those "matches" read as noise
    pattern = re.compile(rf"(?<![\w:])(?:{alternatives})(?![\w:])", re.IGNORECASE)
    hits = [(m.start(), m.end(), m.group().casefold()) for m in pattern.finditer(text)]
    if not hits:
        return snippet_parts(text, terms, width)
    best, best_key, lo_i = hits[0], (0, 0), 0
    for i, (start, _end, _term) in enumerate(hits):  # sliding window over the hits
        while hits[lo_i][0] < start - width:
            lo_i += 1
        hi_i = i
        while hi_i + 1 < len(hits) and hits[hi_i + 1][0] <= start + width:
            hi_i += 1
        key = (len({t for _, _, t in hits[lo_i : hi_i + 1]}), hi_i - lo_i)
        if key > best_key:
            best, best_key = hits[i], key
    lo = max(0, best[0] - 3 * width)
    hi = min(len(text), best[1] + 3 * width)
    parts = snippet_parts(text[lo:hi], terms, width)
    if lo > 0 and parts[:1] != [("… ", False)]:
        parts.insert(0, ("… ", False))
    if hi < len(text) and parts[-1:] != [(" …", False)]:
        parts.append((" …", False))
    return parts
