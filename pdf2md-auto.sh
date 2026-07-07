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
#                                                # (+ report.manifest.json sibling)
#   ./pdf2md-auto.sh report.pdf --route whole-doc  # pre-2026-07 whole-document routing
#   ./pdf2md-auto.sh report.pdf --engine text    # force the fast CPU path (whole doc)
#   ./pdf2md-auto.sh report.pdf --engine mineru  # force MinerU (whole doc)
#
# DEFAULT PDF path (2026-07): per-page engine routing (pdf2md_route.py) --
# each page classified text|ocr and each contiguous same-class run converted
# by the right engine, merged with global page numbers, plus a per-page
# fact manifest. Mixed documents (digital filings with scanned signature
# pages; image-dense DTP documents with perfect text layers) are exactly
# what whole-document routing got wrong in both directions.
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
ROUTE="page"   # page (default): per-page engine routing via pdf2md_route.py;
               # whole-doc: the pre-2026-07 whole-document classify-and-route
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) FORCE_ENGINE="$2"; shift 2 ;;
    --route) ROUTE="$2"; shift 2 ;;
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

# Per-page routing is the DEFAULT for PDFs (2026-07): the unit of conversion
# failure is the page -- mixed documents (a digital filing with a few scanned
# pages; an image-dense DTP document with a perfect text layer) defeat any
# whole-document choice. --route whole-doc restores the previous behaviour;
# --engine forces every page through one engine (whole-document by
# construction). The router derotates by itself, so the derotated copy made
# above is handed over with --no-derotate to avoid doing the work twice.
if [ -z "$FORCE_ENGINE" ] && [ "$ROUTE" = "page" ]; then
  # pull -o back out of ARGS: the router needs the host path (it writes a
  # <out>.manifest.json sibling); with no -o (stdout mode) use a temp
  # sibling and drop the manifest -- stdout callers have nowhere to put it.
  ROUTE_OUT=""
  PASS=()
  prev=""
  for a in "${ARGS[@]+"${ARGS[@]}"}"; do
    if [ "$prev" = "-o" ]; then ROUTE_OUT="$DIR/$a"; prev=""; continue; fi
    if [ "$a" = "-o" ]; then prev="-o"; continue; fi
    PASS+=("$a")
  done
  if [ -n "$ROUTE_OUT" ]; then
    exec python3 "$SCRIPT_DIR/pdf2md_route.py" "$IN" -o "$ROUTE_OUT" \
      --no-derotate ${PASS[@]+"${PASS[@]}"}
  else
    TMP_MD="$DIR/$STEM.routed.tmp.md"
    python3 "$SCRIPT_DIR/pdf2md_route.py" "$IN" -o "$TMP_MD" \
      --no-derotate ${PASS[@]+"${PASS[@]}"}
    cat "$TMP_MD"
    rm -f "$TMP_MD" "${TMP_MD%.md}.manifest.json"
    exit 0
  fi
elif [ -n "$FORCE_ENGINE" ]; then
  ENGINE="$FORCE_ENGINE"
  echo "[pdf2md-auto] forced engine: $ENGINE (whole-document)" >&2
elif [ "$ROUTE" = "whole-doc" ]; then
  # Cheap classify: no GPU, no model load needed on this path.
  CLASS=$(docker run --rm -v "$DIR":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
    pdf2md-text "/work/$BASE" --classify-only)
  case "$CLASS" in
    digital) ENGINE="text" ;;
    scan)    ENGINE="mineru" ;;
    *) echo "[pdf2md-auto] ERROR: unexpected classify output: '$CLASS'" >&2; exit 3 ;;
  esac
  echo "[pdf2md-auto] auto-routed (whole-doc): $CLASS -> engine=$ENGINE" >&2
else
  echo "[pdf2md-auto] ERROR: unknown --route '$ROUTE' (expected page|whole-doc)" >&2
  exit 2
fi

# Post-conversion verification (report-only): when the caller asked for a
# file output (-o), check that the markdown kept the money-like numbers the
# PDF's text layer contains -- layout engines can silently drop blocks
# (small appendix-style tables especially), and this makes that visible at
# conversion time. Never changes the exit code; scanned PDFs (no text
# layer) report as unverifiable, not as failures.
OUT_MD=""
prev=""
for a in "${ARGS[@]}"; do
  if [ "$prev" = "-o" ] || [ "$prev" = "--output" ]; then OUT_MD="$a"; fi
  prev="$a"
done

run_and_verify() {
  "$@"
  local rc=$?
  # the -o handling above rewrites the path relative to the input's dir
  case "$OUT_MD" in /*) : ;; ?*) OUT_MD="$DIR/$OUT_MD" ;; esac
  if [ $rc -eq 0 ] && [ -n "$OUT_MD" ] && [ -f "$OUT_MD" ]; then
    local odir; odir=$(cd "$(dirname "$OUT_MD")" && pwd)
    docker run --rm -v "$DIR":/work -v "$odir":/out --user "$(id -u):$(id -g)"       -e HOME=/tmp -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro       --entrypoint python3 pdf2md-text /usr/local/bin/verify_numbers.py       "/work/$BASE" "/out/$(basename "$OUT_MD")" || true
  fi
  return $rc
}

case "$ENGINE" in
  text)
    run_and_verify "$TEXT_DIR/pdf2md.sh" "$IN" "${ARGS[@]}"
    exit $?
    ;;
  mineru)
    run_and_verify "$MINERU_DIR/mineru.sh" "$IN" "${ARGS[@]}"
    exit $?
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
