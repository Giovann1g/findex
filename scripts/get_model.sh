#!/usr/bin/env bash
# Downloads all-MiniLM-L6-v2 (ONNX export, pinned revision) into models/ and
# checks both files' SHA-256 — `findex embed` and mode=semantic need it.
# Same thing on any OS:  uv run python -m findex.semantic download
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -m findex.semantic download models/all-MiniLM-L6-v2
