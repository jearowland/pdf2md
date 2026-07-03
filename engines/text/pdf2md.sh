#!/usr/bin/env bash
# pdf2md convenience wrapper — handles the docker mount boilerplate.
# CPU-only, no GPU, no model weights to cache (pymupdf4llm + tesseract OSD).
#
#   ./pdf2md.sh report.pdf                  # markdown to stdout
#   ./pdf2md.sh report.pdf -o report.md     # markdown to file (beside the PDF)
#   ./pdf2md.sh report.pdf --classify-only  # print 'digital' or 'scan', exit
#   ./pdf2md.sh report.pdf --derotate out.pdf
#
# The input PDF's directory is mounted as /work, so -o paths are relative to it.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 INPUT.pdf [pdf2md args...]" >&2
  exit 1
fi

IN="$1"; shift || true
if [ ! -f "$IN" ]; then echo "no such file: $IN" >&2; exit 1; fi

DIR="$(cd "$(dirname "$IN")" && pwd)"
BASE="$(basename "$IN")"

exec docker run --rm \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
  -v "$DIR":/work \
  pdf2md-text "/work/$BASE" "$@"
