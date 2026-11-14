#!/usr/bin/env bash
# Builds a local mirror of docs.python.org into data/mirror/: the CPython
# documentation sources (Doc/) rendered to HTML with the project's own Sphinx
# setup — the same pages the real site serves. Lab 6 crawls it through
#   uv run python -m findex.crawler.mirror data/mirror --port 8000
# (Faster alternative: unzip the "HTML" archive from
#  https://docs.python.org/3/download.html into data/mirror/.)
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
git clone --depth 1 --filter=blob:none --sparse https://github.com/python/cpython.git "$tmp/cpython"
git -C "$tmp/cpython" sparse-checkout set Doc Include Misc   # conf.py reads Include/patchlevel.h
uv venv -q "$tmp/venv"
VIRTUAL_ENV="$tmp/venv" uv pip install -q -r "$tmp/cpython/Doc/requirements.txt"
mkdir -p data
(cd "$tmp/cpython/Doc" && "$tmp/venv/bin/sphinx-build" -q -b html -j auto . "$OLDPWD/data/mirror")
rm -rf "$tmp"
echo "mirror: $(find data/mirror -name '*.html' | wc -l) HTML pages in data/mirror"
