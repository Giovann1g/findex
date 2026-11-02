"""Streaming tokenizer.

``tokenize(text)`` is a generator built on ``re.finditer``: tokens are produced
one at a time, no list of all matches is ever built.

Normalization (in this order)
-----------------------------
1. ``unicodedata.normalize("NFC", text)`` — ``e`` + U+0301 and ``é`` (U+00E9)
   become the same code point, so they become the same token.
2. ``str.casefold()`` — aggressive caseless matching (``Straße`` -> ``strasse``).
3. Typographic apostrophes ``’ ‘ ʼ ``` `` are mapped to ASCII ``'``; the soft
   hyphen U+00AD is removed.
4. Combining marks that survive NFC (e.g. the Ukrainian stress mark in
   ``мо́ва`` — there is no precomposed Cyrillic "о with acute") are dropped,
   otherwise ``\\w`` would split the word in two. Letters such as ``й``/``ї``
   are precomposed after NFC and are *not* affected.

Token rules
-----------
* A token is a run of Unicode letters/digits (``[^\\W_]`` — ``\\w`` without
  the underscore), so Latin, Cyrillic, Greek, CJK all work.
* **Apostrophes**: kept *inside* a word (``don't``, ``п'ять``, ``м'ята``);
  leading/trailing quotes are not part of the token (``'hello'`` -> ``hello``).
* **Underscores**: kept *inside* a word, so Python identifiers survive
  (``sys_path`` -> ``sys_path``); dunder underscores are stripped
  (``__init__`` -> ``init``).
* **Hyphens** split words: ``state-of-the-art`` -> ``state``, ``of``, ``the``,
  ``art``; ``Івано-Франківськ`` -> ``івано``, ``франківськ``. A query for any
  part then matches.
* **Digits** are tokens: ``2026`` -> ``2026``; mixed alphanumerics stay whole
  (``utf8``, ``python3``); a dot splits numbers (``3.13`` -> ``3``, ``13``).
* Everything else (punctuation, whitespace, symbols) is a separator.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Iterator

WORD_RE = re.compile(r"[^\W_]+(?:['_][^\W_]+)*")

_APOSTROPHES: dict[int, str | None] = {ord(c): "'" for c in "’‘ʼ`"}
_APOSTROPHES[0x00AD] = None  # soft hyphen


def _combining_class() -> re.Pattern[str]:
    """A regex matching every combining mark (built once, at import)."""
    marks = "".join(
        re.escape(chr(cp))
        for cp in range(0x0300, min(sys.maxunicode, 0xE01EF) + 1)
        if unicodedata.combining(chr(cp))
    )
    return re.compile(f"[{marks}]+")


_COMBINING_RE = _combining_class()


def normalize(text: str) -> str:
    """Apply normalization steps 1–4 of the module docstring."""
    text = unicodedata.normalize("NFC", text).casefold()
    if not text.isascii():
        text = text.translate(_APOSTROPHES)
        # after casefold: "İ".casefold() == "i̇", the mark is dropped too
        text = _COMBINING_RE.sub("", text)
    return text


def tokenize(text: str) -> Iterator[str]:
    """Yield normalized tokens of ``text`` lazily (one ``re`` match at a time)."""
    for match in WORD_RE.finditer(normalize(text)):
        yield match.group()
