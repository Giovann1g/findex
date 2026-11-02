"""Checks for the lazy corpus reader."""

import gzip
import inspect
import json
from pathlib import Path

from findex.corpus import iter_documents
from findex.stats import compute_stats, compute_stats_eager


def make_corpus(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("Hello world", encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("Привіт world", encoding="utf-8")
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9 ok")  # latin-1, not UTF-8
    with gzip.open(tmp_path / "c.txt.gz", "wt", encoding="utf-8") as f:
        f.write("zipped words")
    (tmp_path / "skip.bin").write_bytes(b"\x00\x01")
    return tmp_path


def test_is_a_generator(tmp_path):
    assert inspect.isgeneratorfunction(iter_documents)
    first = next(iter_documents(make_corpus(tmp_path)))
    assert first.text


def test_reads_tree_and_survives_bad_encoding(tmp_path):
    docs = {d.doc_id: d.text for d in iter_documents(make_corpus(tmp_path))}
    assert set(docs) == {"a.txt", "sub/b.md", "bad.txt", "c.txt.gz"}
    assert docs["bad.txt"] == "caf� ok"  # replaced, not crashed


def test_jsonl(tmp_path):
    path = tmp_path / "docs.jsonl"
    lines = [json.dumps({"id": "x", "text": "one"}), "not json", json.dumps({"text": "two"})]
    path.write_text("\n".join(lines), encoding="utf-8")
    assert [d.text for d in iter_documents(path)] == ["one", "two"]


def test_lazy_and_eager_agree(tmp_path):
    root = make_corpus(tmp_path)
    lazy = compute_stats(iter_documents(root))
    eager = compute_stats_eager(root)
    assert (lazy.documents, lazy.tokens, lazy.counts) == (
        eager.documents,
        eager.tokens,
        eager.counts,
    )
    assert lazy.counts["world"] == 2


def test_count_documents_without_reading(tmp_path):
    from findex.corpus import count_documents

    root = make_corpus(tmp_path)
    assert count_documents(root) == len(list(iter_documents(root)))
    assert count_documents(root / "a.txt") == 1  # a single file is one document
    (tmp_path / "x.jsonl").write_text("{}", encoding="utf-8")
    assert count_documents(tmp_path) is None  # JSON lines: unknown until read
    assert count_documents(tmp_path / "x.jsonl") is None


def test_single_text_file_is_a_corpus(tmp_path):
    path = tmp_path / "one.md"
    path.write_text("just one", encoding="utf-8")
    [doc] = iter_documents(path)
    assert (doc.doc_id, doc.text) == ("one.md", "just one")


def test_unreadable_file_is_skipped_with_a_warning(tmp_path, caplog):
    from findex.corpus import read_text

    assert read_text(tmp_path / "missing.txt") is None
    assert "skipping" in caplog.text


def test_stats_cli_lazy_and_eager_agree(tmp_path, capsys):
    from findex.stats import main

    root = make_corpus(tmp_path)
    main([str(root), "--top", "3"])
    lazy = capsys.readouterr().out
    main([str(root), "--top", "3", "--mode", "eager", "--no-trace"])
    eager = capsys.readouterr().out
    assert "tokens:          8" in lazy and "tokens:          8" in eager
    assert "peak memory:" in lazy and "n/a" in eager
