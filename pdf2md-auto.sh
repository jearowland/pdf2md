#!/usr/bin/env bash
# pdf2md-auto.sh — the reusable financial-document->Markdown entrypoint.
# Routes automatically, purely by input file extension:
#
#   .pdf, digital (real text layer)  -> pymupdf4llm         (engines/text, fast, CPU)
#   .pdf, scanned / image-only       -> MinerU hybrid-engine (engines/mineru, GPU OCR)
#   .docx                            -> docx2md.py           (engines/text, fast, CPU)
#   .xlsx                            -> xlsx2md.py            (engines/text, fast, CPU)
#   .doc / .xls (legacy binary)      -> LibreOffice -> PDF, then the normal PDF pipeline above
#
# docx/xlsx need no classify or derotate step -- they're already structured,
# text-native formats (no scanning, no page rotation concept), so those
# concerns simply don't apply. --engine is a PDF-only flag; passing it
# alongside a .docx/.xlsx/.doc/.xls input is a hard error rather than being
# silently ignored, since the combination is nonsensical.
#
# .doc/.xls are the pre-2007 OLE2/CFB binary formats -- no ZIP-based
# structure for python-docx/openpyxl to read at all (unlike .docx/.xlsx), so
# these convert to PDF first (LibreOffice headless, baked into the
# pdf2md-text image) and fall through into the ordinary PDF pipeline
# (derotate/classify/text-or-MinerU) from there. The converted PDF is kept
# as a visible sibling artifact (<stem>.fromdoc.pdf / <stem>.fromxls.pdf),
# same auditability precedent as <stem>.derotated.pdf below.
#
# MinerU is the scanned-PDF engine: on a real scanned financial-report fixture
# it correctly rendered nil cells as empty where Marker silently fabricated
# plausible numbers.
# Marker was tried and archived -- see archive/marker-engine/README.md for why.
#
#   ./pdf2md-auto.sh report.pdf                  # markdown to stdout
#   ./pdf2md-auto.sh report.pdf -o report.md     # markdown to file (beside the PDF)
#   ./pdf2md-auto.sh report.pdf --engine text    # force the fast CPU path
#   ./pdf2md-auto.sh report.pdf --engine mineru  # force MinerU
#   ./pdf2md-auto.sh report.docx -o report.md    # Word -> markdown
#   ./pdf2md-auto.sh report.xlsx -o report.md    # Excel -> markdown
#
# For PDFs: classification (digital vs scan) reuses the text engine's own
# PyMuPDF-based classify() logic via --classify-only — cheap, CPU-only, no
# model load — so there is one source of truth for the routing decision.
#
# classify() also catches a real defect: a font subsetted without a proper
# ToUnicode CMap looks "digital" by character COUNT (there's plenty of
# extracted text) but that text is undecodable garbage, not real content --
# confirmed on a real corpus, 69 of 1,719 "digital" documents (~4%). Any page
# with this problem forces the whole document to 'scan' -> mineru (which
# reads rendered pixels, not the broken embedded text), even if only a small
# fraction of pages are affected -- a garbage page produces actively WRONG
# text, not an honest gap, so it isn't blended into the ordinary image-only
# page ratio. See engines/text/pdf2md.py's CONTROL_CHAR_RE.
#
# Before routing a PDF, it's passed through --derotate: a page-level rotation
# check (Tesseract OSD, self-verified -- see engines/text/pdf2md.py) that fixes a
# real defect where MinerU's layout model silently drops the second of two stacked
# tables at a footnote seam specifically when the page is rotated. Only the PDF's
# /Rotate flag is ever changed -- no pixel or content is touched. The corrected
# copy is kept as a visible sibling artifact (<stem>.derotated.pdf), not a
# throwaway temp file, for auditability. Skip with --no-derotate.
#
# MinerU's engine (engines/mineru/mineru2md.py) also runs an automated spelling-
# reconciliation pass by default: a cheap reference pass on the 'pipeline' backend
# (no VLM stage) is used to deterministically correct rare-token spelling drift
# that hybrid-engine's VLM occasionally introduces (e.g. an unusual proper noun
# silently "corrected" toward a common word). Never a model decision -- pure
# cross-pass evidence. See engines/mineru/mineru2md.py's reconcile_spelling().
#
# Point TEXT_DIR / MINERU_DIR at wherever those engine folders live.
set -euo pipefail

# Mirror all output to a stable log path (in addition to normal stdout/stderr) so
# `tail -f ~/pdf2md/logs/pdf2md-auto.log` keeps working across runs/sessions
# without re-pointing a symlink each time. Override with PDF2MD_LOG_DIR.
LOG_DIR="${PDF2MD_LOG_DIR:-$HOME/pdf2md/logs}"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/pdf2md-auto.log") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') pdf2md-auto.sh $* ==="

