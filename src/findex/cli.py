"""The ``findex`` command: ``findex crawl``, ``index``, ``search``, ``stats``.

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

import asyncio
import heapq
import json
import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console, Group
from rich.live import Live
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
from findex.crawler.crawl import CrawlStats
from findex.crawler.fetch import USER_AGENT
from findex.crawler.run import crawl_to_jsonl
from findex.index import Layout, build_index, fmt_bytes
from findex.merge import ENGINES, EngineName
from findex.parallel import ExecutorName, build_parallel
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

# Lab 5 benchmark: processes beat serial and threads on this machine, so they
# are the default; one worker per core.
DEFAULT_EXECUTOR: ExecutorName = "processes"
DEFAULT_WORKERS = os.cpu_count() or 1


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
    executor: Annotated[
        ExecutorName,
        typer.Option(help="serial loop, a thread pool, or a process pool (spawn)."),
    ] = DEFAULT_EXECUTOR,
    workers: Annotated[
        int, typer.Option("--workers", "-j", min=1, help="Pool size (default: CPU cores).")
    ] = DEFAULT_WORKERS,
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
    parallel = executor != "serial"
    if parallel and total is None:  # JSON lines: one file, many documents
        log.warning("a JSON-lines corpus can't be split by file; indexing serially")
        parallel = False
    if parallel and layout == "plain":
        fail("parallel builds merge into --layout slots or array; use --executor serial")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=stderr,
    ) as progress:
        label = f"indexing ({executor} × {workers})" if parallel else "indexing"
        task = progress.add_task(label, total=total)
        if parallel:
            # the bar advances by a chunk's documents each time a worker reports back
            ix, stats = build_parallel(
                corpus,
                executor=executor,
                workers=workers,
                positions=positions,
                limit=limit,
                layout="array" if layout == "array" else "slots",
                on_done=lambda part: progress.advance(task, part.num_docs),
            )
            del stats  # parallel.build_parallel already logs partial/merge times at INFO
        else:

            def tracked(stream: Iterable[Document]) -> Iterator[Document]:
                # still one lazy pass: the bar advances as documents flow through
                for doc in stream:
                    yield doc
                    progress.advance(task)

            ix = build_index(tracked(islice(iter_documents(corpus), limit)), layout, positions)

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


# --------------------------------------------------------------------------- crawl
DEFAULT_CRAWL_OUT = Path("data/crawl.jsonl")


class _SlowCallbacks(logging.Handler):
    """Counts asyncio debug-mode reports of a callback that blocked the loop."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("Executing"):  # "Executing <Task …> took 0.2 s"
            self.count += 1


def _crawl_panel(stats: CrawlStats, max_pages: int) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("pages", f"{stats.pages:,} / {max_pages:,}")
    table.add_row("pages/s", f"{stats.pages_per_s:.1f}")
    table.add_row("in flight", f"{stats.in_flight}")
    table.add_row("queue", f"{stats.queued:,}")
    table.add_row(
        "errors", f"{stats.errors}" + (f"  (retries {stats.retries})" if stats.retries else "")
    )
    table.add_row("robots.txt", f"{stats.robots_blocked} blocked")
    table.add_row("elapsed", f"{stats.elapsed:.1f} s")
    return table


@app.command("crawl")
def crawl_cmd(
    seeds: Annotated[list[str], typer.Argument(metavar="SEED_URL...", help="Where to start.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="JSON lines, one page per line.")] = Path(
        "data/crawl.jsonl"
    ),
    max_pages: Annotated[int, typer.Option("--max-pages", min=1, help="Page budget.")] = 500,
    concurrency: Annotated[
        int, typer.Option("--concurrency", "-c", min=1, help="Requests in flight, all hosts.")
    ] = 10,
    per_host: Annotated[
        int, typer.Option("--per-host", min=1, help="Requests in flight to one host.")
    ] = 2,
    delay: Annotated[
        float, typer.Option("--delay", min=0.0, help="Minimum seconds between requests to a host.")
    ] = 0.5,
    allow_domain: Annotated[
        list[str] | None,
        typer.Option("--allow-domain", help="Stay on these domains (default: the seeds')."),
    ] = None,
    max_depth: Annotated[
        int | None, typer.Option("--max-depth", min=0, help="Links to follow from a seed.")
    ] = None,
    timeout: Annotated[float, typer.Option(min=0.1, help="Seconds per request attempt.")] = 10.0,
    log_file: Annotated[Path, typer.Option("--log", help="Per-URL status log.")] = Path(
        "crawl.log"
    ),
    debug: Annotated[
        bool, typer.Option("--debug", help="asyncio debug mode: report anything blocking the loop.")
    ] = False,
) -> None:
    """Crawl politely from SEED_URL(s), streaming pages to JSON lines for `findex index`."""
    if out == DEFAULT_CRAWL_OUT:
        out.parent.mkdir(exist_ok=True)  # data/ is git-ignored, so a fresh clone lacks it
    if not out.parent.is_dir():
        fail(f"output directory does not exist: {out.parent}")
    for seed in seeds:
        if not seed.startswith(("http://", "https://")):
            fail(f"not an http(s) URL: {seed}")

    # crawl.log gets every URL's outcome; the screen shows only the counters.
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    crawl_log = logging.getLogger("findex.crawl")
    crawl_log.setLevel(logging.INFO)
    crawl_log.addHandler(file_handler)
    crawl_log.propagate = False
    slow = _SlowCallbacks()
    aio_log = logging.getLogger("asyncio")
    aio_log.addHandler(file_handler)
    aio_log.addHandler(slow)
    crawl_log.info("user-agent: %s", USER_AGENT)
    crawl_log.info(
        "seeds %s, max_pages %d, concurrency %d, per_host %d, delay %.2f s, debug %s",
        seeds, max_pages, concurrency, per_host, delay, debug,
    )  # fmt: skip

    stats = CrawlStats()
    run = crawl_to_jsonl(
        seeds,
        out,
        stats=stats,
        max_pages=max_pages,
        concurrency=concurrency,
        per_host=per_host,
        min_delay=delay,
        allowed_domains=allow_domain,
        max_depth=max_depth,
        timeout=timeout,
    )
    title = Text(f"findex crawl · {', '.join(seeds)}", style="bold")
    try:
        # rich redraws from its own thread, reading the counters the loop updates
        with Live(
            get_renderable=lambda: Group(title, _crawl_panel(stats, max_pages)),
            console=stderr,
            refresh_per_second=8,
        ):
            asyncio.run(run, debug=debug)
        if not stderr.is_terminal:
            stderr.line()  # the last frame of a non-terminal Live has no newline
    except KeyboardInterrupt:
        stderr.print(f"interrupted: {stats.pages:,} pages already in {out}", soft_wrap=True)
        raise typer.Exit(130) from None
    finally:
        for logger in (crawl_log, aio_log):
            logger.removeHandler(file_handler)
        aio_log.removeHandler(slow)
        file_handler.close()

    stderr.print(
        f"wrote [bold]{out}[/]: {stats.pages:,} pages in {stats.elapsed:.1f} s "
        f"({stats.pages_per_s:.1f} pages/s), {stats.requests:,} requests, "
        f"{stats.errors} errors, {stats.retries} retries; log: {log_file}",
        soft_wrap=True,
    )
    if debug:
        stderr.print(
            f"asyncio debug mode: {slow.count} callbacks blocked the loop for > 100 ms",
            soft_wrap=True,
        )
    stderr.print(f"next: findex index {out} -o crawl.pkl --positions", soft_wrap=True)
