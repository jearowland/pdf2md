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
flock "$LOCK_FILE" docker run --rm --gpus all \
  --shm-size 32g --ipc=host \
  -v "$MODELS":/models \
  -v "$DIR":/work \
  pdf2md-mineru "/work/$BASE" "$@"
STATUS=$?
set -e
docker run --rm -v "$DIR":/work --entrypoint chown pdf2md-mineru \
  -R "$(id -u):$(id -g)" /work >/dev/null 2>&1 || true
exit "$STATUS"
