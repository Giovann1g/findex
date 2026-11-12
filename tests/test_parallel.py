"""Lab 5: build_partial + merge, and the three executors that run them."""

from __future__ import annotations

import importlib
import json
import pickle
import random
import tempfile
from pathlib import Path

import pytest
from conftest import TEXT_DOCS
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from findex.cli import app
from findex.corpus import Document, iter_documents, list_document_paths
from findex.index import Index, build_index
from findex.parallel import (
    Chunk,
    PartialIndex,
    build_chunk,
    build_parallel,
    build_partial,
    make_chunks,
    merge,
    run_chunks,
)

runner = CliRunner()


def as_bytes(ix: Index) -> bytes:
    return pickle.dumps(ix, protocol=pickle.HIGHEST_PROTOCOL)


def explode(chunk: Chunk) -> PartialIndex:
    """A worker that fails — module level, so a spawned process can import it."""
    if chunk.first_doc_id > 0:
        raise RuntimeError(f"worker failed on chunk starting at {chunk.first_doc_id}")
    return build_chunk(chunk)


# --- the serial path equals Lab 4 -----------------------------------------------
@pytest.mark.parametrize("positions", [False, True])
def test_serial_merge_equals_lab4_build_index(corpus_dir: Path, positions: bool) -> None:
    lab4 = build_index(iter_documents(corpus_dir), positions=positions)
    base, paths = list_document_paths(corpus_dir)
    serial = merge([build_partial(paths, base=base, positions=positions)])
    assert serial == lab4  # Mapping equality: every term, every posting
    assert (serial.doc_lengths, serial.doc_meta) == (lab4.doc_lengths, lab4.doc_meta)
    assert as_bytes(serial) == as_bytes(lab4)  # same term order, same everything


@pytest.mark.slow  # spawns processes: 0.3–0.5 s here, more on a busy CI runner
@pytest.mark.parametrize("executor", ["serial", "threads", "processes"])
def test_every_executor_builds_the_same_index(corpus_dir: Path, executor: str) -> None:
    reference = as_bytes(build_index(iter_documents(corpus_dir), positions=True))
    ix, stats = build_parallel(
        corpus_dir,
        executor=executor,  # type: ignore[arg-type]  # parametrized with the three valid names
        workers=2,
        positions=True,
        chunks_per_worker=2,
    )
    assert as_bytes(ix) == reference
    assert stats.documents == TEXT_DOCS and stats.merge_s >= 0


# --- doc-id ranges and merge -----------------------------------------------------
def test_a_big_last_file_still_gets_its_own_chunk(corpus_dir: Path) -> None:
    # The fixture's biggest file comes last in traversal order; a greedy
    # byte-only split once put all six files into a single chunk.
    base, paths = list_document_paths(corpus_dir)
    chunks = make_chunks(paths, 4, base=base)
    assert len(chunks) == 4
    biggest = max(paths, key=lambda p: p.stat().st_size)
    assert any(c.paths == (biggest,) for c in chunks)


def test_chunks_tile_the_id_range(corpus_dir: Path) -> None:
    base, paths = list_document_paths(corpus_dir)
    chunks = make_chunks(paths, 3, base=base)
    assert 1 <= len(chunks) <= 3
    assert [c.first_doc_id for c in chunks] == [0] + [
        sum(len(c.paths) for c in chunks[:i]) for i in range(1, len(chunks))
    ]
    assert [p for c in chunks for p in c.paths] == paths  # contiguous, in order, complete
    assert make_chunks([], 4, base=base) == []


def test_merge_ignores_arrival_order_and_checks_ranges(corpus_dir: Path) -> None:
    base, paths = list_document_paths(corpus_dir)
    chunks = make_chunks(paths, 4, base=base)
    partials = [build_chunk(c) for c in chunks]
    in_order = as_bytes(merge(partials))
    random.Random(1).shuffle(partials)  # as_completed order is arbitrary
    assert as_bytes(merge(partials)) == in_order
    assert len(partials) > 1
    without_first = [p for p in partials if p.first_doc_id != 0]
    with pytest.raises(ValueError, match="gap or overlap"):
        merge(without_first)


