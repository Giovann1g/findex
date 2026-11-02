"""The ``findex`` command: ``findex index``, ``findex search``, ``findex stats``.

Streams: results (the table, ``--json`` lines, the stats) go to **stdout**;
progress bars, log lines and errors go to **stderr** — so
``findex search ix.pkl "dict" --json | jq .title`` sees only data.

Logging is configured here, once, and nowhere else: the library modules only
call ``logging.getLogger(__name__)``. ``-v`` shows INFO (progress messages),
``-vv`` DEBUG (``@timed`` timings, query-cache hits).

Expected failures (missing index file, broken query, unreadable corpus) print
one ``error: …`` line and exit with code 1 — never a traceback.
"""

from __future__ import annotations

import heapq
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from findex.corpus import Document, count_documents, iter_documents
from findex.index import Layout, build_index, fmt_bytes
from findex.merge import ENGINES, EngineName
from findex.query import PhraseNeedsPositions, QuerySyntaxError, parse
from findex.rank import ScorerName, make_scorer, search
from findex.snippets import Part, document_text, snippet_parts
from findex.store import Format, IndexFileError, detect_format, open_index, save

log = logging.getLogger(__name__)

app = typer.Typer(
    name="findex",
    help="Full-text search over your own corpus: build an index, query it, inspect it.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
stdout = Console()
stderr = Console(stderr=True)

LEVELS = (logging.WARNING, logging.INFO, logging.DEBUG)


def configure_logging(verbosity: int) -> None:
    """The one place logging is set up: 0 -> WARNING, -v -> INFO, -vv -> DEBUG."""
    handler = RichHandler(console=stderr, show_time=False, show_path=False, markup=False)
    logging.basicConfig(
        level=LEVELS[min(verbosity, 2)],
        format="%(name)s: %(message)s",
        handlers=[handler],
        force=True,  # replace handlers from a previous call (tests invoke the app repeatedly)
    )


def fail(message: str, code: int = 1) -> NoReturn:
    """An *expected* error: one line on stderr, non-zero exit, no traceback."""
    # typer.echo, not rich: a long message must stay one line, never re-wrapped.
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        try:
            typer.echo(f"findex {version('findex')}")
        except PackageNotFoundError:  # running from a source tree that isn't installed
            typer.echo("findex (not installed)")
        raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="-v: INFO, -vv: DEBUG (to stderr).")
    ] = 0,
    show_version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Full-text search over your own corpus."""
    configure_logging(verbose)


# --------------------------------------------------------------------------- index
@app.command("index")
def index_cmd(
    corpus: Annotated[Path, typer.Argument(help="Corpus directory, text file or .jsonl file.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Index file: .pkl, .json or .bin.")],
    positions: Annotated[
        bool, typer.Option("--positions", help="Store token positions (needed for phrases).")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Only the first N documents.")
    ] = None,
    layout: Annotated[Layout, typer.Option(help="Postings layout in memory.")] = "slots",
    fmt: Annotated[
        Format | None, typer.Option("--format", help="File format (default: from suffix).")
    ] = None,
) -> None:
    """Build an index from a corpus, with a progress bar."""
    if not corpus.exists():
        fail(f"corpus not found: {corpus}")
    # Check everything that can fail cheaply *before* a long indexing run.
    try:
        fmt = detect_format(out, fmt)
    except IndexFileError as exc:
        fail(str(exc))
    if not out.parent.is_dir():
        fail(f"output directory does not exist: {out.parent}")
    if layout == "array" and positions:
        fail("--positions needs --layout slots or plain")
    if fmt == "bin" and positions:
        fail("the .bin format has no room for positions; use .pkl or .json")
    total = count_documents(corpus)
    if total is not None and limit is not None:
        total = min(total, limit)
    docs = islice(iter_documents(corpus), limit)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=stderr,
    ) as progress:
        task = progress.add_task("indexing", total=total)

        def tracked(stream: Iterable[Document]) -> Iterator[Document]:
            # still one lazy pass: the bar advances as documents flow through
            for doc in stream:
                yield doc
                progress.advance(task)

        ix = build_index(tracked(docs), layout, positions)

    ix.meta["root"] = str(corpus)  # lets `findex search` re-read texts for snippets
    try:
        used = save(ix, out, fmt)
    except (OSError, ValueError) as exc:
        fail(f"cannot write {out}: {exc}")
    stderr.print(
        f"wrote [bold]{out}[/] ({used}, {fmt_bytes(out.stat().st_size)}): "
        f"{ix.num_docs:,} documents, {len(ix):,} terms" + (", with positions" if positions else ""),
        soft_wrap=True,  # one line, whatever the terminal width
    )


# --------------------------------------------------------------------------- search
@dataclass(frozen=True, slots=True)
class Hit:
    """One result row, shared by the table and the --json output."""

    rank: int
    doc_id: int
    score: float
    title: str
    path: str
    parts: list[Part]

    def to_json(self) -> str:
        snippet = "".join(t for t, _ in self.parts)
        row = {"rank": self.rank, "doc_id": self.doc_id, "score": round(self.score, 4),
               "title": self.title, "path": self.path, "snippet": snippet}  # fmt: skip
        return json.dumps(row, ensure_ascii=False)


def _snippet_text(parts: list[Part]) -> Text:
    return Text.assemble(*((t, "bold yellow") if hit else t for t, hit in parts))


@app.command("search")
def search_cmd(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX", help="File from `findex index`.")],
    query: Annotated[str, typer.Argument(help="e.g. 'python AND (async OR await) \"event loop\"'")],
    k: Annotated[int, typer.Option("--k", "-k", min=1, help="How many results.")] = 10,
    scorer: Annotated[ScorerName, typer.Option(help="Ranking function.")] = "bm25",
    k1: Annotated[float, typer.Option(help="BM25 tf saturation.")] = 1.5,
    b: Annotated[float, typer.Option(help="BM25 length normalization, 0..1.")] = 0.75,
    engine: Annotated[EngineName, typer.Option(help="Boolean set operations.")] = "merge",
    json_out: Annotated[
        bool, typer.Option("--json", help="JSON lines on stdout instead of a table.")
    ] = False,
    snippets: Annotated[bool, typer.Option(help="Show text snippets.")] = True,
    root: Annotated[
        Path | None, typer.Option(help="Corpus directory, if moved since indexing.")
    ] = None,
    lazy: Annotated[bool, typer.Option(help=".bin only: mmap, decode on demand.")] = False,
    fmt: Annotated[
        Format | None, typer.Option("--format", help="Index format (default: from suffix).")
    ] = None,
) -> None:
    """Ranked search with highlighted snippets."""
    try:
        node = parse(query)
    except QuerySyntaxError as exc:
        fail(f"bad query: {exc.message} (at column {exc.pos + 1})")
    if node is None:
        fail("empty query")
    try:
        ranker = make_scorer(scorer, k1, b)
    except ValueError as exc:
        fail(str(exc))

    rows: list[Hit] = []
    try:
        with open_index(index_path, fmt, lazy=lazy) as ix:
            results = search(ix, node, scorer=ranker, k=k, engine=ENGINES[engine])
            total = len(ix.match(node, ENGINES[engine]))
            terms = node.terms()
            for rank, r in enumerate(results, start=1):
                text = document_text(ix, r.doc_id, root) if snippets else None
                parts = snippet_parts(text, terms) if text is not None else []
                path = ix.doc_meta[r.doc_id].path
                rows.append(Hit(rank, r.doc_id, r.score, r.title, path, parts))
    except (FileNotFoundError, IndexFileError, PhraseNeedsPositions) as exc:
        fail(str(exc))

    if json_out:
        for row in rows:
            typer.echo(row.to_json())
        return
    if not rows:
        stderr.print("no documents match", soft_wrap=True)
        return
    table = Table(
        title=f"{total:,} matching documents · top {len(rows)} by {ranker!r}",
        show_lines=True,
        expand=True,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("document", ratio=1)
    if snippets:
        table.add_column("snippet", ratio=2)
    for row in rows:
        doc = Text.assemble((row.title, "bold"), "\n", (row.path, "dim"))
        cells: list[str | Text] = [str(row.rank), f"{row.score:.3f}", doc]
        if snippets:
            cells.append(_snippet_text(row.parts))
        table.add_row(*cells)
    stdout.print(table)


# --------------------------------------------------------------------------- stats
@app.command("stats")
def stats_cmd(
    index_path: Annotated[Path, typer.Argument(metavar="INDEX", help="File from `findex index`.")],
    top: Annotated[int, typer.Option(min=0, help="How many top terms.")] = 20,
    json_out: Annotated[bool, typer.Option("--json", help="One JSON object on stdout.")] = False,
    fmt: Annotated[
        Format | None, typer.Option("--format", help="Index format (default: from suffix).")
    ] = None,
) -> None:
    """Corpus statistics from an index: sizes and the most frequent terms."""
    try:
        with open_index(index_path, fmt) as ix:
            summary = {
                "documents": ix.num_docs,
                "tokens": ix.num_tokens,
                "vocabulary": len(ix),
                "avg_doc_length": round(ix.avg_doc_length, 1),
                "layout": ix.layout,
                "positions": ix.has_positions,
            }
            top_terms = heapq.nlargest(
                top, ((t, ix.collection_frequency(t), ix.df(t)) for t in ix), key=lambda x: x[1]
            )
    except (FileNotFoundError, IndexFileError) as exc:
        fail(str(exc))

    if json_out:
        payload = {
            **summary,
            "top_terms": [{"term": t, "cf": cf, "df": df} for t, cf, df in top_terms],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    overview = Table(title=f"{index_path}", show_header=False)
    overview.add_column(style="bold")
    overview.add_column(justify="right")
    for key, value in summary.items():
        shown = (
            f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)
        )
        overview.add_row(key.replace("_", " "), shown)
    stdout.print(overview)
    if top_terms:
        terms = Table(title=f"top {len(top_terms)} terms")
        terms.add_column("#", justify="right", style="dim")
        terms.add_column("term", style="bold")
        terms.add_column("occurrences", justify="right")
        terms.add_column("documents", justify="right")
        for i, (t, cf, df) in enumerate(top_terms, start=1):
            terms.add_row(str(i), t, f"{cf:,}", f"{df:,}")
        stdout.print(terms)
