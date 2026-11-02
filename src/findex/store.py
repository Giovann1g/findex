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
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from findex.index import ArrayPostings, DocMeta, Index, Posting, PostingList
from findex.timing import timed

# A plain assignment, not a PEP 695 `type` statement: typer reads this annotation
# at runtime to build CLI choices and doesn't understand `TypeAliasType` objects.
Format = Literal["pickle", "json", "bin"]
FORMATS: tuple[Format, ...] = ("pickle", "json", "bin")
_SUFFIX: dict[str, Format] = {".pkl": "pickle", ".pickle": "pickle", ".json": "json", ".bin": "bin"}
MAGIC = b"FIDX"
VERSION = 1


class IndexFileError(ValueError):
    """The file exists but is not a findex index this version can read."""


class _JsonIndex(TypedDict):
    format: str
    version: int
    has_positions: bool
    doc_lengths: list[int]
    doc_meta: list[list[str]]
    meta: dict[str, object]
    postings: dict[str, list[list[Any]]]  # [doc_id, tf] or [doc_id, tf, [positions]]


class _BinHeader(TypedDict):
    n_postings: int
    doc_lengths: list[int]
    doc_meta: list[list[str]]
    meta: dict[str, object]
    terms: list[tuple[str, int, int]]  # (term, offset, count)


def detect_format(path: Path, fmt: Format | None = None) -> Format:
    if fmt:
        return fmt
    try:
        return _SUFFIX[path.suffix]
    except KeyError:
        raise IndexFileError(
            f"can't tell the index format of {path.name!r} from its suffix "
            "(.pkl, .json, .bin); pass --format"
        ) from None


def _meta_rows(index: Index) -> tuple[list[int], list[list[str]]]:
    n = index.num_docs
    lengths = [index.doc_lengths[i] for i in range(n)]
    metas = [[index.doc_meta[i].path, index.doc_meta[i].title] for i in range(n)]
    return lengths, metas


def _restore_meta(
    lengths: list[int], metas: list[list[str]]
) -> tuple[dict[int, int], dict[int, DocMeta]]:
    doc_lengths = dict(enumerate(lengths))
    doc_meta = {i: DocMeta(i, path, title) for i, (path, title) in enumerate(metas)}
    return doc_lengths, doc_meta


# ---------------------------------------------------------------------------
def save(index: Index, path: Path, fmt: Format | None = None) -> Format:
    """Write ``index`` to ``path``; return the format used."""
    path = Path(path)
    fmt = detect_format(path, fmt)
    if fmt == "pickle":
        with path.open("wb") as f:
            pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
    elif fmt == "json":
        _save_json(index, path)
    else:
        _save_bin(index, path)
    return fmt


@timed
def load(path: Path, fmt: Format | None = None, *, lazy: bool = False) -> Index:
    """Read an index. Raises ``FileNotFoundError`` for a missing file and
    :class:`IndexFileError` for a file that isn't a readable findex index."""
    path = Path(path)
    fmt = detect_format(path, fmt)
    if not path.is_file():
        raise FileNotFoundError(f"index file not found: {path}")
    try:
        if fmt == "pickle":
            with path.open("rb") as f:
                obj: object = pickle.load(f)  # only ever files we wrote ourselves, see above
            if not isinstance(obj, Index):
                raise IndexFileError(f"{path} holds a {type(obj).__name__}, not a findex index")
            return obj
        if fmt == "json":
            return _load_json(path)
        return _load_bin(path, lazy=lazy)
    except IndexFileError:
        raise
    except (pickle.UnpicklingError, EOFError, AttributeError, ImportError) as exc:
        raise IndexFileError(f"{path} is not a findex pickle ({exc})") from exc
    except (ValueError, KeyError, TypeError, struct.error) as exc:
        raise IndexFileError(f"{path} is not a valid findex {fmt} file ({exc})") from exc


@contextmanager
def open_index(path: Path, fmt: Format | None = None, *, lazy: bool = False) -> Generator[Index]:
    """``with open_index("index.pkl") as ix: ...`` — load, use, always release.

    Everything before ``yield`` runs on ``__enter__``; the ``finally`` runs on
    ``__exit__`` whether the body finished, ``return``-ed or raised, so a lazy
    ``.bin`` never leaks its mmap. The exception itself is not swallowed: it
    is re-raised at the ``yield`` and propagates after the cleanup.
    """
    index = load(path, fmt, lazy=lazy)
    try:
        yield index
    finally:
        index.close()


def _portable_meta(index: Index) -> dict[str, object]:
    """The JSON-safe part of ``index.meta`` (e.g. the corpus root)."""
    return {k: v for k, v in index.meta.items() if isinstance(v, str | int | float | bool)}


