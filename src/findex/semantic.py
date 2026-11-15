"""Semantic search: embeddings, cosine similarity, and RRF hybrid ranking (Lab 8).

An **embedding** is a vector that stands for what a piece of text *means*:
``all-MiniLM-L6-v2`` maps any text to 384 numbers so that texts about the
same thing land close together, whether or not they share a word. With unit
vectors, closeness is the cosine, and the cosine is a dot product.

``findex embed INDEX`` cuts every document into chunks of ~``max_words``
words, embeds each chunk, normalizes it and stores the ``float32`` matrix in a
``.npy`` file (plus ``.chunks.npy``: which document and character span each
row is). At query time::

    q      = embed(query)                         # (384,), unit length
    sims   = vectors @ q                          # (chunks,): one matrix-vector product
    doc[d] = max(sims[first[d] : first[d + 1]])   # a document is as good as its best chunk

``mode=hybrid`` fuses the BM25 ranking and the semantic ranking with
**Reciprocal Rank Fusion**: ``score(d) = Σ 1 / (60 + rank_i(d))``. RRF uses
only the *ranks*, so it needs no calibration between a BM25 score (unbounded)
and a cosine (−1..1), and a document near the top of either list rises.

The model runs with ``onnxruntime`` from the ONNX export (no PyTorch): the
tokenizer is the model's own ``tokenizer.json`` (HF ``tokenizers``), pooling
is the mean of the token vectors weighted by the attention mask, then L2
normalization — exactly what ``sentence-transformers`` does for this model.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from findex.index import Index
from findex.rank import SearchResult
from findex.snippets import document_text
from findex.tokenize import tokenize

log = logging.getLogger(__name__)

# A plain assignment, not a PEP 695 `type`: typer and FastAPI read it at runtime.
Mode = Literal["lexical", "semantic", "hybrid"]
MODES: tuple[Mode, ...] = ("lexical", "semantic", "hybrid")

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
# The ONNX export published as Xenova/all-MiniLM-L6-v2 at a pinned revision;
# scripts/get_model.sh downloads exactly these bytes and checks the hashes.
MODEL_REVISION = "751bff37182d3f1213fa05d7196b954e230abad9"
MODEL_FILES = {
    "model.onnx": "759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e",
    "tokenizer.json": "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0",
}
RRF_K = 60  # the constant from Cormack et al. (2009); larger = flatter fusion

type F32 = NDArray[np.float32]
type Span = tuple[int, int]


class Embedder(Protocol):
    """Anything that turns texts into unit-length rows of a float32 matrix."""

    name: str

    @property
    def dim(self) -> int: ...

    def encode(self, texts: Sequence[str]) -> F32: ...


def check_model(model_dir: Path) -> None:
    """Refuse model files whose SHA-256 isn't the pinned one."""
    for name, expected in MODEL_FILES.items():
        path = model_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing (run scripts/get_model.sh)")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"{path}: sha256 {digest[:12]}… is not the pinned {expected[:12]}…")


HF_BASE = f"https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/{MODEL_REVISION}"
HF_PATHS = {"model.onnx": "onnx/model.onnx", "tokenizer.json": "tokenizer.json"}


def download_model(model_dir: Path) -> None:
    """Fetch the pinned model files from Hugging Face and verify their SHA-256."""
    import urllib.request

    model_dir.mkdir(parents=True, exist_ok=True)
    for name, expected in MODEL_FILES.items():
        target = model_dir / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        url = f"{HF_BASE}/{HF_PATHS[name]}"
        print(f"downloading {url}", flush=True)
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
            data = resp.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            raise ValueError(f"{url}: sha256 {digest} is not the pinned {expected}")
        target.write_bytes(data)
    check_model(model_dir)


