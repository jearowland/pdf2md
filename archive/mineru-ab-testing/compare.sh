#!/usr/bin/env bash
# compare.sh — run ONE pdf through multiple engines, one output file each,
# for side-by-side comparison on your own document.
#
#   ./compare.sh report2.pdf
# produces (beside the PDF):
#   report2.marker.md   (Marker,  via the pdf2md container + your chosen flags)
#   report2.mineru.md   (MinerU,  hybrid-engine, -m ocr)
#
# Point MARKER_DIR / MINERU_DIR at wherever those tool folders live.
set -euo pipefail

# Mirror all output to a stable log path (in addition to normal stdout/stderr) so
# `tail -f ~/pdf2md-mineru/logs/compare.log` keeps working across runs/sessions
# without re-pointing a symlink each time. Override with PDF2MD_LOG_DIR.
LOG_DIR="${PDF2MD_LOG_DIR:-$HOME/pdf2md-mineru/logs}"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/compare.log") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') compare.sh $* ==="

if [ $# -lt 1 ]; then echo "usage: $0 INPUT.pdf" >&2; exit 1; fi
IN="$1"
if [ ! -f "$IN" ]; then echo "no such file: $IN" >&2; exit 1; fi

DIR="$(cd "$(dirname "$IN")" && pwd)"
STEM="$(basename "${IN%.*}")"

MARKER_DIR="${MARKER_DIR:-$HOME/pdf2md}"
MINERU_DIR="${MINERU_DIR:-$HOME/pdf2md-mineru}"

# Marker flags: the fidelity set we landed on (plain markdown, not HTML — HTML
# regressed the dense tables). Override with MARKER_FLAGS if you want to A/B.
MARKER_FLAGS="${MARKER_FLAGS:---engine marker --dpi 300}"

echo "=== [1/2] Marker -> $STEM.marker.md ==="
( cd "$MARKER_DIR" && ./pdf2md.sh "$DIR/$(basename "$IN")" $MARKER_FLAGS -o "$STEM.marker.md" )

echo "=== [2/2] MinerU -> $STEM.mineru.md ==="
( cd "$MINERU_DIR" && ./mineru.sh "$DIR/$(basename "$IN")" -o "$STEM.mineru.md" )

echo
echo "=== done ==="
echo "  $DIR/$STEM.marker.md"
echo "  $DIR/$STEM.mineru.md"
echo "Diff the two financial tables against ground truth to see which handles nil cells."
