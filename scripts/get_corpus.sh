#!/usr/bin/env bash
# Downloads the corpus (CPython documentation sources, *.rst) into data/python-docs.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
git clone --depth 1 --filter=blob:none --sparse https://github.com/python/cpython.git "$tmp/cpython"
git -C "$tmp/cpython" sparse-checkout set Doc
mkdir -p data/python-docs
(cd "$tmp/cpython/Doc" && find . -name '*.rst' -exec cp --parents {} "$OLDPWD/data/python-docs/" \;)
rm -rf "$tmp"
echo "corpus: $(find data/python-docs -name '*.rst' | wc -l) files"
