"""Pydantic models: the API's *boundary*, and nothing else.

Requests are validated on the way in (query parameters are declared on the
routes), responses are validated and serialized on the way out, and the
OpenAPI page at ``/docs`` is generated from these classes. Inside, the search
engine keeps its own dataclasses from Labs 2–3 (``SearchResult``,
``DocMeta``, ``Posting``) — :mod:`findex.web.service` converts at the seam.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SnippetPart(BaseModel):
    text: str
    hit: bool = Field(description="True for a word that matched the query")


class SearchHit(BaseModel):
    rank: int = Field(ge=1)
    doc_id: int = Field(ge=0)
    title: str
    path: str
    score: float
    snippet: list[SnippetPart]


class SearchResponse(BaseModel):
    query: str
    mode: str = Field(description="lexical (BM25/TF-IDF), semantic (embeddings) or hybrid (RRF)")
    scorer: str
    page: int = Field(ge=1)
    k: int = Field(ge=1, description="results per page")
    total: int = Field(ge=0, description="documents that match, on all pages")
    pages: int = Field(ge=0)
    took_ms: float
    results: list[SearchHit]


class DocumentResponse(BaseModel):
    doc_id: int
    title: str
    path: str
    length: int = Field(description="tokens in the document")
    text: str | None = Field(description="null when the corpus isn't available to the server")


class StatsResponse(BaseModel):
    documents: int
    tokens: int
    vocabulary: int
    avg_doc_length: float
    positions: bool
    layout: str
    index_path: str
    index_bytes: int
    semantic_chunks: int | None = Field(description="embedded chunks; null: semantic off")
    semantic_model: str | None
    loaded_at: datetime
    uptime_s: float


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok", "loading", "failed"])
    documents: int | None = None
    semantic: bool | None = None
    detail: str | None = None


class ErrorResponse(BaseModel):
    detail: str
    request_id: str | None = None
