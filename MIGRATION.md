# Running pdf2md on GPU worker hosts

Written for whoever sets up the hosts. The shape this describes: one **CPU orchestrator host**
(no GPU) decides what gets converted and collects the results, and one or more **GPU worker
hosts** (Windows machines with a 24GB NVIDIA card, running pdf2md inside WSL2) do the
conversions, reached over a private network. pdf2md is what runs on the workers. A rented
serverless GPU endpoint is the burst option on top of that.

This repository contains the converter only. How the orchestrator ships a document to a
worker and collects the output (for example `rsync` plus `ssh`) is the caller's business
and is not in here.

## The two things that must not happen

**Two GPU jobs on one card at the same time.** MinerU's backend is built on vLLM, which
reserves GPU memory up front. A second GPU workload on the same card risks running out of
memory, and on WSL a GPU fault can take down the whole host, not just the job. See
[GPU memory contention](#gpu-memory-contention).

**Scripts from one commit and images from another.** The engine scripts are copied *into*
the container images at build time. The orchestration scripts (`pdf2md-auto.sh`,
`pdf2md_route.py`) run on the host from the checkout. After any `git checkout` or `git pull`,
rebuild the images, or the host side will call flags the containers do not have yet. The
typical symptom is `pdf2md_route.py` failing at `--classify-pages` or `--slice` against a
`pdf2md-text` image built before per-page routing existed.

## Which branch workers run

**`per-page-routing`.** It is `main` plus three things: per-page engine routing
(`pdf2md_route.py`, now the default PDF path in `pdf2md-auto.sh`), the Docling engine, and
MinerU's `--middle-json` flag. `main` is an ancestor of it, so nothing on `main` is missing
from it.

Every worker should run the **same commit**, with images built from that commit. Record it:

```
git -C ~/pdf2md rev-parse HEAD
```

Clone to `~/pdf2md`. The log directory defaults to `~/pdf2md/logs`; clone somewhere else only
if you also set `PDF2MD_LOG_DIR`.

## What a worker needs

### The GPU, reached from inside WSL

- **The driver is installed on the Windows side, never inside WSL.** Installing a Linux NVIDIA
  driver inside the distro breaks the passthrough. WSL exposes the Windows driver through
  `/usr/lib/wsl/lib`; `check-deps.sh` adds that directory to `PATH` if `nvidia-smi` is not
  found.
- **The driver must be new enough for CUDA 13.0**, because the MinerU base image
  (`engines/mineru/Dockerfile.mineru`) is `vllm/vllm-openai:v0.21.0`, a CUDA 13.0 build.
  `nvidia-smi` inside WSL should report a CUDA version of 13.0 or higher in its header. On an
  older driver, switch that Dockerfile to the `-cu129` base line it carries commented out.
- **No CUDA toolkit is needed on the host.** The containers bring their own CUDA runtime.
  Docling brings its own through its PyTorch wheel. A host `nvcc` is irrelevant either way.
- **Give WSL enough memory.** WSL2 caps its VM's memory by default. MinerU runs with
  `--shm-size 32g --ipc=host`, and vLLM leans on shared memory. Raise the cap in the Windows
  user's `.wslconfig` if conversions die without a clear error.
- **Stop Windows from sleeping.** A worker that sleeps drops off the network mid-job, and
  the orchestrator sees that as a hang rather than a failure.

### Docker, with GPU passthrough

`./check-deps.sh` installs and verifies git, Docker Engine, the NVIDIA Container Toolkit (only
when a GPU is detected), and `inotify-tools`. Re-running it is safe. Docker Desktop's WSL
integration also works instead of Docker Engine inside the distro. Either way, `docker` must
be on `PATH` *inside* the distro, and the account the orchestrator logs in as must be in the
`docker` group, because every engine runs through `docker run`.

### Host-side tools that pdf2md-auto.sh calls directly

Everything else runs inside the containers. The host itself needs only:

| Tool | Used by | Note |
|---|---|---|
| `bash` | `pdf2md-auto.sh`, the engine wrappers | |
| `python3` | `pdf2md_route.py` | Standard library only; no venv or packages |
| `flock` (util-linux) | `engines/mineru/mineru.sh` | The GPU lock; present on stock Ubuntu |
| `docker` | everything | See above |
| `inotifywait` | `watch.sh` only | Not needed for direct or orchestrated use |

Tesseract, PyMuPDF, pymupdf4llm and LibreOffice all live inside the `pdf2md-text` image. The
host needs none of them.

### The images

| Image | Built from | Size | GPU |
|---|---|---|---|
| `mineru:latest` | `engines/mineru/Dockerfile.mineru` | ~43GB, weights included | builds without one |
| `pdf2md-mineru` | `engines/mineru/Dockerfile` (on `mineru:latest`) | thin layer on top | needed at run time |
| `pdf2md-text` | `engines/text/Dockerfile` | small | never |

```
cd ~/pdf2md/engines/text   && docker build -t pdf2md-text .
cd ~/pdf2md/engines/mineru && docker build -t mineru:latest -f Dockerfile.mineru . \
                           && docker build -t pdf2md-mineru .
```

**Build `mineru:latest` once and copy it to the other workers. Do not rebuild it on each
one.** `Dockerfile.mineru` installs `mineru[core]>=3.4.0` and downloads "all" models at build
time. Both are unpinned, so two builds on different days can produce different engines and
different output. Build once, then move the image:

```
docker save mineru:latest | ssh OTHER_WORKER 'docker load'
```

A private registry works too, and doubles as the source for the serverless image below.
`pdf2md-text` and `pdf2md-mineru` are cheap, so rebuild those on each worker after every
checkout (see the second item under "must not happen").

The build needs disk headroom well beyond the final 43GB for intermediate layers. Check
`docker system df` before assuming a failed build was anything other than a full disk.

### Model weight caches

| What | Where | Size | Needed |
|---|---|---|---|
| MinerU models | **Inside the `mineru:latest` image** (`MINERU_MODEL_SOURCE=local`) | part of the ~43GB | Every worker, and they arrive with the image |
| MinerU host mount | `~/.cache/mineru-models` (override: `MINERU_MODELS`), mounted at `/models` by `mineru.sh` | stays empty in practice | Created automatically; nothing to copy |
| Docling layout and table models | `~/.cache/huggingface/hub/models--docling-project--docling-models` and `...--docling-layout-heron` (override the root with `HF_HOME`) | ~0.5GB together | Workers that run Docling |
| Docling's Python environment (PyTorch with CUDA and NVIDIA libraries) | wherever you create the venv | ~4-5GB | Workers that run Docling |
| Retired Marker engine | `~/.cache/pdf2md-models` | ~3GB | Nothing. Do not copy it |
| Text engine | none | none | |

`~/.cache/huggingface` is shared by every tool on the machine that uses Hugging Face, and on a
workstation it often holds tens of gigabytes of unrelated models. Only the two
`docling-project` directories belong to pdf2md. Copy just those, or let Docling download them
again on first use (it does so on its own, given network access).

### Docling

Docling is direct-call only. It is not containerized, not part of the routing in
`pdf2md-auto.sh`, and nothing in this repository installs it. The known-good set is Python
3.12, `docling==2.111.0`, `docling-core==2.86.0`, `docling-ibm-models==3.13.3` and
`torch==2.11.0` (a CUDA 13.0 build). Keep the venv outside the checkout:

```
python3 -m venv ~/.venvs/docling
~/.venvs/docling/bin/pip install 'docling==2.111.0' 'torch==2.11.0'
~/.venvs/docling/bin/python -c 'import torch; print(torch.cuda.is_available())'   # must print True
```

If that prints `False`, pip installed a CPU-only PyTorch. Docling will still run, just slowly,
and the worker is not doing its job. Docling may fetch further models (OCR, for example) the
first time it sees a scanned page, so warm it once with the smoke test below while the worker
has network access.

## What works on a CPU-only host

The `pdf2md-text` image needs no GPU, so the CPU orchestrator can do real work itself:

- **Digital PDFs whose pages all classify as text.** Per-page routing plans a single text run
  and never touches MinerU. `pdf2md-auto.sh doc.pdf -o doc.md` works unchanged.
- **`.docx` and `.xlsx`**, which are read directly by structural readers with no OCR, and
  **legacy `.doc`/`.xls`**, which go through LibreOffice to PDF and then follow the same rule.
- **Derotation, classification, slicing and `verify_numbers`.** All of these run in
  `pdf2md-text`.
- **Triage.** `--classify-pages` returns one JSON row per page with `class: text|ocr`. An
  orchestrator can run it locally, convert documents that contain no `ocr` pages itself, and
  send only the rest to a GPU worker. Most digital-first document sets are mostly text pages.

  ```
  docker run --rm -v "$PWD":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
    pdf2md-text /work/doc.pdf --classify-pages --quiet
  ```

What does **not** work there:

- **Any document with even one `ocr` page.** The router calls `mineru.sh`, `docker run --gpus
  all` fails, and the conversion exits non-zero. It fails loudly, which is the right way to
  fail. Send the document to a worker.
- **`--engine text` forced on a scanned document.** This does not error; it produces
  near-empty pages. Never use it to "get something" out of a scan on a CPU host.
- **Docling on CPU.** It runs, but slowly, and has not been measured. Do not plan volume on it.

A CPU host can *build* `mineru:latest` (the build downloads weights but runs no inference). That
is only worth doing if the orchestrator is also where images are stored and distributed from.

## The serverless worker

For burst capacity beyond the workers, `serverless/` wraps the whole pipeline in a single
image for RunPod Serverless. `serverless/README.md` is the authority. This section covers what
matters for the migration.

- **Image:** `serverless/Dockerfile`, built `FROM pdf2md-mineru`, from the repository root:
  `docker build -t pdf2md-serverless -f serverless/Dockerfile .`. It is about 43GB with the
  MinerU weights baked in, so every worker starts warm. Build it on a machine that already has
  `pdf2md-mineru` from the pinned commit, which means a GPU worker or wherever `mineru:latest`
  was built.
- **Registry:** Docker Hub or GHCR; RunPod pulls from either. For a private repository, add
  the registry credential in RunPod's settings and select it on the endpoint. The image itself
  contains no credentials. The first push is slow on residential upload bandwidth, and later
  pushes send only the changed layers.
- **Endpoint:** the settings in `serverless/README.md` §4:
  - several 16-24GB GPU types listed, for availability (MinerU's measured peak is under 9GB)
  - `workersMin: 0`, so it scales to zero
  - `idleTimeout: 60`
  - `containerDiskInGb` large enough for the image plus scratch space
  - no environment variables or secrets
  - spot ("flex") workers, because a job on a killed worker returns to the queue
- **It is not the same pipeline as the workers.** The handler does *whole-document* routing
  on both branches: no per-page routing, no `manifest.json`, no `verify_numbers`, and no
  `.doc`/`.xls`. Documents converted there need the caller's own completeness check.
- **Proof without an account:** run the handler's `--test` mode locally (see
  `serverless/README.md` §2). It exercises the exact code path RunPod invokes.

`serverless-llm/` is a separate, generic Ollama inference worker and has nothing to do with
conversion. If it is used, its endpoint needs a RunPod network volume attached (the image sets
`OLLAMA_MODELS=/runpod-volume/ollama`). The model is pulled onto the volume by the first job,
and every worker after that finds it there. A network volume is tied to one datacenter, which
limits the GPU types available to that endpoint.

## GPU memory contention

**Rule: one GPU job per card at a time.**

What enforces it today: `engines/mineru/mineru.sh` wraps its `docker run --gpus all` in
`flock` on `/tmp/pdf2md-mineru.lock` (override: `PDF2MD_MINERU_LOCK`). Every MinerU
invocation goes through it, whether from `pdf2md-auto.sh`, `pdf2md_route.py`, `watch.sh` or a
direct call, so those queue behind each other. The lock is local to each host, which is
correct, since each card only has to coordinate with itself. CPU text-engine work is not
locked and runs in parallel freely.

What it does **not** cover:

- **Docling.** It is a direct Python call and takes no lock. Wrap it in the same one:
  `flock "${PDF2MD_MINERU_LOCK:-/tmp/pdf2md-mineru.lock}" ~/.venvs/docling/bin/python ...`.
- **The serverless image run locally** with `--test --gpus all`. Wrap it the same way.
- **Anything else on the card**, such as a local LLM server or another tool's inference. A
  lock cannot help against a process that holds its weights resident. Stop it, or check
  headroom first:
  `nvidia-smi --query-gpu=memory.used,memory.total --format=csv`.

For the orchestrator: send **at most one GPU job per worker at a time**, and let CPU-only
jobs fill the gaps. Queuing on the lock is safe, but it hides the queue from the orchestrator
and makes its timeouts count time spent waiting, not converting. The lock is a backstop, not a
scheduler.

## Proving a worker is ready

Run these in order on each worker. Every one must pass before the orchestrator sends real
work to it.

**1. Checkout and GPU.**

```
git -C ~/pdf2md rev-parse --abbrev-ref HEAD      # per-page-routing
git -C ~/pdf2md rev-parse HEAD                   # same commit on every worker
nvidia-smi                                        # the card, CUDA version >= 13.0
docker run --rm --gpus all nvidia/cuda:12.5.0-base-ubuntu22.04 nvidia-smi   # same card, from a container
docker image ls | grep -E 'pdf2md-(text|mineru)'  # both present, built after the checkout
```

**2. Make a smoke document.** This builds a two-page PDF: page 1 has a real text layer, and
page 2 is the same kind of content rendered to an image with no text at all. It needs no test
document and exercises per-page routing, both engines, slicing, merging and `verify_numbers`
in a single run. Each column of numbers adds up to its total, so a misread digit is easy to
spot.

```
mkdir -p ~/pdf2md-smoke && cd ~/pdf2md-smoke
docker run --rm -i -v "$PWD":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --entrypoint python3 pdf2md-text - <<'EOF'
import fitz
def page(doc, title, rows):
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 90), title, fontsize=16)
    for i, (k, v) in enumerate(rows):
        y = 130 + 22 * i
        p.insert_text((72, y), k, fontsize=12)
        p.insert_text((380, y), v, fontsize=12)
out = fitz.open()
page(out, "Smoke test: digital page", [("Alpha", "1,234,567"), ("Beta", "987,654"), ("Total", "2,222,221")])
src = fitz.open()
page(src, "Smoke test: scanned page", [("Gamma", "3,456,789"), ("Delta", "1,112,131"), ("Total", "4,568,920")])
out.new_page(width=595, height=842).insert_image(
    fitz.Rect(0, 0, 595, 842), pixmap=src[0].get_pixmap(dpi=200))
out.save("smoke.pdf")
EOF
```

**3. Convert it.**

```
~/pdf2md/pdf2md-auto.sh smoke.pdf -o smoke.md; echo "exit $?"
```

A pass shows all of the following:

- `[route] 2 pages -> 2 run(s): p1-1:text, p2-2:mineru`. This proves the page classification
  and the split. If both pages went to one engine, routing is not working.
- `[mineru.sh] waiting for GPU lock` followed by MinerU running. The first run is slow while
  vLLM loads, and later runs are quicker.
- `[verify] text-layer number coverage: 100% (3/3)`. Only page 1 has a text layer, so three
  numbers is the whole reference set.
- `[route] wrote ... (0 warning(s))` and `exit 0`.
- The OCR page's numbers present and correct:

  ```
  for n in 3,456,789 1,112,131 4,568,920; do grep -q "$n" smoke.md && echo "ok $n" || echo "MISSING $n"; done
  ```

  All three lines must say `ok`.
- `smoke.manifest.json` beside the output, with page 1 `"engine": "text"`, page 2
  `"engine": "mineru"`, and `"warnings": []`.

**4. Prove the verifier fires.** A check that always says 100% proves nothing, so delete a
number and confirm it gets reported:

```
sed 's/987,654/REMOVED/' smoke.md > smoke.cut.md
docker run --rm -v "$PWD":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --entrypoint python3 pdf2md-text /usr/local/bin/verify_numbers.py /work/smoke.pdf /work/smoke.cut.md
```

A pass is `[verify] text-layer number coverage: 67% (2/3) -- 1 missing, on page(s) 1`,
followed by `page 1: 987,654` on stderr. `verify_numbers` only reports and always exits 0, so
the orchestrator has to read that line itself. On real documents, anything below 100% on a
document with a text layer means the conversion dropped content, and the page list says
where. Scanned documents report `no text layer numbers to check against`. That result means
the check could not run, not that the document passed.

**5. Docling, on workers that run it.**

```
flock "${PDF2MD_MINERU_LOCK:-/tmp/pdf2md-mineru.lock}" \
  ~/.venvs/docling/bin/python ~/pdf2md/engines/docling/docling2md.py smoke.pdf -o smoke.docling.md
docker run --rm -v "$PWD":/work --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --entrypoint python3 pdf2md-text /usr/local/bin/verify_numbers.py /work/smoke.pdf /work/smoke.docling.md
```

A pass is `100% (3/3)`. This test says nothing about Docling's OCR of page 2; judge that
separately if it matters.

**6. From the orchestrator.** Repeat step 3 through a non-interactive login, the way real jobs
will arrive:

```
ssh WORKER 'cd ~/pdf2md-smoke && ~/pdf2md/pdf2md-auto.sh smoke.pdf -o smoke.md' ; echo "exit $?"
```

This catches the problems an interactive shell hides: `docker` group membership, `PATH`, and a
worker that has gone to sleep. The output files the orchestrator should collect are
`<stem>.md`, `<stem>.manifest.json`, and `images/` when MinerU kept any. The router deletes each
piece's MinerU `content_list.json` provenance file unless `--keep-parts` is passed. `-o` must
point beside the input, because only the input's directory is mounted into the containers.

`rm -rf ~/pdf2md-smoke` when done.
