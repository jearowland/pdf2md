#!/usr/bin/env bash
# check-deps.sh — verify and install everything pdf2md needs on a fresh
# Ubuntu/Debian host (including WSL2). Safe to re-run: every step checks
# first and skips if already satisfied, so this is the same script whether
# you're setting up a brand-new machine or just double-checking an existing
# one after something changed.
#
# Installs, in order:
#   1. git            -- to clone the repo
#   2. Docker Engine   -- runs every pdf2md engine in a container
#   3. NVIDIA Container Toolkit -- ONLY if an NVIDIA GPU is detected; lets
#      `docker run --gpus all` reach it. The GPU driver itself is NOT
#      installed here -- on WSL2 that's a Windows-side driver, out of
#      scope for a script running inside the Linux distro.
#   4. inotify-tools  -- for watch.sh's folder watcher (not needed for the
#      core converter, only the drop-and-forget workflow)
#
# Usage: ./check-deps.sh
# Needs sudo for apt/docker install steps -- will prompt interactively.
set -euo pipefail

log() { echo "[check-deps] $*"; }
ok()  { echo "[check-deps]   ✓ $*"; }

# ---------------------------------------------------------------------------
log "1/4 git"
if command -v git >/dev/null 2>&1; then
  ok "already installed ($(git --version))"
else
  log "installing git..."
  sudo apt-get update -qq
  sudo apt-get install -y git
  ok "installed ($(git --version))"
fi

# ---------------------------------------------------------------------------
log "2/4 Docker Engine"
if command -v docker >/dev/null 2>&1; then
  ok "already installed ($(docker --version))"
else
  log "installing Docker Engine (official convenience script)..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  ok "installed"
  echo
  echo "  NOTE: you were just added to the 'docker' group. That only takes"
  echo "  effect in a NEW shell session -- run 'newgrp docker' now, or"
  echo "  close and reopen your terminal, before using docker without sudo."
  echo
fi

# ---------------------------------------------------------------------------
log "3/4 NVIDIA Container Toolkit (GPU support for the MinerU engine)"
GPU_PRESENT=0
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_PRESENT=1
elif [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
  # WSL2: the GPU shim isn't always on $PATH by default even when GPU
  # passthrough from the Windows-side driver is working correctly.
  GPU_PRESENT=1
  if ! echo "$PATH" | grep -q '/usr/lib/wsl/lib'; then
    log "found /usr/lib/wsl/lib/nvidia-smi but it's not on \$PATH -- adding it"
    echo 'export PATH="$PATH:/usr/lib/wsl/lib"' >> "$HOME/.bashrc"
    export PATH="$PATH:/usr/lib/wsl/lib"
  fi
fi

if [ "$GPU_PRESENT" -eq 0 ]; then
  log "no NVIDIA GPU detected -- skipping. The MinerU (scanned-PDF/OCR) engine"
  log "needs a GPU; if you never plan to run it on this machine, that's fine."
else
  ok "GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo '(name unavailable)')"
  log "checking if Docker can already reach it..."
  if docker run --rm --gpus all nvidia/cuda:12.5.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
    ok "docker run --gpus all already works, nothing to install"
  else
    log "installing NVIDIA Container Toolkit..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y nvidia-container-toolkit
    sudo nvidia-ctk runtime configure --runtime=docker
    if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
      sudo systemctl restart docker
    else
      # WSL2 without systemd enabled: no service manager, restart the daemon directly
      sudo service docker restart 2>/dev/null || log "couldn't auto-restart docker -- restart it manually, then re-run this script to verify"
    fi
    log "verifying..."
    if docker run --rm --gpus all nvidia/cuda:12.5.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
      ok "docker run --gpus all now works"
    else
      echo "[check-deps]   ✗ still not working -- run the smoke test yourself to see the real error:" >&2
      echo "      docker run --rm --gpus all nvidia/cuda:12.5.0-base-ubuntu22.04 nvidia-smi" >&2
      exit 1
    fi
  fi
fi

# ---------------------------------------------------------------------------
log "4/4 inotify-tools (for watch.sh's folder watcher)"
if command -v inotifywait >/dev/null 2>&1; then
  ok "already installed"
else
  log "installing inotify-tools..."
  sudo apt-get update -qq
  sudo apt-get install -y inotify-tools
  ok "installed"
fi

echo
log "All dependencies satisfied. Next: build the images (see README.md's Build section)."
