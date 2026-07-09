# pdf2md

A containerised, domain-agnostic PDF → Markdown tool. Bytes in (PDF), Markdown
out. No document-type-specific logic lives here — that belongs in whatever
pipeline calls this tool.

Built and hardened against real scanned financial reports, where silent
fidelity loss (a nil rendered as a fabricated number, a whole table dropped,
a name misspelled) is far more dangerous than an obvious failure. Every
non-obvious design decision below exists because a real fixture broke in
exactly that way.

## Routing

```
pdf2md-auto.sh INPUT.pdf [-o OUT.md]
```

1. **Derotate** — every page is checked for rotation (Tesseract OSD) and
   corrected before anything else touches it. Pure geometry: only the PDF's
   `/Rotate` flag is changed, never a pixel or a character.
2. **Classify** — digital (real text layer) vs. scanned (image-only), by
   PyMuPDF page-text extraction.
3. **Route** — digital → `engines/text` (pymupdf4llm, fast, CPU). Scanned →
   `engines/mineru` (MinerU, GPU OCR).

```bash
./pdf2md-auto.sh report.pdf                  # markdown to stdout
./pdf2md-auto.sh report.pdf -o report.md     # markdown to file (beside the PDF)
./pdf2md-auto.sh report.pdf --engine text    # force the fast CPU path
./pdf2md-auto.sh report.pdf --engine mineru  # force MinerU
./pdf2md-auto.sh report.pdf --no-derotate    # skip the rotation check
```

Routing/timing logs go to stderr and are mirrored to `logs/pdf2md-auto.log`
(`tail -f logs/pdf2md-auto.log` works across runs without re-pointing anything).

## Why two engines, and why MinerU for scans

Marker (the original OCR engine here) has a critical failure on dense,
borderless financial tables: wherever the source shows a nil (`-`), it
frequently substitutes a small, plausible **fabricated** number. Silent —
totals often still foot, so a totals-only check passes. Confirmed on a real
fixture: ten fabricated cells across all six statements of a scanned charity
financial report. An LLM table-refinement detour (local Ollama vision model,
explicit "preserve every value" prompt) fixed none of them — the fabrication
happens in the recognition layer, upstream of any formatting pass.

MinerU, tested head-to-head on the same fixture, correctly rendered every one
of those ten cells as empty. It's the scanned-document engine here. Marker was
archived (`archive/marker-engine/`) after never once beating MinerU across
three independent test fixtures — see that folder's README for the full
account.

## Known defects, and how each is handled

Three distinct silent-failure classes were found and fixed during validation.
Each needed a different kind of fix — worth understanding before touching
this code, since a fix for one class does not generalise to another.

