#!/usr/bin/env bash
# pdf2md convenience wrapper — handles the docker mount boilerplate.
#
#   ./pdf2md.sh report.pdf                        # markdown to stdout
#   ./pdf2md.sh report.pdf -o report.md           # markdown to file (beside the PDF)
#   ./pdf2md.sh report.pdf --format json          # structured JSON via Marker
#   ./pdf2md.sh report.pdf --engine marker --dpi 300
#   ./pdf2md.sh report.pdf --engine marker --use-llm   # LLM table refine via host Ollama
#
# The input PDF's directory is mounted as /work, so -o paths are relative to it.
# Model weights persist in ~/.cache/pdf2md-models (downloaded once).
#
# --add-host below maps host.docker.internal to the host gateway so the container
# can reach Ollama running on the host (needed for --use-llm).
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 INPUT.pdf [pdf2md args...]" >&2
  exit 1
fi

IN="$1"; shift || true
if [ ! -f "$IN" ]; then echo "no such file: $IN" >&2; exit 1; fi

DIR="$(cd "$(dirname "$IN")" && pwd)"
BASE="$(basename "$IN")"
MODELS="${PDF2MD_MODELS:-$HOME/.cache/pdf2md-models}"
mkdir -p "$MODELS"

exec docker run --rm --gpus all \
  --add-host=host.docker.internal:host-gateway \
  -v "$MODELS":/models \
  -v "$DIR":/work \
  pdf2md "/work/$BASE" "$@"