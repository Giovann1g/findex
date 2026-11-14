"""Lab 7: findex on the web — a FastAPI app with a JSON API and a search page.

* :mod:`~findex.web.app`      — ``create_app()``, lifespan, request-id middleware
* :mod:`~findex.web.api`      — ``/search``, ``/docs/{doc_id}``, ``/stats``, ``/health``
* :mod:`~findex.web.ui`       — ``/`` and ``/doc/{doc_id}``: Jinja2 + htmx
* :mod:`~findex.web.service`  — the engine's side: ``run_search``, ``IndexHolder``
* :mod:`~findex.web.models`   — Pydantic models, at the boundary only
* :mod:`~findex.web.deps`     — ``Depends``: ``get_index``, ``get_holder``, settings
* :mod:`~findex.web.settings` — ``Settings`` from the environment
* :mod:`~findex.web.logs`     — stdout logging with the request id
* :mod:`~findex.web.bench`    — ``def`` vs ``async def`` and the oha table

Import ``findex.web.app:app`` (or run ``findex serve``); this package itself
imports nothing heavy, so ``findex`` commands that don't serve stay fast.
"""
