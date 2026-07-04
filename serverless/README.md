# pdf2md on RunPod Serverless

Burst-convert a large document corpus on rented serverless GPU. Workers
pull jobs from RunPod's managed queue and return Markdown; the submitting
machine only ever makes outbound calls (submit, poll). The worker image is
pdf2md and nothing else — bytes in, Markdown out, same contract as the CLI.

## 1. Build the image

From the repo root (context must span both engine dirs):

```bash
docker build -t pdf2md-serverless -f serverless/Dockerfile .
```

One image carries the full routing pipeline (derotate → classify →
text-or-MinerU) because serverless workers can't orchestrate per-engine
containers the way `pdf2md-auto.sh` does. MinerU model weights are baked in
via the base image — every worker starts warm.

## 2. Smoke-test locally (no account needed)

```bash
# CPU path (digital PDF):
docker run --rm -v /path/to/docs:/work --entrypoint python3 pdf2md-serverless \
  /usr/local/bin/handler.py --test /work/report.pdf

# GPU path (scanned PDF):
docker run --rm --gpus all --shm-size 32g --ipc=host \
  -v /path/to/docs:/work --entrypoint python3 pdf2md-serverless \
  /usr/local/bin/handler.py --test /work/scanned.pdf
```

`--test` exercises the exact code path RunPod invokes, minus the SDK.

## 3. Push to a registry

The image is ~43GB (MinerU weights). Docker Hub or GHCR both work — RunPod
pulls from either. Expect the initial push to take a while on residential
upload bandwidth; layers are cached, so rebuilds push only what changed.

```bash
docker tag pdf2md-serverless YOUR_REGISTRY_USER/pdf2md-serverless:latest
docker login   # once
docker push YOUR_REGISTRY_USER/pdf2md-serverless:latest
```

## 4. Create the endpoint

Via the RunPod console (Serverless → New Endpoint), or the REST API:

```bash
export RUNPOD_API_KEY=...   # runpod.io → Settings → API Keys

curl -s -X POST https://rest.runpod.io/v1/endpoints \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "pdf2md",
    "templateId": null,
    "imageName": "YOUR_REGISTRY_USER/pdf2md-serverless:latest",
    "gpuTypeIds": ["NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"],
    "workersMin": 0,
    "workersMax": 20,
    "idleTimeout": 60,
    "containerDiskInGb": 60,
    "env": {}
  }'
```

Notes on the choices:
- **GPU types**: MinerU's hybrid backend peaks well under 12GB VRAM
  (measured 8.78GB on a 60-page scanned document), so 24GB cards are
  comfortable and 16GB workable — list several types for spot availability,
  cheapest first.
- **workersMin: 0** — scale to zero, pay nothing idle.
- **containerDiskInGb**: must fit the image plus scratch space.
- **The endpoint needs no env/secrets** — the image contains no credentials
  and the job payload carries everything a conversion needs.

## 5. Job contract

Submit (`POST https://api.runpod.ai/v2/{ENDPOINT_ID}/run`):

```json
{"input": {"filename": "report.pdf",
            "url": "https://public.example/report.pdf"}}
```

or `"content_base64"` instead of `"url"` (mind RunPod's ~10MB payload cap;
base64 inflates by 4/3). Optional: `"engine": "text"|"mineru"` to force
routing, `"no_derotate": true`, `"no_reconcile_spelling": true` (halves GPU
time per scanned document at the cost of the rare-proper-noun protection).

Result (`GET .../status/{job_id}` → `output`):

```json
{"markdown": "...", "engine": "mineru", "classify": "scan",
 "images_base64": {"<hash>.jpg": "..."}, "content_list_json": "...",
 "log": "..."}
```

Errors come back as `{"error": "...", "log": "..."}` — the job still
COMPLETEs; check for the `error` key.

## 6. Cost/perf shape

Blended ~70s/document on a mixed digital/scanned corpus (scanned dominates:
~95-170s through MinerU incl. the spelling-reconciliation reference pass;
digital ~10-15s on CPU). Twenty concurrent workers ≈ 50 docs/hr/worker ≈
1,000 docs/hr fleet-wide. Spot ("flex") workers are the right choice: a
killed worker's job just returns to the queue.
