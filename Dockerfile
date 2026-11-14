# findex — the search API and web page in one image.
#
#   docker build -t findex .
#   docker run --rm -p 8000:8000 findex                       # the baked-in index
#   docker run --rm -p 8000:8000 -v "$PWD/crawl.pkl:/srv/crawl.pkl:ro" \
#       -e INDEX_PATH=/srv/crawl.pkl findex                    # any other index
#
# Three stages: `deps` installs the locked dependencies and findex itself into
# /app/.venv with uv; `corpus` fetches the CPython documentation sources (the
# same pinned commit as in the README) and builds the index with findex; the
# final image copies only the venv, the corpus and the index onto a slim
# Python base and runs as an unprivileged user. Config comes from the
# environment only (INDEX_PATH, CORPUS_ROOT, PORT, LOG_LEVEL, LOG_FORMAT).

ARG PYTHON_IMAGE=python:3.13-slim

# ---------------------------------------------------------------- 1. deps
FROM ${PYTHON_IMAGE} AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, from the lockfile alone: this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
# Then findex itself, as a regular (non-editable) package in the same venv.
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# -------------------------------------------------------------- 2. corpus
FROM ${PYTHON_IMAGE} AS corpus
ARG CPYTHON_COMMIT=386d9d071cee2168bee135bc15897a27377d0999
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/cpython
RUN git init -q \
 && git remote add origin https://github.com/python/cpython.git \
 && git sparse-checkout set Doc \
 && git fetch -q --depth 1 --filter=blob:none origin "${CPYTHON_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && mkdir -p /app/data/python-docs \
 && cd Doc && find . -name '*.rst' -exec cp --parents {} /app/data/python-docs/ \;
COPY --from=deps /app/.venv /app/.venv
WORKDIR /app
RUN mkdir -p /app/index \
 && /app/.venv/bin/findex index data -o /app/index/index.pkl --positions --executor serial

# ------------------------------------------------------------- 3. runtime
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 findex \
 && useradd --system --uid 10001 --gid findex --no-create-home --shell /usr/sbin/nologin findex
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY --from=corpus /app/data /app/data
COPY --from=corpus /app/index /app/index
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    INDEX_PATH=/app/index/index.pkl \
    CORPUS_ROOT=/app/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_FORMAT=json
USER findex
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=2)"]
# The CLI stays the entry point: `findex serve` reads HOST/PORT/WEB_CONCURRENCY
# and INDEX_PATH from the environment and runs uvicorn.
CMD ["findex", "serve"]
