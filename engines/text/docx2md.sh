#!/usr/bin/env bash
# docx2md convenience wrapper — handles the docker mount boilerplate.
# Same image as pdf2md.sh (engines/text) -- docx2md.py is a sibling script
# in the same container, selected via --entrypoint override. No GPU, no
# model weights: python-docx is a pure structural reader.
#
#   ./docx2md.sh report.docx                  # markdown to stdout
#   ./docx2md.sh report.docx -o report.md     # markdown to file (beside the docx)
#
# The input file's directory is mounted as /work, so -o paths are relative to it.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 INPUT.docx [docx2md args...]" >&2
  exit 1
fi

IN="$1"; shift || true
if [ ! -f "$IN" ]; then echo "no such file: $IN" >&2; exit 1; fi

DIR="$(cd "$(dirname "$IN")" && pwd)"
BASE="$(basename "$IN")"

exec docker run --rm \
  --entrypoint python \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
  -v "$DIR":/work \
  pdf2md-text /usr/local/bin/docx2md.py "/work/$BASE" "$@"
