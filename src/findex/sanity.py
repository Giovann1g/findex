"""Three ranking sanity checks on the real corpus: ``python -m findex.sanity index.pkl``.

1. A rare query term outweighs a common one.
2. Under BM25 the 20th repetition of a term adds almost nothing.
3. A short document with one match outranks a very long one with one match.

Prints Markdown, ready for the README.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from findex import store
from findex.index import Index, Posting
from findex.rank import BM25, TfIdf, search

bm25, tfidf = BM25(), TfIdf()


def rank_of(results, doc_id: int) -> int | None:
    return next((i for i, r in enumerate(results, 1) if r.doc_id == doc_id), None)


def check_rare_vs_common(ix: Index, common: str, rare: str) -> None:
    print(f"### 1. Rare term beats common term: `{common} OR {rare}`\n")
    a, b = set(ix.doc_ids(common)), set(ix.doc_ids(rare))
    print(f"df(`{common}`) = {len(a)}, df(`{rare}`) = {len(b)} of N = {ix.num_docs}.\n")
    print("| | idf BM25 | idf TF-IDF | one occurrence in an average-length doc, BM25 |")
    print("|---|---:|---:|---:|")
    for t in (common, rare):
        one = bm25.idf(t, ix) * bm25.tf_part(1, round(ix.avg_doc_length), ix.avg_doc_length)
        print(f"| `{t}` | {bm25.idf(t, ix):.3f} | {tfidf.idf(t, ix):.3f} | {one:.3f} |")
    print()
    rare_only = sorted(b - a)
    for scorer in (bm25, tfidf):
        res = search(ix, f"{common} OR {rare}", scorer=scorer, k=ix.num_docs)
        common_only = [r for r in res if r.doc_id in a - b]
        for d in rare_only:
            p = ix[rare][[q.doc_id for q in ix[rare]].index(d)]
            print(
                f"- {scorer!r}: the only document with `{rare}` but no `{common}` "
                f"(*{ix.title(d)}*, tf={p.tf}, {ix.doc_length(d):,} tokens) is ranked "
                f"**#{rank_of(res, d)}** of {len(res)}; it outranks "
                f"**{sum(r.score < res[rank_of(res, d) - 1].score for r in common_only)} of "
                f"{len(common_only)}** documents that contain only `{common}`."
            )
    print()


def check_saturation(ix: Index, term: str) -> None:
    dl = round(ix.avg_doc_length)
    print(f"### 2. Saturation: term `{term}` in a document of average length ({dl:,} tokens)\n")

    def s_bm25(tf: int) -> float:
        return bm25.idf(term, ix) * bm25.tf_part(tf, dl, ix.avg_doc_length) if tf else 0.0

    def s_tfidf(tf: int) -> float:
        return tfidf.score(term, Posting(0, tf), ix) if tf else 0.0

    print(
        "| tf | BM25 score | BM25: added by the tf-th occurrence | TF-IDF score | TF-IDF: added |"
    )
    print("|---:|---:|---:|---:|---:|")
    for tf in (1, 2, 3, 5, 10, 20, 50):
        print(
            f"| {tf} | {s_bm25(tf):.3f} | {s_bm25(tf) - s_bm25(tf - 1):.4f} | "
            f"{s_tfidf(tf):.3f} | {s_tfidf(tf) - s_tfidf(tf - 1):.4f} |"
        )
    g20, g1 = s_bm25(20) - s_bm25(19), s_bm25(1)
    t20, t1 = s_tfidf(20) - s_tfidf(19), s_tfidf(1)
    print(
        f"\nUnder BM25 the 20th occurrence adds **{g20:.4f}**, {g20 / g1:.1%} of what the "
        f"first one added, and the score can never pass idf·(k1+1) = "
        f"{bm25.idf(term, ix) * (bm25.k1 + 1):.3f}. TF-IDF's 1 + ln tf also flattens "
        f"({t20 / t1:.1%} for the 20th), but has no ceiling: at tf = 50 it is "
        f"{s_tfidf(50) / s_tfidf(1):.1f}× the single-occurrence score, BM25 only "
        f"{s_bm25(50) / s_bm25(1):.1f}×.\n"
    )


def check_length(ix: Index, term: str | None = None) -> None:
    # A term whose tf=1 documents include both a very short and a very long one.
    best = None
    for t in [term] if term else ix:
        if not term and not (t.isalpha() and len(t) >= 6 and 5 <= ix.df(t) <= 40):
            continue
        ones = [p.doc_id for p in ix[t] if p.tf == 1]
        if len(ones) < 2:
            continue
        short = min(ones, key=ix.doc_length)
        long = max(ones, key=ix.doc_length)
        ratio = ix.doc_length(long) / ix.doc_length(short)
        if best is None or ratio > best[0]:
            best = (ratio, t, short, long)
    assert best is not None
    ratio, t, short, long = best
    print(f"### 3. Length normalization: query `{t}`\n")
    print(
        f"`{t}` occurs exactly once in *{ix.title(short)}* ({ix.doc_length(short):,} tokens) "
        f"and once in *{ix.title(long)}* ({ix.doc_length(long):,} tokens, ×{ratio:.0f} longer).\n"
    )
    print("| Scorer | score short | score long | rank short | rank long |")
    print("|---|---:|---:|---:|---:|")
    for scorer in (bm25, BM25(b=0.0), tfidf):
        res = search(ix, t, scorer=scorer, k=ix.num_docs)
        s_short = next(r.score for r in res if r.doc_id == short)
        s_long = next(r.score for r in res if r.doc_id == long)
        print(
            f"| {scorer!r} | {s_short:.3f} | {s_long:.3f} | "
            f"{rank_of(res, short)} | {rank_of(res, long)} |"
        )
    print(
        "\nWith `b = 0` and with TF-IDF the two scores are equal (the tie is broken by "
        "doc id); only BM25's length normalization puts the short document ahead.\n"
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.sanity")
    p.add_argument("index", type=Path)
    p.add_argument("--common", default="python")
    p.add_argument("--rare", default="heapq")
    p.add_argument("--saturation-term", default="coroutine")
    p.add_argument("--length-term", default=None)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    with store.open_index(args.index) as ix:
        print(f"Index: `{ix!r}`, average document length {ix.avg_doc_length:,.1f} tokens.\n")
        check_rare_vs_common(ix, args.common, args.rare)
        check_saturation(ix, args.saturation_term)
        check_length(ix, args.length_term)


if __name__ == "__main__":
    main()
