"""Lazy corpus loading.

``iter_documents(root)`` is a *generator*: it opens one file (or reads one
JSON line) at a time, yields a :class:`Document` and moves on. At any moment
only the current document's text is held in memory.

Supported inputs
----------------
* a directory tree of text files (``*.txt``, ``*.md``, ``*.rst``, also
  ``*.gz``-compressed variants) — walked lazily with ``Path.rglob``;
* a single JSON-lines file (``*.jsonl`` or ``*.jsonl.gz``), one document per
  line: ``{"id": ..., "text": ...}``;
* a single text file.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".rst"}


class Document(NamedTuple):
    doc_id: str  # stable id: path relative to the corpus root (or JSON "id")
    path: Path
    text: str


def _is_text_file(path: Path) -> bool:
    suffixes = path.suffixes
    if suffixes and suffixes[-1] == ".gz":
        suffixes = suffixes[:-1]
    return bool(suffixes) and suffixes[-1] in TEXT_SUFFIXES


def _is_jsonl(path: Path) -> bool:
    return path.name.endswith((".jsonl", ".jsonl.gz"))


def _read_text(path: Path) -> str | None:
    """Read one file as UTF-8. Bad bytes are replaced (and logged), not fatal."""
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        log.warning("skipping %s: %s", path, exc)
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        log.warning("bad UTF-8 in %s (%s); undecodable bytes replaced", path, exc)
        return raw.decode("utf-8", errors="replace")


def _iter_jsonl(path: Path) -> Iterator[Document]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, start=1):  # the file object is lazy
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("%s:%d: invalid JSON (%s), skipped", path, lineno, exc)
                continue
            doc_id = str(record.get("id", f"{path.name}:{lineno}"))
            yield Document(doc_id, path, str(record.get("text", "")))


def iter_documents(root: Path) -> Iterator[Document]:
    """Yield documents from ``root`` one at a time, never the whole corpus."""
    root = Path(root)
    if root.is_file():
        if _is_jsonl(root):
            yield from _iter_jsonl(root)
        else:
            text = _read_text(root)
            if text is not None:
                yield Document(root.name, root, text)
        return

    # rglob is itself a lazy generator over the tree. We deliberately do not
    # sort() it — that would materialize every path first. doc_id is the
    # relative path, so ids stay stable whatever the traversal order.
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_jsonl(path):
            yield from _iter_jsonl(path)
        elif _is_text_file(path):
            text = _read_text(path)
            if text is not None:
                yield Document(path.relative_to(root).as_posix(), path, text)
