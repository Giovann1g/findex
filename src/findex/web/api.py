"""The JSON API: ``/search``, ``/docs/{doc_id}``, ``/stats``, ``/health``.

``search`` is a plain ``def`` — measured, not guessed: BM25 scoring and
snippets are CPU work. A ``def`` route runs in Starlette's thread pool, so the
event loop keeps accepting connections and answering ``/health`` while a
search computes; an ``async def`` route would compute *on* the loop and stall
every other request for its duration (README: "def чи async def").
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from findex.rank import ScorerName
from findex.web.deps import HolderDep, IndexDep, SettingsDep
from findex.web.models import (
    DocumentResponse,
    ErrorResponse,
    HealthResponse,
    SearchHit,
    SearchResponse,
    SnippetPart,
    StatsResponse,
)
from findex.web.service import BadQuery, SearchPage, get_document, run_search

router = APIRouter()

ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "The query can't be parsed"},
    503: {"model": ErrorResponse, "description": "The index is still loading"},
}

Q = Annotated[
    str,
    Query(min_length=1, max_length=500, description='Words, "phrases", AND / OR / NOT, ( )'),
]
K = Annotated[int, Query(ge=1, le=50, description="Results per page")]
PageNo = Annotated[int, Query(ge=1, le=100, description="1-based page number")]


def to_response(page: SearchPage) -> SearchResponse:
    """The internal dataclasses → the public schema (the one seam)."""
    return SearchResponse(
        query=page.query,
        scorer=page.scorer,
        page=page.page,
        k=page.k,
        total=page.total,
        pages=page.pages,
        took_ms=round(page.took_ms, 2),
        results=[
            SearchHit(
                rank=h.rank,
                doc_id=h.result.doc_id,
                title=h.title,
                path=h.path,
                score=round(h.result.score, 4),
                snippet=[SnippetPart(text=t, hit=hit) for t, hit in h.snippet],
            )
            for h in page.hits
        ],
    )


def search_page(
    index: IndexDep, settings: SettingsDep, q: str, k: int, page: int, scorer: ScorerName
) -> SearchResponse:
    try:
        result = run_search(index, q, k=k, page=page, scorer=scorer, root=settings.corpus_root)
    except BadQuery as exc:
        raise HTTPException(400, str(exc)) from None
    return to_response(result)


@router.get("/search", response_model=SearchResponse, responses=ERRORS, tags=["search"])
def api_search(
    index: IndexDep,
    settings: SettingsDep,
    q: Q,
    k: K = 10,
    page: PageNo = 1,
    scorer: ScorerName = "bm25",
) -> SearchResponse:
    """Ranked search, one page of results with highlighted snippets."""
    return search_page(index, settings, q, k, page, scorer)


@router.get(
    "/docs/{doc_id}",
    response_model=DocumentResponse,
    responses={404: {"model": ErrorResponse}, 503: ERRORS[503]},
    tags=["documents"],
)
def api_document(
    index: IndexDep, settings: SettingsDep, doc_id: Annotated[int, Path(ge=0)]
) -> DocumentResponse:
    """One document: title, path, length and full text."""
    doc = get_document(index, doc_id, settings.corpus_root)
    if doc is None:
        raise HTTPException(404, f"no document with id {doc_id}")
    return DocumentResponse(
        doc_id=doc.doc_id, title=doc.title, path=doc.path, length=doc.length, text=doc.text
    )


@router.get("/stats", response_model=StatsResponse, responses=ERRORS, tags=["index"])
def api_stats(index: IndexDep, holder: HolderDep, settings: SettingsDep) -> StatsResponse:
    """Index size and how long this process has been up."""
    path = settings.index_path
    loaded = holder.loaded_at if holder and holder.loaded_at else datetime.now(UTC)
    started = holder.started if holder else time.monotonic()
    return StatsResponse(
        documents=index.num_docs,
        tokens=index.num_tokens,
        vocabulary=len(index),
        avg_doc_length=round(index.avg_doc_length, 1),
        positions=index.has_positions,
        layout=index.layout,
        index_path=str(path),
        index_bytes=path.stat().st_size if path.is_file() else 0,
        loaded_at=loaded,
        uptime_s=round(time.monotonic() - started, 1),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Index loading or failed"}},
    response_model_exclude_none=True,
    tags=["index"],
)
async def api_health(holder: HolderDep) -> HealthResponse | JSONResponse:
    """200 once the index is in memory; 503 before that (the platform's probe).

    ``async def`` on purpose: it only reads two attributes, so it answers on
    the event loop at once — even while every pool thread is busy searching.
    """
    if holder is None or holder.index is None:
        status = "failed" if holder is not None and holder.error else "loading"
        body = HealthResponse(status=status, detail=holder.error if holder else None)
        return JSONResponse(body.model_dump(exclude_none=True), status_code=503)
    return HealthResponse(status="ok", documents=holder.index.num_docs)
