#!/usr/bin/env bash
# watch.sh — drop-and-forget folder watcher for pdf2md-auto.sh.
#
# Watches a directory for new pdf/docx/xlsx/doc/xls files and converts each
# one automatically as it lands, writing the markdown (and content_list.json
# etc.) beside the source file -- the same output convention pdf2md-auto.sh
# already uses everywhere else, so "drop a file, pick up the .md next to it"
# needs no new mental model.
#
# Uses inotifywait (inotify-tools) for event-driven detection, not polling --
# near-instant reaction, no wasted CPU checking an empty folder. IMPORTANT
# WSL2 caveat: inotify only fires reliably on WSL2's own Linux filesystem,
# NOT on a Windows-mounted drive (/mnt/c/..., DrvFs) -- so WATCH_DIR should
# live under the WSL2 home filesystem and be accessed from Windows Explorer
# via \\wsl$\<distro>\... rather than being a normal Windows folder.
#
# Reacts to close_write, not create: a file mid-copy triggers create() well
# before its bytes are fully written, and converting a half-written file
# would silently produce garbage or a confusing error. close_write only
# fires once the writer's file handle is closed, i.e. the copy is done.
#
# Source files are never deleted -- on success the original moves to
# processed/, on failure to failed/ (so a broken document doesn't get
# reprocessed in a loop, and failures are visually obvious: they're just
# sitting there in their own folder). Only the top-level WATCH_DIR is
# watched (no -r), so files already moved into processed/failed/ are never
# picked up again.
#
# Usage
# -----
#   ./watch.sh [WATCH_DIR]              # default: ~/pdf2md/inbox
#   WATCH_DIR=/some/path ./watch.sh
#
# Leave it running (tmux/screen/systemd --user, your choice) -- Ctrl-C to stop.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH_DIR="${1:-${WATCH_DIR:-$HOME/pdf2md/inbox}}"
PROCESSED_DIR="$WATCH_DIR/processed"
FAILED_DIR="$WATCH_DIR/failed"
LOG_DIR="${PDF2MD_LOG_DIR:-$HOME/pdf2md/logs}"
LOG_FILE="$LOG_DIR/watch.log"

if ! command -v inotifywait >/dev/null 2>&1; then
  echo "[watch] ERROR: inotifywait not found -- install with: sudo apt-get install -y inotify-tools" >&2
  exit 1
fi

mkdir -p "$WATCH_DIR" "$PROCESSED_DIR" "$FAILED_DIR" "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') watch.sh starting -- watching $WATCH_DIR ==="

is_supported() {
  local lower="${1,,}"
  # Never re-watch pdf2md-auto.sh's own intermediate artifacts -- confirmed
  # a real bug: its derotated copy (<stem>.derotated.pdf) is written INTO
  # this same folder (same convention as everywhere else in pdf2md, output
  # beside input) and, being a .pdf, would otherwise trigger close_write and
  # get picked up as if it were a brand-new source file -- converting a
  # conversion byproduct, then ITS derotated copy, cascading indefinitely.
  case "$lower" in
    *.derotated.pdf|*.fromdoc.pdf|*.fromxls.pdf) return 1 ;;
  esac
  case "$lower" in
    *.pdf|*.docx|*.xlsx|*.doc|*.xls) return 0 ;;
    *) return 1 ;;
  esac
}

convert_one() {
  local f="$1"
  local base stem md
  base="$(basename "$f")"
  stem="${base%.*}"
  md="$WATCH_DIR/$stem.md"

  echo "[watch] converting: $base"
  if "$SCRIPT_DIR/pdf2md-auto.sh" "$f" -o "$md"; then
    mv "$f" "$PROCESSED_DIR/$base"
    echo "[watch] done: $base -> $stem.md (source moved to processed/)"
  else
    mv "$f" "$FAILED_DIR/$base"
    echo "[watch] FAILED: $base (source moved to failed/, see pdf2md-auto.log for details)"
  fi
}

# Sweep anything already sitting in the folder before the watcher started --
# "drop and forget" shouldn't depend on drop-then-immediately-start-watching
# ordering.
shopt -s nullglob
for f in "$WATCH_DIR"/*; do
  [ -f "$f" ] || continue
  is_supported "$f" || continue
  convert_one "$f"
done
shopt -u nullglob

echo "[watch] initial sweep complete -- now watching for new files"

inotifywait -m -e close_write --format '%f' "$WATCH_DIR" | while read -r name; do
  f="$WATCH_DIR/$name"
  [ -f "$f" ] || continue
  is_supported "$f" || continue
  convert_one "$f"
done
