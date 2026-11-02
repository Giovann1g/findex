"""Shared fixtures: a tiny on-disk corpus and indexes built from it.

Scopes: the corpus *directory* is session-scoped — it is written once and only
ever read. Indexes are function-scoped on purpose: ``Index`` carries mutable
state (the query cache, ``cached_property`` values, ``close()``), so a
session-wide index would let one test's side effects leak into the next.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings

from findex.corpus import iter_documents
from findex.index import Index, build_index

# CI machines are slower and noisier than a laptop: no per-example deadline
# there, so a busy runner can't turn a passing property into a flaky failure.
settings.register_profile("ci", deadline=None, print_blob=True)
if os.environ.get("CI"):
    settings.load_profile("ci")

CORPUS: dict[str, str] = {
    "intro.txt": "Python is a programming language. The event loop runs coroutines.",
    "asyncio.md": (
        "Asyncio\n=======\n\nThe event loop schedules tasks; await suspends a coroutine.\n"
        "One event loop per thread. Event loop, event loop, event loop."
    ),
    "unicode.rst": (
        "Unicode HOWTO\n=============\n\nNormalization: NFC composes café into café; "
        "Straße casefolds to strasse. Привіт, світе! П’ять м'ят."
    ),
    "sub/sorting.txt": "Sort with a key function: sorted(data, key=len). The sort is stable.",
    "sub/long.txt": "filler " * 300 + "needle in a haystack",
    "short.txt": "needle",
    "skip.bin": "\x00not text",  # wrong suffix: must be ignored
}
TEXT_DOCS = 6  # CORPUS minus skip.bin


@pytest.fixture(scope="session")
def corpus_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus")
    for name, text in CORPUS.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _build(corpus_dir: Path, **kwargs: object) -> Index:
    ix = build_index(iter_documents(corpus_dir), **kwargs)  # type: ignore[arg-type]  # test helper forwards keywords
    ix.meta["root"] = str(corpus_dir)
    return ix


@pytest.fixture
def corpus_index(corpus_dir: Path) -> Iterator[Index]:
    """Positional index of the fixture corpus, fresh for every test."""
    ix = _build(corpus_dir, positions=True)
    yield ix
    ix.close()


def doc_id_of(ix: Index, path_suffix: str) -> int:
    return next(d for d, m in ix.doc_meta.items() if m.path.endswith(path_suffix))
