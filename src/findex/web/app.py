"""The ASGI application: ``findex.web.app:app`` (``findex serve`` runs it).

The path of a request: uvicorn reads the socket and calls this ASGI app →
:class:`RequestContext` (request id, access log, last-resort 500) → CORS →
Starlette's router → FastAPI resolves the dependencies (``Depends``) and
validates the query parameters (Pydantic; a bad one is a 422 right here) →
the route function (a ``def`` one in the thread pool) → its return value is
validated against ``response_model`` and serialized → back out the same way.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from findex.web import api, ui
from findex.web.logs import request_id
from findex.web.service import IndexHolder
from findex.web.settings import Settings, get_settings

log = logging.getLogger("findex.web")
access = logging.getLogger("findex.web.access")


def _version() -> str:
    try:
        return version("findex")
    except PackageNotFoundError:  # a source tree that isn't installed
        return "0+unknown"


VERSION = _version()


class RequestContext:
    """Pure ASGI middleware: request id, one access-log line, and a clean 500.

    Any exception a route didn't turn into an HTTP error is logged here *with
    its traceback and the request id*, and the client gets
    ``{"detail": "internal server error", "request_id": …}`` — never the stack.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming[:64] if incoming.isprintable() and incoming else uuid.uuid4().hex[:12]
        token = request_id.set(rid)
        t0 = time.perf_counter()
        status = 0

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception(
                "unhandled error in %s %s (request %s)", scope["method"], scope["path"], rid
            )
            if status == 0:  # nothing sent yet: we can still answer properly
                status = 500
                body = f'{{"detail":"internal server error","request_id":"{rid}"}}'.encode()
                await send({
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"x-request-id", rid.encode())],
                })  # fmt: skip
                await send({"type": "http.response.body", "body": body})
        finally:
            query = scope.get("query_string", b"").decode("latin-1")
            target = scope["path"] + (f"?{query}" if query else "")
            ms = (time.perf_counter() - t0) * 1000
            access.info("%s %s %d %.1f ms", scope["method"], target, status, ms)
            request_id.reset(token)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Load the index once per process; serve ``/health`` 503 until it's in.

    A missing ``INDEX_PATH`` stops the server before it accepts a connection
    (a misconfigured deploy should fail loudly). The load itself runs in a
    thread, so the port opens at once and the platform sees "loading", not a
    dead process.
    """
    settings: Settings = app.state.settings or get_settings()
    path = settings.index_path
    if not path.is_file():
        raise RuntimeError(f"INDEX_PATH={path}: no such file")
    holder = IndexHolder(path)
    app.state.holder = holder
    log.info("findex %s: loading index %s", VERSION, path)
    loader = asyncio.create_task(asyncio.to_thread(holder.load), name="load-index")
    try:
        yield
    finally:
        await loader  # a thread can't be interrupted; it ends on its own
        holder.close()
        log.info("shut down")


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="findex",
        version=VERSION,
        summary="Full-text search over the Python documentation: BM25, phrases, Boolean queries.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Outermost first: the request id must cover everything below it.
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
    )
    app.add_middleware(RequestContext)
    app.include_router(api.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=ui.HERE / "static"), name="static")
    return app


app = create_app()