if [ $# -lt 1 ]; then
  echo "usage: $0 INPUT.{pdf,docx,xlsx,doc,xls} [-o OUT.md] [--engine text|mineru] [--no-derotate] [engine-specific flags...]" >&2
  exit 1
fi
IN="$1"; shift || true
if [ ! -f "$IN" ]; then echo "no such file: $IN" >&2; exit 1; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEXT_DIR="${TEXT_DIR:-$SCRIPT_DIR/engines/text}"
MINERU_DIR="${MINERU_DIR:-$SCRIPT_DIR/engines/mineru}"
DIR="$(cd "$(dirname "$IN")" && pwd)"
BASE="$(basename "$IN")"
STEM="$(basename "${IN%.*}")"
EXT="${BASE##*.}"
EXT="$(echo "$EXT" | tr '[:upper:]' '[:lower:]')"

FORCE_ENGINE=""
SKIP_DEROTATE=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) FORCE_ENGINE="$2"; shift 2 ;;
    --no-derotate) SKIP_DEROTATE=1; shift ;;
    -o|--output)
      # -o gets forwarded into a container where only $DIR (mounted as /work) is
      # visible, so it must be relative to $DIR, not an absolute host path.
      OUT_ABS="$(realpath -m "$2")"
      case "$OUT_ABS" in
        "$DIR"/*) REL="${OUT_ABS#$DIR/}" ;;
        *) echo "[pdf2md-auto] ERROR: -o must resolve inside $DIR (only the input file's directory is mounted into the container): $2" >&2; exit 6 ;;
      esac
      ARGS+=("-o" "$REL")
      shift 2
      ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

# docx/xlsx: no classify, no derotate -- already structured, text-native
# formats, so neither concept applies. Dispatch straight to the matching
# script in engines/text and exit; everything below this block is PDF-only.
if [ "$EXT" = "docx" ] || [ "$EXT" = "xlsx" ]; then
  if [ -n "$FORCE_ENGINE" ]; then
    echo "[pdf2md-auto] ERROR: --engine is PDF-only and doesn't apply to .$EXT input" >&2
    exit 5
  fi
  echo "[pdf2md-auto] .$EXT input -> engine=$EXT" >&2
  exec "$TEXT_DIR/${EXT}2md.sh" "$IN" "${ARGS[@]}"
fi

# Legacy pre-2007 binary .doc/.xls: no ZIP-based structure to read directly
# (unlike .docx/.xlsx), so convert to PDF first (LibreOffice headless, baked
# into the pdf2md-text image) and let the rest of this script's ordinary PDF
# pipeline (derotate/classify/text-or-MinerU) handle it from there -- same
# "convert the format we can't read into one we can, then reuse everything"
# approach already proven ad hoc for a legacy .xlsx case earlier in this
# project, now built into the pipeline properly rather than a one-off.
if [ "$EXT" = "doc" ] || [ "$EXT" = "xls" ]; then
  if [ -n "$FORCE_ENGINE" ]; then
    echo "[pdf2md-auto] ERROR: --engine is PDF-only and doesn't apply to .$EXT input" >&2
    exit 5
  fi
  CONVERTED="$STEM.from${EXT}.pdf"
  echo "[pdf2md-auto] .$EXT input -> converting to PDF via LibreOffice first ($CONVERTED)..." >&2
  docker run --rm -v "$DIR":/work --entrypoint bash \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
    pdf2md-text -c \
    "mkdir -p /work/.libreoffice-tmp && soffice --headless --convert-to pdf --outdir /work/.libreoffice-tmp '/work/$BASE' && mv '/work/.libreoffice-tmp/$STEM.pdf' '/work/$CONVERTED' && rmdir /work/.libreoffice-tmp"
  BASE="$CONVERTED"
  STEM="$(basename "${BASE%.*}")"
  IN="$DIR/$BASE"
  EXT="pdf"
  echo "[pdf2md-auto] converted -> $IN, continuing through the normal PDF pipeline" >&2
fi

if [ "$EXT" != "pdf" ]; then
  echo "[pdf2md-auto] ERROR: unsupported input extension '.$EXT' (expected pdf, docx, xlsx, doc, or xls)" >&2
  exit 5
fi

if [ -z "$SKIP_DEROTATE" ]; then
  echo "[pdf2md-auto] checking page rotation..." >&2
  docker run --rm -v "$DIR":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
    pdf2md-text "/work/$BASE" --derotate "/work/$STEM.derotated.pdf"
  BASE="$STEM.derotated.pdf"
  IN="$DIR/$BASE"
  echo "[pdf2md-auto] using derotated copy: $IN" >&2
else
  echo "[pdf2md-auto] --no-derotate: skipping rotation check" >&2
fi

if [ -n "$FORCE_ENGINE" ]; then
  ENGINE="$FORCE_ENGINE"
  echo "[pdf2md-auto] forced engine: $ENGINE" >&2
else
  # Cheap classify: no GPU, no model load needed on this path.
  CLASS=$(docker run --rm -v "$DIR":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
    pdf2md-text "/work/$BASE" --classify-only)
  case "$CLASS" in
    digital) ENGINE="text" ;;
    scan)    ENGINE="mineru" ;;
    *) echo "[pdf2md-auto] ERROR: unexpected classify output: '$CLASS'" >&2; exit 3 ;;
  esac
  echo "[pdf2md-auto] auto-routed: $CLASS -> engine=$ENGINE" >&2
fi

case "$ENGINE" in
  text)
    exec "$TEXT_DIR/pdf2md.sh" "$IN" "${ARGS[@]}"
    ;;
  mineru)
    exec "$MINERU_DIR/mineru.sh" "$IN" "${ARGS[@]}"
    ;;
  marker)
    echo "[pdf2md-auto] ERROR: the Marker engine was archived (never won a single test vs" >&2
    echo "  MinerU -- see archive/marker-engine/README.md). To revive it manually:" >&2
    echo "  archive/marker-engine/pdf2md.sh $IN --engine marker ${ARGS[*]}" >&2
    exit 7
    ;;
  *)
    echo "[pdf2md-auto] ERROR: unknown engine '$ENGINE' (expected text|mineru)" >&2
    exit 2
    ;;
esac
