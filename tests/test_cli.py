"""The ``findex`` command, driven through typer's CliRunner (no subprocess)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import TEXT_DOCS
from typer.testing import CliRunner

from findex.cli import app

runner = CliRunner()


@pytest.fixture
def index_file(corpus_dir: Path, tmp_path: Path) -> Path:
    out = tmp_path / "ix.pkl"
    result = runner.invoke(app, ["index", str(corpus_dir), "--out", str(out), "--positions"])
    assert result.exit_code == 0, result.output
    return out


def test_help_lists_the_three_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("index", "search", "stats"):
        assert command in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.stdout.startswith("findex ")


def test_index_reports_on_stderr_only(corpus_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "ix.json"
    result = runner.invoke(app, ["index", str(corpus_dir), "-o", str(out)])
    assert result.exit_code == 0
    assert out.is_file()
    assert result.stdout == ""  # nothing pollutes stdout
    assert f"{TEXT_DOCS} documents" in result.stderr


def test_index_limit(corpus_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "ix.bin"
    result = runner.invoke(app, ["index", str(corpus_dir), "-o", str(out), "--limit", "2"])
    assert result.exit_code == 0
    assert "2 documents" in result.stderr


def test_search_table(index_file: Path) -> None:
    result = runner.invoke(app, ["search", str(index_file), '"event loop"', "-k", "5"])
    assert result.exit_code == 0, result.stderr
    assert "2 matching documents" in result.stdout
    assert "Asyncio" in result.stdout


def test_search_json_is_pipeable(index_file: Path) -> None:
    result = runner.invoke(app, ["search", str(index_file), "event", "--json", "--scorer", "tfidf"])
    assert result.exit_code == 0
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert {"doc_id", "score", "title", "path", "snippet"} <= rows[0].keys()
    assert rows[0]["score"] >= rows[-1]["score"]


def test_search_no_match_is_not_an_error(index_file: Path) -> None:
    result = runner.invoke(app, ["search", str(index_file), "zebra"])
    assert result.exit_code == 0
    assert "no documents match" in result.stderr


def test_stats_json(index_file: Path) -> None:
    result = runner.invoke(app, ["stats", str(index_file), "--json", "--top", "3"])
    assert result.exit_code == 0
    stats = json.loads(result.stdout)
    assert stats["documents"] == TEXT_DOCS and stats["positions"] is True
    assert [t["term"] for t in stats["top_terms"]][0] == "filler"


def test_stats_table(index_file: Path) -> None:
    result = runner.invoke(app, ["stats", str(index_file)])
    assert result.exit_code == 0 and "vocabulary" in result.stdout


def test_verbose_flags_raise_the_log_level(index_file: Path) -> None:
    quiet = runner.invoke(app, ["search", str(index_file), "event"])
    debug = runner.invoke(app, ["-vv", "search", str(index_file), "event"])
    assert "took" not in quiet.stderr
    assert "load took" in debug.stderr and "match cache" in debug.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["search", "missing.pkl", "x"], "index file not found"),
        (["search", "{index}", "a OR"], "bad query"),
        (["search", "{index}", '"open'], "unclosed quote"),
        (["search", "{index}", "—"], "empty query"),
        (["search", "{index}", "x", "--b", "2"], "0 <= b <= 1"),
        (["search", "README.md", "x"], "can't tell the index format"),
        (["stats", "missing.pkl"], "index file not found"),
        (["index", "no/such/dir", "-o", "x.pkl"], "corpus not found"),
        (["index", "{corpus}", "-o", "x.bin", "--positions"], "no room for positions"),
        (["index", "{corpus}", "-o", "no/such/dir/x.pkl"], "output directory does not exist"),
    ],
)
def test_expected_errors_are_one_line_and_nonzero(
    args: list[str], message: str, index_file: Path, corpus_dir: Path
) -> None:
    argv = [a.format(index=index_file, corpus=corpus_dir) for a in args]
    result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")
    assert message in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1  # one line, no traceback
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_phrase_on_an_index_without_positions(corpus_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "plain.pkl"
    assert runner.invoke(app, ["index", str(corpus_dir), "-o", str(out)]).exit_code == 0
    result = runner.invoke(app, ["search", str(out), '"event loop"'])
    assert result.exit_code == 1 and "needs token positions" in result.stderr
