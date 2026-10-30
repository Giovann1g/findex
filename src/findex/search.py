"""Ranked search over a saved index (Lab 3; Boolean-only mode kept from Lab 2).

Usage::

    python -m findex.search index.pkl 'python AND (async OR await) NOT java "event loop"'
    python -m findex.search index.pkl "unicode normalization" --scorer tfidf -k 5
    python -m findex.search index.pkl "dict hash" --repeat 2     # 2nd run: cache hit
    python -m findex.search index.bin "generator NOT async" --boolean --engine set

Query syntax (see ``findex.query``): implicit AND, ``AND``/``OR``/``NOT`` in
upper case, ``( … )`` and ``"quoted phrases"`` (phrases need ``--positions``).
``@timed`` and match-cache lines go to stderr; ``-q`` silences them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from findex import store
from findex.index import setup_logging
from findex.merge import ENGINES
from findex.query import PhraseNeedsPositions, QuerySyntaxError, parse
from findex.rank import BM25, TfIdf, search
from findex.snippets import document_text, make_snippet


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m findex.search",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("index", type=Path, help="file written by python -m findex.index")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10, help="how many results")
    p.add_argument("--scorer", choices=["bm25", "tfidf"], default="bm25")
    p.add_argument("--k1", type=float, default=1.5, help="BM25 tf saturation")
    p.add_argument("--b", type=float, default=0.75, help="BM25 length normalization")
    p.add_argument("--engine", choices=list(ENGINES), default="merge")
    p.add_argument("--boolean", action="store_true", help="unranked matches (Lab 2 mode)")
    p.add_argument("--repeat", type=int, default=1, help="run the query N times")
    p.add_argument("--no-snippets", action="store_true")
    p.add_argument("--root", type=Path, default=None, help="corpus dir, if moved since build")
    p.add_argument(
        "--mark",
        choices=["ansi", "md", "brackets", "none"],
        default=None,
        help="how to highlight terms (default: ansi on a terminal, else md)",
    )
    p.add_argument("--format", choices=store.FORMATS, default=None)
    p.add_argument("--lazy", action="store_true", help=".bin only: mmap, decode on demand")
    p.add_argument("-q", "--quiet", action="store_true", help="no @timed / cache log lines")
    args = p.parse_args(argv)
    setup_logging(args.quiet)
    mark = args.mark or ("ansi" if sys.stdout.isatty() else "md")
    scorer = BM25(args.k1, args.b) if args.scorer == "bm25" else TfIdf()
    engine = ENGINES[args.engine]

    try:
        node = parse(args.query)
    except QuerySyntaxError as exc:
        print(f"query error: {exc}", file=sys.stderr)
        return 2
    print(f"query:  {args.query}")
    print(f"tree:   {node!r}")

    with store.open_index(args.index, args.format, lazy=args.lazy) as ix:
        print(f"index:  {ix!r}")
        if node is None:
            print("empty query")
            return 0
        try:
            for _ in range(args.repeat):
                if args.boolean:
                    hits = ix.match(node, engine)
                else:
                    results = search(ix, node, scorer=scorer, k=args.k, engine=engine)
        except PhraseNeedsPositions as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if args.boolean:
            print(f"\n{len(hits):,} of {ix.num_docs:,} documents match [engine={engine.name}]")
            for doc_id in hits[: args.k]:
                m = ix.doc_meta[doc_id]
                print(f"  {doc_id:>5}  {m.title[:60]:<60}  {m.path}")
            return 0

        total = len(ix.match(node, engine)) if results else 0
        print(f"\n{total:,} matching documents, top {len(results)} by {scorer!r}:\n")
        terms = node.terms()
        for rank, r in enumerate(results, start=1):
            print(f"{rank:>2}. {r.score:7.3f}  {r.title}  ({ix.doc_meta[r.doc_id].path})")
            if not args.no_snippets:
                text = document_text(ix, r.doc_id, args.root)
                if text is not None:
                    print(f"    {make_snippet(text, terms, mark=mark)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
