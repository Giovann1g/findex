"""precision@5: ``python -m findex.evaluate index.pkl eval/queries.json [--embeddings E.npy]``.

Lab 3: TF-IDF vs BM25 on ten labeled queries. Lab 8 adds five *paraphrase*
queries (other words than the pages use) and, with ``--embeddings``, the
Semantic and Hybrid (RRF) columns.

P@5 = (relevant documents among the top 5) / 5. "Max" is the best a perfect
ranker could get when a query has fewer than 5 relevant documents,
min(|relevant|, 5) / 5 — so a query with 2 relevant pages can't score above 0.4.
Prints a Markdown table (and, with ``--details``, the top 5 of each run).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict, cast

from findex import store
from findex.rank import BM25, Scorer, TfIdf, search

K = 5


class _QueryRequired(TypedDict):
    query: str
    relevant: list[str]
    why: str


class QueryItem(_QueryRequired, total=False):
    kind: str  # "original" (Lab 3) or "paraphrase" (Lab 8)


class QueriesFile(TypedDict):
    criterion: str
    queries: list[QueryItem]


@dataclass
class Row:
    query: str
    kind: str
    n_rel: int
    p_at_k: dict[str, float] = field(default_factory=dict[str, float])


def precision_at_k(ranked_paths: list[str], relevant: set[str], k: int = K) -> float:
    return sum(p in relevant for p in ranked_paths[:k]) / k


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m findex.evaluate")
    p.add_argument("index", type=Path)
    p.add_argument("queries", type=Path)
    p.add_argument("--embeddings", type=Path, help="adds the Semantic and Hybrid columns (Lab 8)")
    p.add_argument("--model", type=Path, default=Path("models/all-MiniLM-L6-v2"))
    p.add_argument("--details", action="store_true")
    args = p.parse_args(argv)
    spec = cast(QueriesFile, json.loads(args.queries.read_text(encoding="utf-8")))
    scorers: dict[str, Scorer] = {"TF-IDF": TfIdf(), "BM25": BM25()}
    columns = [*scorers]

    rows: list[Row] = []
    details: list[tuple[str, str, list[str], set[str]]] = []
    with store.open_index(args.index) as ix:
        semantic = None
        if args.embeddings is not None:
            from findex.modes import Semantic
            from findex.semantic import Embeddings, OnnxEmbedder

            semantic = Semantic(Embeddings.load(args.embeddings), OnnxEmbedder(args.model))
            semantic.check(ix)
            columns += ["Semantic", "Hybrid"]
        for item in spec["queries"]:
            relevant = set(item["relevant"])
            row = Row(item["query"], item.get("kind", "original"), len(relevant))
            ranked: dict[str, list[int]] = {
                name: [r.doc_id for r in search(ix, item["query"], scorer, K)]
                for name, scorer in scorers.items()
            }
            if semantic is not None:
                from findex.modes import run

                for name, mode in (("Semantic", "semantic"), ("Hybrid", "hybrid")):
                    out = run(ix, item["query"], mode=mode, k=K, semantic=semantic)  # type: ignore[arg-type]
                    ranked[name] = [r.doc_id for r in out.ranked.results]
            for name in columns:
                paths = [ix.doc_meta[d].path for d in ranked[name]]
                row.p_at_k[name] = precision_at_k(paths, relevant)
                details.append((item["query"], name, paths, relevant))
            rows.append(row)

    head = " | ".join(f"{c} P@5" for c in columns)
    print(f"| # | Query | Kind | Relevant | Max P@5 | {head} |")
    print("|---:|---|---|---:|---:|" + "---:|" * len(columns))
    for i, r in enumerate(rows, 1):
        best = min(r.n_rel, K) / K
        cells = " | ".join(f"{r.p_at_k[c]:.1f}" for c in columns)
        print(f"| {i} | `{r.query}` | {r.kind} | {r.n_rel} | {best:.1f} | {cells} |")
    for label, group in (
        ("mean, original 10", [r for r in rows if r.kind == "original"]),
        ("mean, paraphrases", [r for r in rows if r.kind == "paraphrase"]),
        ("mean, all", rows),
    ):
        if not group:
            continue
        n = len(group)
        mean_max = sum(min(r.n_rel, K) / K for r in group) / n
        cells = " | ".join(f"**{sum(r.p_at_k[c] for r in group) / n:.2f}**" for c in columns)
        print(f"| | **{label}** | | | **{mean_max:.2f}** | {cells} |")

    if args.details:
        for query, name, paths, relevant in details:
            print(f"\n{name}  {query}")
            for i, path in enumerate(paths, 1):
                print(f"  {i}. {'✓' if path in relevant else '·'} {path}")


if __name__ == "__main__":
    main()