### 1. Fabricated values in nil cells (numeric)
Marker-specific; not observed in MinerU across any tested fixture. The
project's design principle: **a table failing to blank (obviously unusable)
is safer than failing to a plausible wrong number (silent trap).** If this
resurfaces, the fix is a downstream re-footing validator (re-foot every line
item against its own stated total; not built here — belongs in the calling
pipeline, since it needs the document's schema).

### 2. Whole-table structural omission (rotation-triggered)
MinerU's layout model can lose the second of two stacked tables at a
footnote-interrupted seam — but only when the page is rotated (glyphs drawn
sideways, PDF `/Rotate` flag still reading 0, common on scanned landscape
schedules). Confirmed and fixed: `engines/text/pdf2md.py`'s
`detect_and_fix_rotation()`, run automatically by `pdf2md-auto.sh` before
every conversion.

- **Detection**: Tesseract OSD (orientation-and-script-detection) — reads
  stroke geometry only, never content. No model decision involved.
- **Correction**: only the PDF's `/Rotate` flag changes. No pixel, no text.
- **Self-verifying, never guesses**: Tesseract's OSD `rotate` field was found
  empirically to NOT map onto PDF's `/Rotate` direction consistently — two
  genuinely-rotated pages on the same real fixture needed *opposite*
  corrections. So both candidate corrections are tried, and whichever one a
  **fresh OSD pass confirms upright** is kept. If neither verifies, the page
  is left untouched and flagged loudly — never silently guessed.
- Recovered several additional tables on real scanned financial-report
  fixtures that were previously silently dropped.

### 3. Silent real-word substitution of an unusual proper noun (text)
MinerU's `hybrid-engine` backend runs a VLM stage that can "correct" an
unusual, repeated proper noun toward a common English word — inconsistently,
within the same document (confirmed on a real fixture: an organisation name
like "Reliabilty" silently "corrected" to "Reliability" in running prose,
dozens of times, while left correct in structured contexts like an ABN
line). Root-caused to the VLM specifically: MinerU's plain `pipeline`
backend (no VLM) never exhibits it — but `pipeline` also regresses on table
structure on harder documents (column collapse, value misalignment,
confirmed on a real fixture), so it can't just replace `hybrid-engine`.

**Fix, in `engines/mineru/mineru2md.py`'s `reconcile_spelling()`**: run
`pipeline` as a cheap reference-only pass alongside the primary `hybrid-engine`
pass. Wherever a repeated rare token in the primary output has a same-length,
same-document real-word "twin," and the reference pass shows an unambiguous,
uniform preference for the rare form, substitute deterministically. This is
**not a model decision** — it's mechanical cross-pass consensus between two
already-computed, independent outputs. Table structure always comes from
`hybrid-engine`, untouched; only this narrow token-level pattern is patched.

Same-length matching is required and was tuned by a real false positive during
testing: `"Expense"` (correct, a table header) was nearly rewritten to
`"expensed"` — edit-distance 1, but a genuinely different word (a length
change, not a misread), not the OCR-glyph-confusion signature same-length
substitutions like `"Reliabilty"`/`"Reliability"` actually represent.

On by default; skip with `--no-reconcile-spelling` (roughly doubles MinerU's
per-document runtime — the reference pass costs about 40s on top of
`hybrid-engine`'s ~85s).

## Setup

Fresh machine (including WSL2): run `./check-deps.sh` first — verifies/installs
git, Docker Engine, NVIDIA Container Toolkit (only if a GPU is present), and
`inotify-tools` (for `watch.sh`). Safe to re-run at any point.

## Build

```bash
# text engine (CPU only, no GPU)
cd engines/text && docker build -t pdf2md-text .

# mineru engine — build the upstream base first (large, slow, one-time).
# Dockerfile.mineru is a pinned copy of MinerU's own official base image
# definition, committed here for a reproducible build that doesn't depend
# on an external URL staying available/unchanged.
cd engines/mineru
docker build -t mineru:latest -f Dockerfile.mineru .
docker build -t pdf2md-mineru .
```

The MinerU base bakes all model weights in at build time (reproducible,
offline, no runtime download — ~43GB image). The text engine is CPU-only,
no weights to cache.

## Requirements

- Docker with GPU passthrough for the MinerU engine — `check-deps.sh`
  verifies this with `docker run --rm --gpus all
  nvidia/cuda:12.5.0-base-ubuntu22.04 nvidia-smi`, which should show your
  GPU. The text engine needs no GPU.
- `--shm-size 32g --ipc=host` for MinerU's vLLM-based hybrid backend (already
  set in `engines/mineru/mineru.sh`).
- `inotify-tools`, only if you want `watch.sh`'s folder watcher (not needed
  for direct `pdf2md-auto.sh` use).

## Layout

```
pdf2md-auto.sh              # main entrypoint: derotate -> classify -> route
engines/
  text/                     # digital-PDF path: pymupdf4llm + classify + derotate (CPU)
  mineru/                   # scanned-PDF path: MinerU hybrid-engine + spelling reconciliation (GPU)
  docling/                  # alternative engine, direct-call only (not yet in pdf2md-auto.sh's
                             # routing or containerized — see docling2md.py's own docstring).
                             # Evaluated 2026-07-10 on a 100-doc real-fixture sample: comparable
                             # table structure, genuine Markdown pipe tables (not HTML), no
                             # fabrication pattern found -- but 3 silent whole-statement-omission
                             # cases in 100, no error raised. Same rule as the other two engines:
                             # never trust unattended without a downstream completeness check.
archive/
  marker-engine/            # retired OCR engine, kept for reference — see its README
  mineru-ab-testing/        # compare.sh, the Marker-vs-MinerU A/B harness (comparison settled)
docs/
  conversion-limitations-*.md           # per-fixture evaluation reports (non-numeric findings)
test-fixtures/              # drop your own local test documents here (gitignored, never committed)
logs/                       # runtime logs (gitignored)
```

## Notes

- Streams: clean markdown goes to **stdout**; all routing/timing/library
  messages go to **stderr**. `pdf2md-auto.sh x.pdf > x.md` gives a clean file.
- This tool is intentionally domain-agnostic. Schema, page-selection, and
  provenance logic belong in the pipeline that *calls* it, not here.
- Do NOT trust a totals-only match when validating a new fixture — several of
  the fabrication defects found here leave the total correct. Re-foot each
  line item against its own stated total.
