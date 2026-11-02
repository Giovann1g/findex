"""findex — full-text search over your own corpus.

The command line lives in :mod:`findex.cli` (``findex --help``); the library
entry points are :func:`findex.index.build_index`, :func:`findex.store.open_index`,
:func:`findex.query.parse` and :func:`findex.rank.search`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("findex")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled source tree
    __version__ = "0+unknown"
