#!/usr/bin/env bash
# pdf2md-mineru wrapper — MinerU PDF->markdown, GPU.
#
#   ./mineru.sh report.pdf                 # markdown to stdout
#   ./mineru.sh report.pdf -o report.md    # markdown to file (beside the PDF)
#   ./mineru.sh report.pdf --backend vlm-engine
#
# --shm-size / --ipc=host are needed by MinerU's vllm-based backends.
#
# flock-serialized against every OTHER MinerU invocation on this host,
# regardless of which caller started it (a batch pipeline, watch.sh's
# folder watcher, a manual run, anything) -- there's exactly one physical
# GPU, and confirmed live: an unrelated caller's overnight batch job and a
# watch.sh smoke test both landed on the GPU at once (two concurrent
# `docker run --gpus all` MinerU containers), a real risk of VRAM
# exhaustion or severe slowdown from contention, not just a hypothetical.
# This only serializes the GPU-touching MinerU path -- the CPU-only
# text-engine/docx/xlsx path (the majority of documents) is untouched and
# still runs freely in parallel. Lock is host-local (/tmp): each machine's
# own GPU only needs to coordinate with itself, not across machines.
LOCK_FILE="${PDF2MD_MINERU_LOCK:-/tmp/pdf2md-mineru.lock}"
set -euo pipefail

if [ $# -lt 1 ]; then echo "usage: $0 INPUT.pdf [args...]" >&2; exit 1; fi
IN="$1"; shift || true
if [ ! -f "$IN" ]; then echo "no such file: $IN" >&2; exit 1; fi

DIR="$(cd "$(dirname "$IN")" && pwd)"
BASE="$(basename "$IN")"
MODELS="${MINERU_MODELS:-$HOME/.cache/mineru-models}"
mkdir -p "$MODELS"

# A job's MinerU server (tools/mineru-session) is running: hand this document to
# it instead of starting a container, so the models don't load again. Needs -o
# (the server writes files); without it, fall through to a one-shot container.
# PDF2MD_MINERU_SERVER=off (no such queue dir) also means one-shot.
if [ -n "${PDF2MD_MINERU_SERVER:-}" ] && [ -d "$PDF2MD_MINERU_SERVER/requests" ]; then
  Q="$PDF2MD_MINERU_SERVER"; OUT=""; REST=(); prev=""
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then OUT="$a"; prev=""; continue; fi
    if [ "$a" = "-o" ]; then prev="-o"; continue; fi
    REST+=("$a")
  done
  if [ -n "$OUT" ] && [ ! -f "$Q/exited" ]; then
    OUT="$(basename "$OUT")"
    ID="$(date +%s%N)-$$"
    mkdir -p "$Q/$ID"
    cp "$IN" "$Q/$ID/$BASE"
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
      "/q/$ID/$BASE" -o "/q/$ID/$OUT" ${REST[@]+"${REST[@]}"} >"$Q/requests/$ID.json.tmp"
    mv "$Q/requests/$ID.json.tmp" "$Q/requests/$ID.json"
    echo "[mineru.sh] queued on the job's MinerU server ($Q)" >&2
    while [ ! -f "$Q/$ID/status.json" ]; do
      if [ -f "$Q/exited" ]; then
        echo "[mineru.sh] ERROR: the job's MinerU server stopped; see $Q/server.log" >&2
        tail -20 "$Q/server.log" >&2 || true
        exit 4
      fi
      sleep 1
    done
    STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["rc"])' "$Q/$ID/status.json")"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(f"[mineru.sh] server: queued {d.get(\"queued\")}s, model load {d.get(\"api_start\")}s, total {d.get(\"secs\")}s", file=sys.stderr)' "$Q/$ID/status.json" || true
    STEM_OUT="${OUT%.md}"
    for f in "$OUT" "$STEM_OUT.content_list.json" "$STEM_OUT.middle.json"; do
      [ -f "$Q/$ID/$f" ] && cp "$Q/$ID/$f" "$DIR/$f"
    done
    if [ -d "$Q/$ID/images/$STEM_OUT" ]; then
      mkdir -p "$DIR/images/$STEM_OUT" && cp -r "$Q/$ID/images/$STEM_OUT/." "$DIR/images/$STEM_OUT/"
    fi
    rm -rf "$Q/$ID"
    exit "$STATUS"
  fi
fi

echo "[mineru.sh] waiting for GPU lock ($LOCK_FILE)..." >&2
# NOT running as --user here (unlike the other engines) -- confirmed a real,
# live failure when tried: MinerU's model-config lookup
# (auto_download_and_get_model_root_path) reads a config file baked into
# the image at BUILD time under root's actual home directory; forcing
# HOME=/tmp at runtime made that lookup return None ('NoneType' object has
# no attribute 'get'), breaking every MinerU conversion. Runs as root, same
# as always, then a quick separate root-owned chown fixes ownership on
# whatever it wrote (root can chown to anyone; a non-root --user process
# can't) -- sidesteps the home-directory assumption entirely instead of
# fighting it. Not `exec`, since a following command is needed; exit status
# is preserved manually so a real conversion failure still propagates.
set +e
# Hardened (security review F-03, 2026-09-27): this container reads documents
# uploaded from outside, so it gets no network (the models are baked into the
# image), no host IPC (a private shared-memory segment instead), no root
# (the calling user, as on rootful workers; HOME=/tmp for caches), no
# capabilities or privilege escalation, and pids/memory limits. Only this
# job's own directory is mounted. --gpus all stays.
flock "$LOCK_FILE" docker run --rm --gpus all \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
  --network none --shm-size 32g \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 4096 --memory 20g \
  -v "$MODELS":/models \
  -v "$DIR":/work \
  pdf2md-mineru "/work/$BASE" "$@"
STATUS=$?
set -e
exit "$STATUS"