class OnnxEmbedder:
    """all-MiniLM-L6-v2 on onnxruntime: tokenize, run, mean-pool, normalize."""

    name = MODEL_NAME

    def __init__(
        self, model_dir: Path, *, max_length: int = 256, batch_size: int = 32,
        threads: int | None = None, verify: bool = True,
    ) -> None:  # fmt: skip
        # heavy, imported only when semantic search is used; onnxruntime ships no
        # type information, so it is handled as `Any` on purpose
        ort: Any = importlib.import_module("onnxruntime")
        from tokenizers import Tokenizer

        if verify:
            check_model(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=max_length)  # pyright: ignore[reportUnknownMemberType]
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")  # pyright: ignore[reportUnknownMemberType]
        opts: Any = ort.SessionOptions()
        opts.intra_op_num_threads = threads or (os.cpu_count() or 1)
        opts.inter_op_num_threads = 1
        self.session: Any = ort.InferenceSession(  # onnxruntime ships no type hints
            str(model_dir / "model.onnx"), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.batch_size = batch_size

    @property
    def dim(self) -> int:
        return 384

    def encode(self, texts: Sequence[str]) -> F32:
        out = np.empty((len(texts), self.dim), dtype=np.float32)
        for start in range(0, len(texts), self.batch_size):
            batch = self.tokenizer.encode_batch(list(texts[start : start + self.batch_size]))
            ids = np.array([e.ids for e in batch], dtype=np.int64)
            mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
            hidden: F32 = self.session.run(["last_hidden_state"], feeds)[0]
            m = mask[:, :, None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            out[start : start + len(batch)] = pooled
        return normalize_rows(out)


class HashingEmbedder:
    """A deterministic stand-in for tests: hashed bag of words, no model.

    Texts that share words get similar vectors — enough to test the plumbing
    (chunking, max-over-chunks, RRF, the API) without a 90 MB download.
    """

    name = "hashing-test-embedder"

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str]) -> F32:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in tokenize(text):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "little")
                out[row, h % self._dim] += 1.0
        return normalize_rows(out)


def normalize_rows(m: F32) -> F32:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.where(norms == 0, 1, norms)).astype(np.float32)


# --- chunking --------------------------------------------------------------------------
_WORD = re.compile(r"\S+")
_RST = [
    (re.compile(r"^\s*\.\. [\w:-]+::.*$", re.M), " "),  # .. directive:: args
    (re.compile(r"^\s*\.\. _[^:]+:\s*$", re.M), " "),  # .. _label:
    (re.compile(r":[A-Za-z][\w:+.-]*:`[!~]?([^`<]*?)(?:\s*<[^>]*>)?`"), r"\1"),  # :role:`x <y>`
    (re.compile(r"``?|\*\*?|`_"), ""),  # literals, emphasis, link ends
    (re.compile(r"^\s*([=\-~^#*+\"'.:_`])\1{2,}\s*$", re.M), " "),  # section underlines
    (re.compile(r"\s+"), " "),
]


def clean_rst(text: str) -> str:
    """Text without reST markup — what the model should read."""
    for pattern, repl in _RST:
        text = pattern.sub(repl, text)
    return text.strip()


def chunk_spans(text: str, max_words: int = 200) -> list[Span]:
    """Character spans of consecutive runs of ``max_words`` words (no overlap)."""
    words = [m.span() for m in _WORD.finditer(text)]
    return [
        (words[i][0], words[min(i + max_words, len(words)) - 1][1])
        for i in range(0, len(words), max_words)
    ]


