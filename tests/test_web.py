"""Lab 7: the FastAPI app — every route, through ``TestClient``.

No index file is involved: ``dependency_overrides`` hands the routes the
small fixture index from ``conftest.py`` (``corpus_index``). Only the lifespan
tests write a real file, because loading one is what they test.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import TEXT_DOCS, doc_id_of
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from findex.cli import app as cli
from findex.corpus import iter_documents
from findex.index import Index, build_index
from findex.snippets import tidy_rst
from findex.store import save
from findex.web.app import create_app
from findex.web.deps import get_holder, get_index
from findex.web.logs import JsonFormatter, RequestIdFilter, logging_config
from findex.web.service import IndexHolder, highlight, run_search
from findex.web.settings import Settings
from findex.web.ui import page_numbers

runner = CliRunner()


def make_client(index: Index | None, *, loading_error: str | None = None) -> TestClient:
    """An app whose routes see ``index`` — no lifespan, no file."""
    app = create_app(Settings(index_path=Path("does-not-exist.pkl")))
    holder = IndexHolder(Path("fixture"), index=index, error=loading_error)

    async def fixture_holder() -> IndexHolder:
        return holder

    app.dependency_overrides[get_holder] = fixture_holder
    if index is not None:
        app.dependency_overrides[get_index] = lambda: index
    return TestClient(app)


@pytest.fixture
def client(corpus_index: Index) -> Iterator[TestClient]:
    # Not entered with `with`: that would run the lifespan, which wants a real file.
    yield make_client(corpus_index)


# --- /search -----------------------------------------------------------------------
def test_search_returns_ranked_hits_with_snippets(client: TestClient) -> None:
    r = client.get("/search", params={"q": '"event loop"', "k": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == '"event loop"' and body["total"] >= 1 and body["page"] == 1
    top = body["results"][0]
    assert top["rank"] == 1 and top["title"] == "Asyncio" and top["path"].endswith("asyncio.md")
    assert any(part["hit"] and part["text"].lower() == "event" for part in top["snippet"])
    assert r.headers["x-request-id"]


def test_search_paginates(client: TestClient) -> None:
    all_hits = client.get("/search", params={"q": "needle OR loop OR sort", "k": 50}).json()
    total = all_hits["total"]
    assert total >= 3
    page1 = client.get("/search", params={"q": "needle OR loop OR sort", "k": 2}).json()
    page2 = client.get("/search", params={"q": "needle OR loop OR sort", "k": 2, "page": 2}).json()
    assert page1["pages"] == page2["pages"] == -(-total // 2)
    assert [h["rank"] for h in page2["results"]] == [3, 4][: len(page2["results"])]
    ids = [h["doc_id"] for h in page1["results"] + page2["results"]]
    assert ids == [h["doc_id"] for h in all_hits["results"]][: len(ids)]
    beyond = client.get("/search", params={"q": "needle", "k": 10, "page": 9}).json()
    assert beyond["results"] == [] and beyond["total"] == 2


def test_tfidf_scorer_and_no_match(client: TestClient) -> None:
    r = client.get("/search", params={"q": "stable", "scorer": "tfidf"})
    assert r.status_code == 200 and r.json()["scorer"] == "tfidf" and r.json()["total"] == 1
    empty = client.get("/search", params={"q": "zzzqqq"}).json()
    assert empty["total"] == 0 and empty["pages"] == 0 and empty["results"] == []


@pytest.mark.parametrize(
    "params",
    [
        {},  # q is required
        {"q": ""},
        {"q": "x" * 501},
        {"q": "loop", "k": 0},
        {"q": "loop", "k": 51},
        {"q": "loop", "k": "ten"},
        {"q": "loop", "page": 0},
        {"q": "loop", "page": 101},
        {"q": "loop", "scorer": "pagerank"},
    ],
)
def test_search_validation_is_a_422(client: TestClient, params: dict[str, object]) -> None:
    r = client.get("/search", params=params)  # type: ignore[arg-type]
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][0] == "query"  # Pydantic's report, as JSON


@pytest.mark.parametrize(("q", "message"), [("a OR", "bad query"), ('"open', "unclosed quote")])
def test_unparseable_query_is_a_400(client: TestClient, q: str, message: str) -> None:
    r = client.get("/search", params={"q": q})
    assert r.status_code == 400 and message in r.json()["detail"]


def test_phrase_without_positions_is_a_400(corpus_dir: Path) -> None:
    ix = build_index(iter_documents(corpus_dir))  # no positions
    r = make_client(ix).get("/search", params={"q": '"event loop"'})
    assert r.status_code == 400 and "positions" in r.json()["detail"]


# --- /docs/{id}, /stats --------------------------------------------------------------
def test_document_and_404(client: TestClient, corpus_index: Index) -> None:
    doc = doc_id_of(corpus_index, "sorting.txt")
    r = client.get(f"/docs/{doc}")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "sub/sorting.txt" and "stable" in body["text"] and body["length"] > 5
    missing = client.get("/docs/9999")
    assert missing.status_code == 404 and missing.json() == {"detail": "no document with id 9999"}
    assert client.get("/docs/-1").status_code == 422
    assert client.get("/docs/abc").status_code == 422


def test_document_without_corpus_has_null_text(corpus_index: Index) -> None:
    corpus_index.meta["root"] = "/nowhere"
    assert make_client(corpus_index).get("/docs/0").json()["text"] is None


def test_stats(client: TestClient) -> None:
    body = client.get("/stats").json()
    assert body["documents"] == TEXT_DOCS and body["vocabulary"] > 20 and body["positions"] is True
    assert body["uptime_s"] >= 0 and body["index_bytes"] == 0  # the configured file doesn't exist


def test_openapi_page_documents_the_api(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200  # Swagger UI
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {"/search", "/docs/{doc_id}", "/stats", "/health"}  # the UI is hidden


# --- /health and 503 ----------------------------------------------------------------
def test_health_ok(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {"status": "ok", "documents": TEXT_DOCS, "semantic": False}


def test_health_is_503_until_the_index_is_loaded() -> None:
    app = create_app(Settings())
    c = TestClient(app)  # not entered: lifespan never ran, nothing is loaded
    r = c.get("/health")
    assert r.status_code == 503 and r.json() == {"status": "loading"}
    s = c.get("/search", params={"q": "loop"})
    assert s.status_code == 503 and s.headers["retry-after"] == "5"
    assert c.get("/docs/0").status_code == 503 and c.get("/stats").status_code == 503


def test_health_reports_a_failed_load() -> None:
    r = make_client(None, loading_error="UnpicklingError: truncated").get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "failed", "detail": "UnpicklingError: truncated"}


# --- errors: request id, clean 500 --------------------------------------------------
class Broken:
    """An "index" whose every method fails: a bug, not a bad request."""

    def match(self, *args: object) -> object:
        raise RuntimeError("secret internal detail")


def test_unexpected_error_is_a_clean_500_with_request_id(
    corpus_index: Index, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(Settings())
    app.dependency_overrides[get_index] = lambda: Broken()
    c = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="findex.web"):
        r = c.get("/search", params={"q": "loop"}, headers={"X-Request-ID": "req-42"})
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error", "request_id": "req-42"}
    assert "secret" not in r.text and "Traceback" not in r.text  # nothing leaks out
    assert r.headers["x-request-id"] == "req-42"
    record = next(rec for rec in caplog.records if rec.name == "findex.web")
    assert record.exc_info is not None  # the traceback goes to the log, not to the client
    assert "GET /search" in record.getMessage() and "req-42" in record.getMessage()


def test_request_id_is_generated_and_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="findex.web.access"):
        r = client.get("/stats")
    rid = r.headers["x-request-id"]
    assert len(rid) == 12
    assert any("GET /stats 200" in rec.getMessage() for rec in caplog.records)


# --- the web page --------------------------------------------------------------------
def test_home_page_and_results(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200 and "<form" in home.text and "Спробуйте" in home.text
    results = client.get("/", params={"q": '"event loop"'})
    assert results.status_code == 200
    assert "<mark>event</mark>" in results.text.lower() and "/doc/" in results.text
    assert 'class="score"' in results.text


def test_htmx_gets_only_the_fragment(client: TestClient) -> None:
    r = client.get("/", params={"q": "needle"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text and 'class="hits"' in r.text


def test_page_shows_query_errors_and_pager(client: TestClient) -> None:
    bad = client.get("/", params={"q": "a OR"})
    assert bad.status_code == 200 and "notice--error" in bad.text
    paged = client.get("/", params={"q": "needle OR loop OR sort", "k": 1, "page": 2})
    assert 'aria-current="page">2<' in paged.text and 'rel="prev"' in paged.text


def test_document_page_highlights_and_404(client: TestClient, corpus_index: Index) -> None:
    doc = doc_id_of(corpus_index, "asyncio.md")
    r = client.get(f"/doc/{doc}", params={"q": '"event loop"'})
    assert r.status_code == 200 and '<mark id="hit">' in r.text and "<h1>Asyncio</h1>" in r.text
    missing = client.get("/doc/777")
    assert missing.status_code == 404 and "777" in missing.text


def test_static_css(client: TestClient) -> None:
    r = client.get("/static/style.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]


# --- lifespan with a real file -------------------------------------------------------
def test_lifespan_loads_the_index_once(corpus_index: Index, tmp_path: Path) -> None:
    path = tmp_path / "ix.pkl"
    save(corpus_index, path)
    app = create_app(Settings(index_path=path))
    with TestClient(app) as c:
        deadline = time.monotonic() + 10
        while c.get("/health").status_code == 503 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/search", params={"q": "needle"}).json()["total"] == 2
        assert c.get("/stats").json()["index_bytes"] == path.stat().st_size


def test_lifespan_fails_fast_on_a_missing_index(tmp_path: Path) -> None:
    app = create_app(Settings(index_path=tmp_path / "missing.pkl"))
    with pytest.raises(RuntimeError, match="no such file"), TestClient(app):
        pass


def test_a_corrupt_index_is_reported_by_health(tmp_path: Path) -> None:
    path = tmp_path / "broken.pkl"
    path.write_bytes(b"not a pickle")
    with TestClient(create_app(Settings(index_path=path))) as c:
        deadline = time.monotonic() + 10
        while c.get("/health").json()["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(0.02)
        r = c.get("/health")
    assert (
        r.status_code == 503 and r.json()["status"] == "failed" and "pickle" in r.json()["detail"]
    )


# --- settings, logging, CLI ----------------------------------------------------------
def test_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("INDEX_PATH", "/srv/ix.pkl")
    monkeypatch.setenv("LOG_FORMAT", "json")
    s = Settings()
    assert s.index_path == Path("/srv/ix.pkl") and s.log_format == "json"
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError, match="log_level"):
        Settings()


def test_json_log_lines() -> None:
    record = logging.LogRecord("findex.web", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    RequestIdFilter().filter(record)
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "hello x" and line["request_id"] == "-" and line["level"] == "INFO"
    config = logging_config("DEBUG", "json")
    assert (
        config["handlers"]["stdout"]["formatter"] == "json" and config["root"]["level"] == "DEBUG"
    )


def test_serve_checks_its_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INDEX_PATH", raising=False)  # `serve --index` sets it: restore after
    r = runner.invoke(cli, ["serve", "--index", str(tmp_path / "none.pkl")])
    assert r.exit_code == 1 and "index file not found" in r.stderr
    (tmp_path / "ix.pkl").write_bytes(b"x")
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    r = runner.invoke(cli, ["serve", "--index", str(tmp_path / "ix.pkl")])
    assert r.exit_code == 1 and "LOG_LEVEL" in r.stderr


# --- the service layer, directly -----------------------------------------------------
def test_run_search_window_and_highlight(corpus_index: Index) -> None:
    page = run_search(corpus_index, "needle", k=1, page=2)
    assert page.total == 2 and page.pages == 2 and [h.rank for h in page.hits] == [2]
    parts = highlight("The Event loop, event-loop.", frozenset({"event"}))
    assert [t for t, hit in parts if hit] == ["Event", "event"]
    assert highlight("text", frozenset()) == [("text", False)]


def test_tidy_rst_and_pager() -> None:
    parts = tidy_rst([("… .. currentmodule:: ", False), ("asyncio", True),
                      (" :func:`asyncio.run` and **bold** ===== x", False)])  # fmt: skip
    assert parts == [("… ", False), ("asyncio", True), (" asyncio.run and bold x", False)]
    assert page_numbers(1, 1) == [1]
    assert page_numbers(6, 20) == [1, None, 4, 5, 6, 7, 8, None, 20]
    assert page_numbers(2, 4) == [1, 2, 3, 4]


def test_fast_snippet_matches_the_slow_one() -> None:
    from findex.snippets import fast_snippet, snippet_parts

    text = "filler " * 200 + "The event loop runs; the Event Loop waits. " + "tail " * 200
    terms = frozenset({"event", "loop"})
    fast, slow = fast_snippet(text, terms), snippet_parts(text, terms)
    assert [t for t, hit in fast if hit] == [t for t, hit in slow if hit]
    assert fast[0] == ("… ", False) and fast[-1] == (" …", False)
    # only Unicode normalization finds this one: the regex misses, the exact path doesn't
    assert [t for t, hit in fast_snippet("Die Straße", frozenset({"strasse"})) if hit] == ["Straße"]
    assert fast_snippet("x", frozenset()) == snippet_parts("x", frozenset())
