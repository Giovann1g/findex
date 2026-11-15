"""The web page: Jinja2 templates, server-rendered, with htmx on top.

``GET /`` renders the search page; with ``?q=`` it renders results too, so
every result page has a URL you can share and works without JavaScript.
htmx adds search-as-you-type: the form asks for ``/?q=…`` with an
``HX-Request`` header, and the route answers with just the results fragment,
which htmx swaps into ``#results`` (and pushes the URL into history).
``GET /doc/{doc_id}`` shows one document with the query's words marked.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from findex.rank import ScorerName
from findex.semantic import Mode
from findex.web.deps import HolderDep, SettingsDep
from findex.web.service import (
    BadQuery,
    SearchPage,
    get_document,
    highlight,
    parse_query,
    run_search,
)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
router = APIRouter(include_in_schema=False)

EXAMPLES = [
    "asyncio",
    '"event loop"',
    "unicode AND normalization",
    "thread NOT process",
    '"context manager" (with OR async)',
    "run many things at the same time",  # no shared words with the pages: try "Зміст"
]


def page_numbers(current: int, last: int, around: int = 2) -> list[int | None]:
    """1 … 4 5 [6] 7 8 … 20 — ``None`` marks a gap."""
    shown = {1, last, *range(current - around, current + around + 1)}
    out: list[int | None] = []
    prev = 0
    for n in sorted(x for x in shown if 1 <= x <= last):
        if n - prev > 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    holder: HolderDep,
    settings: SettingsDep,
    q: Annotated[str, Query(max_length=500)] = "",
    page: Annotated[int, Query(ge=1, le=100)] = 1,
    k: Annotated[int, Query(ge=1, le=50)] = 10,
    scorer: ScorerName = "bm25",
    mode: Mode | None = None,
) -> HTMLResponse:
    index = holder.index if holder else None
    semantic = holder.semantic if holder else None
    # the page defaults to hybrid when embeddings are loaded: meaning *and* words
    chosen: Mode = mode or ("hybrid" if semantic is not None else "lexical")
    result: SearchPage | None = None
    error: str | None = None
    if q.strip() and index is not None:
        try:
            result = run_search(
                index, q, k=k, page=page, scorer=scorer, mode=chosen, semantic=semantic,
                root=settings.corpus_root,
            )  # fmt: skip
        except BadQuery as exc:
            error = str(exc)
    context = {
        "q": q,
        "scorer": scorer,
        "mode": chosen,
        "semantic": semantic is not None,
        "k": k,
        "result": result,
        "error": error,
        "index": index,
        "status": holder.status if holder else "loading",
        "examples": EXAMPLES,
        "pages": page_numbers(result.page, result.pages) if result and result.pages else [],
    }
    fragment = request.headers.get("hx-request") == "true"
    name = "_results.html" if fragment else "index.html"
    return templates.TemplateResponse(request, name, context)


@router.get("/doc/{doc_id}", response_class=HTMLResponse)
def document_page(
    request: Request,
    holder: HolderDep,
    settings: SettingsDep,
    doc_id: int,
    q: Annotated[str, Query(max_length=500)] = "",
) -> HTMLResponse:
    index = holder.index if holder else None
    doc = get_document(index, doc_id, settings.corpus_root) if index is not None else None
    if doc is None:
        context = {"doc_id": doc_id, "q": q, "loading": index is None}
        return templates.TemplateResponse(request, "missing.html", context, status_code=404)
    terms: frozenset[str] = frozenset()
    if q.strip():
        with suppress(BadQuery):  # a broken query just highlights nothing
            terms = frozenset(parse_query(q).terms())
    parts = highlight(doc.text, terms) if doc.text is not None else []
    hits = sum(1 for _, hit in parts if hit)
    context = {"doc": doc, "q": q, "parts": parts, "hits": hits}
    return templates.TemplateResponse(request, "doc.html", context)
