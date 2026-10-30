"""precision@5 for TF-IDF vs BM25: ``python -m findex.evaluate index.pkl eval/queries.json``.

P@5 = (relevant documents among the top 5) / 5. "Max" is the best a perfect
ranker could get when a query has fewer than 5 relevant documents,
min(|relevant|, 5) / 5 — so a query with 2 relevant pages can't score above 0.4.
Prints a Markdown table (and, with ``--details``, the top 5 of each run).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from findex import store
from findex.rank import BM25, TfIdf, search

K = 5


def precision_at_k(ranked_paths: list[str], relevant: set[str], k: int = K) -> float:
    return sum(p in relevant for p in ranked_paths[:k]) / k


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.evaluate")
    p.add_argument("index", type=Path)
    p.add_argument("queries", type=Path)
    p.add_argument("--details", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    spec = json.loads(args.queries.read_text(encoding="utf-8"))
    scorers = {"TF-IDF": TfIdf(), "BM25": BM25()}

    rows, details = [], []
    with store.open_index(args.index) as ix:
        for item in spec["queries"]:
            relevant = set(item["relevant"])
            row = {"query": item["query"], "n_rel": len(relevant)}
            for name, scorer in scorers.items():
                paths = [ix.doc_meta[r.doc_id].path for r in search(ix, item["query"], scorer, K)]
                row[name] = precision_at_k(paths, relevant)
                details.append((item["query"], name, paths, relevant))
            rows.append(row)

    print("| # | Query | Relevant | Max P@5 | TF-IDF P@5 | BM25 P@5 |")
    print("|---:|---|---:|---:|---:|---:|")
    for i, r in enumerate(rows, 1):
        best = min(r["n_rel"], K) / K
        print(
            f"| {i} | `{r['query']}` | {r['n_rel']} | {best:.1f} | "
            f"{r['TF-IDF']:.1f} | {r['BM25']:.1f} |"
        )
    n = len(rows)
    mean = {name: sum(r[name] for r in rows) / n for name in scorers}
    mean_max = sum(min(r["n_rel"], K) / K for r in rows) / n
    print(
        f"| | **mean** | | **{mean_max:.2f}** | **{mean['TF-IDF']:.2f}** | **{mean['BM25']:.2f}** |"
    )

    if args.details:
        for query, name, paths, relevant in details:
            print(f"\n{name}  {query}")
            for i, path in enumerate(paths, 1):
                print(f"  {i}. {'✓' if path in relevant else '·'} {path}")


if __name__ == "__main__":
    main()