def test_array_layout_merge(corpus_dir: Path) -> None:
    lab4 = build_index(iter_documents(corpus_dir), layout="array")
    base, paths = list_document_paths(corpus_dir)
    merged = merge([build_chunk(c) for c in make_chunks(paths, 3, base=base)], layout="array")
    for term in lab4:
        assert list(merged.doc_ids(term)) == list(lab4.doc_ids(term))
        assert list(merged.tfs(term)) == list(lab4.tfs(term))


# --- failures surface, never hang ------------------------------------------------
@pytest.mark.slow
@pytest.mark.parametrize("executor", ["threads", "processes"])
def test_worker_exception_fails_the_build(corpus_dir: Path, executor: str) -> None:
    base, paths = list_document_paths(corpus_dir)
    chunks = make_chunks(paths, 3, base=base)
    with pytest.raises(RuntimeError, match="worker failed on chunk") as err:
        run_chunks(chunks, executor, 2, fn=explode)  # type: ignore[arg-type]  # valid name
    if executor == "processes":  # the worker's own traceback travels along
        assert "in explode" in str(err.value.__cause__)


def test_jsonl_corpus_cannot_be_split_by_path(tmp_path: Path) -> None:
    (tmp_path / "docs.jsonl").write_text('{"text": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="JSON-lines"):
        list_document_paths(tmp_path)


def test_python_m_findex_is_spawn_safe() -> None:
    # A spawned worker imports the parent's main module under another name;
    # importing findex.__main__ must therefore not start the CLI.
    module = importlib.import_module("findex.__main__")
    assert module.__name__ == "findex.__main__"


# --- the CLI ---------------------------------------------------------------------
@pytest.mark.slow
def test_cli_executors_write_identical_files(corpus_dir: Path, tmp_path: Path) -> None:
    outputs = {}
    for executor in ("serial", "threads", "processes"):
        out = tmp_path / f"{executor}.json"
        argv = ["index", str(corpus_dir), "-o", str(out), "--positions"]
        result = runner.invoke(app, [*argv, "--executor", executor, "-j", "2"])
        assert result.exit_code == 0, result.output
        outputs[executor] = out.read_bytes()
    assert outputs["serial"] == outputs["threads"] == outputs["processes"]
    stats = runner.invoke(app, ["stats", str(tmp_path / "processes.json"), "--json"])
    assert json.loads(stats.stdout)["documents"] == TEXT_DOCS


def test_cli_jsonl_falls_back_to_serial(tmp_path: Path) -> None:
    corpus = tmp_path / "docs.jsonl"
    corpus.write_text('{"id": "a", "text": "hello"}\n{"id": "b", "text": "world"}\n', "utf-8")
    out = tmp_path / "ix.pkl"
    result = runner.invoke(app, ["index", str(corpus), "-o", str(out), "--executor", "threads"])
    assert result.exit_code == 0
    stderr = " ".join(result.stderr.split())  # rich wraps long log lines
    assert "indexing serially" in stderr and "2 documents" in stderr


# --- property: any chunking of any corpus merges to the serial index -------------
@settings(max_examples=40, deadline=None)
@given(
    texts=st.lists(
        st.lists(st.sampled_from(["cat", "dog", "Straße", "п'ять", "loop"]), max_size=8).map(
            " ".join
        ),
        min_size=1,
        max_size=10,
    ),
    n_chunks=st.integers(1, 6),
    positions=st.booleans(),
)
def test_any_chunking_merges_to_the_serial_index(
    texts: list[str], n_chunks: int, positions: bool
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i, text in enumerate(texts):
            (root / f"d{i:02}.txt").write_text(text, encoding="utf-8")
        base, paths = list_document_paths(root)
        reference = build_index(
            (Document(p.relative_to(base).as_posix(), p, p.read_text("utf-8")) for p in paths),
            positions=positions,
        )
        chunks = make_chunks(paths, n_chunks, base=base, positions=positions)
        merged = merge(build_chunk(c) for c in reversed(chunks))
    assert as_bytes(merged) == as_bytes(reference)
