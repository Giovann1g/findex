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
