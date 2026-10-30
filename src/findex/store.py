"""Saving and loading the index in three formats.

* ``pickle`` (``.pkl``) — the whole ``Index`` object graph, any layout.

  .. warning::
     **Never ``pickle.load`` a file you did not create yourself.** A pickle is
     not data, it is a small program for the pickle virtual machine: its
     ``REDUCE`` opcode calls any importable callable with any arguments, so a
     crafted file can run ``os.system("...")`` the moment it is loaded. Treat
     ``.pkl`` files like executables. The two formats below are safe to load.

* ``json`` (``.json``) — human-readable and safe; postings as ``[doc_id, tf]``
  pairs (plus positions if present). Loads into the ``slots`` layout.
* ``bin`` (``.bin``) — our own compact binary format built on ``array('I')``::

      b"FIDX" | version:u8 | header_len:u32le | header (JSON, UTF-8) | body

  ``header["terms"]`` lists ``[term, offset, count]``; the body is all doc_ids
  of all terms concatenated as little-endian uint32, followed by all tfs in
  the same order. ``load(path)`` reads it into the ``array`` layout;
  ``load(path, lazy=True)`` instead ``mmap``-s the file and decodes a term's
  postings only when a query asks for it (load time ≈ header parse only).
"""

from __future__ import annotations

import json
import mmap
import pickle
import struct
import sys
from array import array
from collections.abc import Iterator, Mapping
from pathlib import Path

from findex.index import DocMeta, Index, Posting

FORMATS = ("pickle", "json", "bin")
_SUFFIX = {".pkl": "pickle", ".pickle": "pickle", ".json": "json", ".bin": "bin"}
MAGIC = b"FIDX"
VERSION = 1


def detect_format(path: Path, fmt: str | None = None) -> str:
    if fmt:
        return fmt
    try:
        return _SUFFIX[path.suffix]
    except KeyError:
        raise ValueError(f"can't infer format from {path.name!r}; pass --format") from None


def _meta_rows(index: Index) -> tuple[list[int], list[list[str]]]:
    n = index.n_docs
    lengths = [index.doc_lengths[i] for i in range(n)]
    metas = [[index.doc_meta[i].path, index.doc_meta[i].title] for i in range(n)]
    return lengths, metas


def _restore_meta(lengths: list[int], metas: list[list[str]]):
    doc_lengths = dict(enumerate(lengths))
    doc_meta = {i: DocMeta(i, path, title) for i, (path, title) in enumerate(metas)}
    return doc_lengths, doc_meta


# ---------------------------------------------------------------------------
def save(index: Index, path: Path, fmt: str | None = None) -> str:
    """Write ``index`` to ``path``; return the format used."""
    path = Path(path)
    fmt = detect_format(path, fmt)
    if fmt == "pickle":
        with path.open("wb") as f:
            pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
    elif fmt == "json":
        _save_json(index, path)
    elif fmt == "bin":
        _save_bin(index, path)
    else:
        raise ValueError(fmt)
    return fmt


def load(path: Path, fmt: str | None = None, *, lazy: bool = False) -> Index:
    path = Path(path)
    fmt = detect_format(path, fmt)
    if fmt == "pickle":
        with path.open("rb") as f:
            return pickle.load(f)  # only ever on files we wrote ourselves — see above
    if fmt == "json":
        return _load_json(path)
    if fmt == "bin":
        return _load_bin(path, lazy=lazy)
    raise ValueError(fmt)


# --- json ------------------------------------------------------------------
def _save_json(index: Index, path: Path) -> None:
    lengths, metas = _meta_rows(index)
    postings: dict[str, list[list]] = {}
    for term in index.postings:
        ids, tfs = index.doc_ids(term), index.tfs(term)
        if index.has_positions:
            pos = [p.positions for p in index.postings[term]]  # type: ignore[attr-defined]
            postings[term] = [[d, t, list(ps)] for d, t, ps in zip(ids, tfs, pos, strict=True)]
        else:
            postings[term] = [[d, t] for d, t in zip(ids, tfs, strict=True)]
    doc = {
        "format": "findex-json",
        "version": VERSION,
        "has_positions": index.has_positions,
        "doc_lengths": lengths,
        "doc_meta": metas,
        "postings": postings,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))


def _load_json(path: Path) -> Index:
    with path.open(encoding="utf-8") as f:
        doc = json.load(f)
    doc_lengths, doc_meta = _restore_meta(doc["doc_lengths"], doc["doc_meta"])
    has_pos = doc["has_positions"]
    postings = {
        term: [Posting(row[0], row[1], tuple(row[2]) if has_pos else None) for row in rows]
        for term, rows in doc["postings"].items()
    }
    return Index(postings, doc_lengths, doc_meta, "slots", has_pos)


# --- bin -------------------------------------------------------------------
def _to_le(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array("I", a)
        a.byteswap()
    return a.tobytes()


def _save_bin(index: Index, path: Path) -> None:
    ids_all, tfs_all = array("I"), array("I")
    terms: list[list] = []
    for term in index.postings:
        ids, tfs = index.doc_ids(term), index.tfs(term)
        terms.append([term, len(ids_all), len(ids)])
        ids_all.extend(ids)
        tfs_all.extend(tfs)
    lengths, metas = _meta_rows(index)
    header = json.dumps(
        {"n_postings": len(ids_all), "doc_lengths": lengths, "doc_meta": metas, "terms": terms},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    with path.open("wb") as f:
        f.write(MAGIC + struct.pack("<BI", VERSION, len(header)))
        f.write(header)
        f.write(_to_le(ids_all))
        f.write(_to_le(tfs_all))


class LazyPostings(Mapping):
    """term -> (doc_ids, tfs) decoded from an mmap on first access."""

    def __init__(self, mm: mmap.mmap, body: int, n: int, terms: dict[str, tuple[int, int]]):
        self._mm, self._body, self._n, self._terms = mm, body, n, terms

    def __getitem__(self, term: str) -> tuple[array, array]:
        off, cnt = self._terms[term]
        a, b = self._body + 4 * off, self._body + 4 * (self._n + off)
        ids, tfs = array("I"), array("I")
        ids.frombytes(self._mm[a : a + 4 * cnt])
        tfs.frombytes(self._mm[b : b + 4 * cnt])
        if sys.byteorder == "big":
            ids.byteswap()
            tfs.byteswap()
        return ids, tfs

    def __iter__(self) -> Iterator[str]:
        return iter(self._terms)

    def __len__(self) -> int:
        return len(self._terms)


def _load_bin(path: Path, lazy: bool) -> Index:
    with path.open("rb") as f:
        if f.read(4) != MAGIC:
            raise ValueError(f"{path} is not a findex .bin file")
        version, hlen = struct.unpack("<BI", f.read(5))
        if version != VERSION:
            raise ValueError(f"unsupported .bin version {version}")
        header = json.loads(f.read(hlen).decode("utf-8"))
        body = 9 + hlen
        n = header["n_postings"]
        doc_lengths, doc_meta = _restore_meta(header["doc_lengths"], header["doc_meta"])
        if lazy:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            terms = {t: (off, cnt) for t, off, cnt in header["terms"]}
            postings: Mapping = LazyPostings(mm, body, n, terms)
        else:
            allv = array("I")
            allv.frombytes(f.read(8 * n))
            if sys.byteorder == "big":
                allv.byteswap()
            postings = {
                t: (allv[off : off + cnt], allv[n + off : n + off + cnt])
                for t, off, cnt in header["terms"]
            }
    return Index(postings, doc_lengths, doc_meta, "array", False, {"lazy": lazy})