# --- the embedding store -----------------------------------------------------------------
@dataclass(slots=True)
class Embeddings:
    """``vectors[i]`` is chunk i: unit-length, ``float32``; chunks grouped by document."""

    vectors: F32  # (chunks, dim)
    chunk_doc: NDArray[np.int32]  # (chunks,) document of each chunk, ascending
    chunk_span: NDArray[np.int32]  # (chunks, 2) [start, end) in the document text
    first: NDArray[np.int64]  # (docs + 1,) rows of doc d: first[d] : first[d + 1]
    model: str
    num_docs: int

    @classmethod
    def build(
        cls, chunk_doc: NDArray[np.int32], chunk_span: NDArray[np.int32], vectors: F32,
        num_docs: int, model: str,
    ) -> Embeddings:  # fmt: skip
        counts = np.bincount(chunk_doc, minlength=num_docs)
        first = np.zeros(num_docs + 1, dtype=np.int64)
        first[1:] = np.cumsum(counts)
        return cls(vectors, chunk_doc, chunk_span, first, model, num_docs)

    def save(self, path: Path) -> None:
        """``path`` (.npy) holds the matrix; ``.chunks.npy`` and ``.json`` sit beside it."""
        np.save(path, self.vectors)
        table = np.column_stack([self.chunk_doc, self.chunk_span]).astype(np.int32)
        np.save(sidecar(path, ".chunks.npy"), table)
        meta = {"model": self.model, "num_docs": self.num_docs, "chunks": len(self.vectors),
                "dim": int(self.vectors.shape[1])}  # fmt: skip
        sidecar(path, ".json").write_text(json.dumps(meta, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Embeddings:
        vectors: F32 = np.load(path)
        table: NDArray[np.int32] = np.load(sidecar(path, ".chunks.npy"))
        meta = json.loads(sidecar(path, ".json").read_text())
        return cls.build(table[:, 0].copy(), table[:, 1:].copy(), vectors,
                         int(meta["num_docs"]), str(meta["model"]))  # fmt: skip

    def doc_scores(self, q: F32) -> tuple[NDArray[np.float32], NDArray[np.int64]]:
        """Best cosine per document (−inf if it has no chunk) and the row it came from."""
        sims = self.vectors @ q  # one BLAS matrix-vector product over every chunk
        has = self.first[1:] > self.first[:-1]
        best = np.full(self.num_docs, -np.inf, dtype=np.float32)
        best[has] = np.maximum.reduceat(sims, self.first[:-1][has])
        # the row of each document's best chunk: per-row "is this the doc's max?"
        is_max = sims == best[self.chunk_doc]
        rows = np.full(self.num_docs, -1, dtype=np.int64)
        hit = np.flatnonzero(is_max)
        rows[self.chunk_doc[hit][::-1]] = hit[::-1]  # first maximal row wins
        return best, rows


def sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name.removesuffix(".npy") + suffix)


def embed_index(
    index: Index,
    embedder: Embedder,
    *,
    root: Path | None = None,
    max_words: int = 200,
    on_progress: Callable[[int, int], None] | None = None,
) -> Embeddings:
    """Chunk every document of ``index`` and embed the chunks (in doc-id order)."""
    docs: list[int] = []
    spans: list[Span] = []
    texts: list[str] = []
    for doc_id in range(index.num_docs):
        text = document_text(index, doc_id, root) or ""
        for span in chunk_spans(text, max_words):
            cleaned = clean_rst(text[span[0] : span[1]])
            if cleaned:
                docs.append(doc_id)
                spans.append(span)
                texts.append(cleaned)
    vectors = np.empty((len(texts), embedder.dim), dtype=np.float32)
    step = 256
    t0 = time.perf_counter()
    for start in range(0, len(texts), step):
        vectors[start : start + step] = embedder.encode(texts[start : start + step])
        if on_progress is not None:
            on_progress(min(start + step, len(texts)), len(texts))
    log.info("embedded %d chunks of %d documents in %.1f s", len(texts), index.num_docs,
             time.perf_counter() - t0)  # fmt: skip
    return Embeddings.build(
        np.array(docs, dtype=np.int32), np.array(spans, dtype=np.int32).reshape(-1, 2),
        vectors, index.num_docs, embedder.name,
    )  # fmt: skip


# --- ranking -------------------------------------------------------------------------------
_OPERATORS = re.compile(r'\b(?:AND|OR|NOT)\b|["()]')


def query_text(query: str) -> str:
    """What the model reads: the query's words, without Boolean syntax."""
    return " ".join(_OPERATORS.sub(" ", query).split())


@dataclass(frozen=True, slots=True)
class Ranked:
    """A ranking plus, for semantic hits, the span of the best chunk (for snippets)."""

    results: list[SearchResult]
    spans: dict[int, Span]


def semantic_rank(index: Index, emb: Embeddings, embedder: Embedder, query: str, k: int) -> Ranked:
    text = query_text(query)
    if not text or k <= 0:
        return Ranked([], {})
    q = embedder.encode([text])[0]
    best, rows = emb.doc_scores(q)
    candidates = np.flatnonzero(np.isfinite(best))
    if candidates.size == 0:
        return Ranked([], {})
    kk = min(k, candidates.size)
    part = candidates[np.argpartition(-best[candidates], kk - 1)[:kk]]
    order = part[np.lexsort((part, -best[part]))]
    results = [SearchResult(int(d), float(best[d]), index.title(int(d))) for d in order]
    spans = {int(d): (int(emb.chunk_span[rows[d], 0]), int(emb.chunk_span[rows[d], 1]))
             for d in order}  # fmt: skip
    return Ranked(results, spans)


def rrf(rankings: Iterable[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: ``Σ 1/(k + rank)`` over the lists, best first."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            fused[doc] = fused.get(doc, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


def hybrid_rank(index: Index, lexical: Sequence[SearchResult], semantic: Ranked, k: int) -> Ranked:
    fused = rrf([[r.doc_id for r in lexical], [r.doc_id for r in semantic.results]])[:k]
    results = [SearchResult(d, s, index.title(d)) for d, s in fused]
    return Ranked(results, {d: semantic.spans[d] for d, _ in fused if d in semantic.spans})


if __name__ == "__main__":  # python -m findex.semantic download [DIR]
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "download":
        target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("models/all-MiniLM-L6-v2")
        download_model(target)
        print(f"{target}: model files present and verified")
    else:
        print("usage: python -m findex.semantic download [DIR]")
