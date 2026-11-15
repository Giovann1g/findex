"""Configuration from the environment (12-factor, rule III), validated by Pydantic.

Every setting is an environment variable (or a line in a ``.env`` file during
development); nothing about paths or ports lives in the code. The same Docker
image runs on a laptop and on Render — only the environment differs::

    INDEX_PATH=/app/index/index.pkl   the index to serve (required in practice)
    CORPUS_ROOT=/app/data             where the documents are, for snippets
                                      (default: the root stored in the index)
    EMBEDDINGS_PATH=/app/index/embeddings.npy   enables mode=semantic and hybrid
    MODEL_DIR=/app/models/all-MiniLM-L6-v2      model.onnx + tokenizer.json
    LOG_LEVEL=INFO                    DEBUG / INFO / WARNING
    LOG_FORMAT=text                   text, or json (one object per line)

A wrong type (``LOG_LEVEL=LOUD``) fails right here, at startup, with a precise
message — not on the first request. A missing index file fails in ``lifespan``
before the server accepts a connection.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    index_path: Path = Path("index.pkl")
    corpus_root: Path | None = None
    embeddings_path: Path | None = None
    model_dir: Path = Path("models/all-MiniLM-L6-v2")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"


@lru_cache
def get_settings() -> Settings:
    """The process's settings, read once (a dependency: tests override it)."""
    return Settings()