# --- json ------------------------------------------------------------------
def _save_json(index: Index, path: Path) -> None:
    lengths, metas = _meta_rows(index)
    postings: dict[str, list[list[Any]]] = {}
    for term, plist in index.items():
        if index.has_positions:
            postings[term] = [[p.doc_id, p.tf, list(p.positions or ())] for p in plist]
        else:
            postings[term] = [[p.doc_id, p.tf] for p in plist]
    doc: _JsonIndex = {
        "format": "findex-json",
        "version": VERSION,
        "has_positions": index.has_positions,
        "doc_lengths": lengths,
        "doc_meta": metas,
        "meta": _portable_meta(index),
        "postings": postings,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))


def _load_json(path: Path) -> Index:
    with path.open(encoding="utf-8") as f:
        # json.load returns Any; the TypedDict documents the shape we wrote.
        doc = cast(_JsonIndex, json.load(f))
    if doc.get("format") != "findex-json":
        raise IndexFileError(f"{path} is JSON but not a findex index")
    doc_lengths, doc_meta = _restore_meta(doc["doc_lengths"], doc["doc_meta"])
    has_pos = doc["has_positions"]
    postings: dict[str, PostingList] = {
        term: [Posting(int(r[0]), int(r[1]), tuple(r[2]) if has_pos else None) for r in rows]
        for term, rows in doc["postings"].items()
    }
    return Index(postings, doc_lengths, doc_meta, "slots", has_pos, doc.get("meta"))


# --- bin -------------------------------------------------------------------
def _to_le(a: array[int]) -> bytes:
    if sys.byteorder == "big":
        a = array("I", a)
        a.byteswap()
    return a.tobytes()


def _save_bin(index: Index, path: Path) -> None:
    ids_all: array[int] = array("I")
    tfs_all: array[int] = array("I")
    terms: list[tuple[str, int, int]] = []
    for term in index:
        ids, tfs = index.doc_ids(term), index.tfs(term)
        terms.append((term, len(ids_all), len(ids)))
        ids_all.extend(ids)
        tfs_all.extend(tfs)
    lengths, metas = _meta_rows(index)
    header: _BinHeader = {
        "n_postings": len(ids_all),
        "doc_lengths": lengths,
        "doc_meta": metas,
        "meta": _portable_meta(index),
        "terms": terms,
    }
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with path.open("wb") as f:
        f.write(MAGIC + struct.pack("<BI", VERSION, len(raw)))
        f.write(raw)
        f.write(_to_le(ids_all))
        f.write(_to_le(tfs_all))


class LazyPostings(Mapping[str, ArrayPostings]):
    """term -> (doc_ids, tfs) decoded from an mmap on first access."""

    def __init__(self, mm: mmap.mmap, body: int, n: int, terms: dict[str, tuple[int, int]]) -> None:
        self._mm, self._body, self._n, self._terms = mm, body, n, terms

    def __getitem__(self, term: str) -> ArrayPostings:
        off, cnt = self._terms[term]
        a, b = self._body + 4 * off, self._body + 4 * (self._n + off)
        ids: array[int] = array("I")
        tfs: array[int] = array("I")
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

    def close(self) -> None:
        self._mm.close()

    @property
    def closed(self) -> bool:
        return self._mm.closed


def _load_bin(path: Path, lazy: bool) -> Index:
    with path.open("rb") as f:
        if f.read(4) != MAGIC:
            raise IndexFileError(f"{path} is not a findex .bin file")
        version, hlen = struct.unpack("<BI", f.read(5))
        if version != VERSION:
            raise IndexFileError(f"unsupported .bin version {version}")
        header = cast(_BinHeader, json.loads(f.read(hlen).decode("utf-8")))
        body = 9 + hlen
        n = header["n_postings"]
        doc_lengths, doc_meta = _restore_meta(header["doc_lengths"], header["doc_meta"])
        postings: Mapping[str, ArrayPostings]
        if lazy:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            terms = {t: (off, cnt) for t, off, cnt in header["terms"]}
            postings = LazyPostings(mm, body, n, terms)
        else:
            allv: array[int] = array("I")
            allv.frombytes(f.read(8 * n))
            if sys.byteorder == "big":
                allv.byteswap()
            postings = {
                t: (allv[off : off + cnt], allv[n + off : n + off + cnt])
                for t, off, cnt in header["terms"]
            }
    meta: dict[str, object] = {**header.get("meta", {}), "lazy": lazy}
    return Index(postings, doc_lengths, doc_meta, "array", False, meta)
