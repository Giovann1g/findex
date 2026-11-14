"""Dependencies (``Depends``): how a route gets the index and the settings.

Routes never reach for globals. They declare what they need, FastAPI calls
these functions per request and passes the results in — and a test swaps
any of them with ``app.dependency_overrides[get_index] = lambda: tiny_index``
without touching a route.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from findex.index import Index
from findex.web.service import IndexHolder
from findex.web.settings import Settings, get_settings


async def get_holder(request: Request) -> IndexHolder | None:
    """What ``lifespan`` put on ``app.state`` (``None`` if lifespan never ran).

    ``async def`` because it never blocks: a *sync* dependency would be run in
    the thread pool, and ``/health`` would queue behind busy searches.
    """
    holder: IndexHolder | None = getattr(request.app.state, "holder", None)
    return holder


async def get_index(holder: Annotated[IndexHolder | None, Depends(get_holder)]) -> Index:
    if holder is None or holder.index is None:
        raise HTTPException(503, "index not loaded yet", headers={"Retry-After": "5"})
    return holder.index


async def get_app_settings(request: Request) -> Settings:
    """The settings ``create_app`` was given, else the environment's."""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_settings()


IndexDep = Annotated[Index, Depends(get_index)]
HolderDep = Annotated[IndexHolder | None, Depends(get_holder)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
