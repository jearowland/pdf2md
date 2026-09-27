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

Ten distinct silent-failure classes were found and fixed during validation.
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

### 4. Designed pages sent to OCR, and text hidden under redactions (layout, privacy)
A one-page grid flyer (day boxes of visible text set over a background
photo) has a complete text layer, but its background image covered 67.5% of
the page, so the `image_page` rule sent it to MinerU. MinerU lost the title
(white text on a coloured band) and scrambled the box order. Docling kept
only the footer. Four of the boxes were also blacked out: a *cosmetic*
redaction, with the original text still in the file, and a name was tucked
under the header band. OCR reads only what renders, so it rightly left those
out; the text engine reads the text layer and emitted them all, silently.

Three fixes, each general:

- **Covered text is removed before any engine runs** (`pdf2md.py
  --prepare`, run by the router and by `pdf2md-auto.sh`). Text counts as
  covered only when an opaque filled *rectangle* is drawn over it (by drawing
  order) **and** removing it leaves the rendered page unchanged (at most 4
  pixels at 144 dpi; a visible nil dash changes 20+). The geometry alone
  flagged plainly visible text on real annual reports (masked fills, a
  near-white table fill over statement rows, rotated pages); the render
  comparison clears all of those. Covered text is removed with a real PDF
  redaction; graphics and images are untouched, so the page renders as
  before. A document with nothing to change is copied byte for byte. Text under a dark fill leaves one invisible
  `[redacted]` marker per block in the text layer; text under any other
  fill is dropped without one. The manifest gets a `covered_text_removed`
  warning per page. Only rectangles count as cover: a curved or slanted
  shape's bounding box can overlap text it doesn't hide.
- **Background images don't make an image page.** An image with enough
  *visible* text drawn *over* it is background: the text is the content.
  Invisible text (render mode 3, the OCR layer of a scan or a pasted
  screenshot) and text underneath the image don't count, so the case
  `image_page` exists for still routes to OCR. The page reports
  `background_image_coverage` beside `image_coverage`. On a sample of 800
  real annual reports, every page this changed was a designed page or a
  letter on full-page letterhead, and none was a scan or a pasted table.
- **Content-loss check** (`engines/text/verify_text.py`): per page, the share
  of the (visible) text layer's words the output kept, counting the page and
  its neighbours, so a block moved across a page break isn't lost. Below 80%,
  the manifest gets a `text_layer_content_missing` warning with a sample of
  the missing words. It is report-only, like `verify_numbers`.

### 5. Ligatures extracted as junk (text)
Word-exported PDFs often draw "ti", "ff" and the like as single ligature
glyphs and leave them out of the font's ToUnicode map, so "Operating"
extracts as `OperaƟng` or `Opera�ng`. The embedded font's own GSUB ligature
table says what each ligature glyph stands for, so `--prepare` adds the
missing map entries from there (Identity-H TrueType fonts only; existing
entries are never changed). The manifest lists them under `ligature_fixes`.

### 6. Text over a background image flattened across columns (layout)
The text engine's layout model treats text drawn over an image as "picture
text" and writes it out line by line across the whole image. On the grid
flyer, that interleaved five columns of day boxes and lost a line. The text
engine now hides, in its own in-memory copy only, every image that covers at
least 5% of the page and has visible text drawn over it; the same model then
finds the grid and emits a table. Logos, icons, photos without text on them,
and signatures (a name printed over a signature image is 1-2% of the page;
hiding one lost that line on a real declaration) stay. OCR engines still see
the real page.

A ligature glyph's later letters get zero-width boxes, and the table-cell
path clips them ("snowflakes" came out "snowfakes"). A word that appears
nowhere on the page is repaired only when exactly one of the page's own
words equals it with a ligature's letters restored.

### 7. Upright pages turned upside down by derotation (geometry)
Tesseract's orientation check (OSD) called four upright notes pages of a real
report "rotated 180", and its own recheck "confirmed" the flip; the text
engine then emitted nothing for them. A digital page's visible text layer
says which way it reads, exactly, so derotation now skips OSD when at least
90% of a page's visible text runs left to right as displayed, unless the
page is mostly covered by a non-background image (a pasted scan printed
sideways under an upright header still goes to OSD). Scans (no visible
text) are unchanged. When nothing is corrected, the original bytes are passed
on instead of a re-save. A side effect: derotation no longer runs OSD on
digital pages, so it takes about half the time.

### 8. Undecodable text as runs of U+FFFD (text)
A stamp in a font that is neither embedded nor mapped to Unicode has no
recoverable text; it came out as a highlighted run of `�` at the top of nearly
every page of a real report. The text engine writes a run of four or more as
`[undecodable text]`.

### 9. One document index, with global page numbers (merge)
Each routed run's engine appended its own heading index, numbered from that
run's first page, so a mixed document had several indexes mid-document with
wrong page numbers. The router now drops them and indexes the merged
document once.

### 10. Statement rows merged, thousands separators dropped (tables)
On tightly spaced statement rows the text engine's table path can cut
through a number's low-sitting commas: on a real report "(1,207,513)" came
out "(1207513)" (its commas landing in the next row as ",,"), "66,529" as
"66529", and pairs of rows were merged into one ("Additions<br>Disposals").
A bare number found nowhere on the page gets its separators back when
exactly one word in the page's own text layer has the same digits; a body
row whose first cell holds 2+ labels and whose other cells hold the same
number of amounts is split back into rows. Headers, wrapped labels and prose
are never split. On 60 corpus documents: 3 rows split, all correct; no amount
changed.

## Icon alt text (opt-in)

`pdf2md_route.py --alt-text-ollama URL` (also through `pdf2md-auto.sh`) labels
icon-sized images on text-engine pages with a local vision model
(`--alt-text-model`, default `qwen2.5vl:7b`) and writes each label into the
output where the icon sits, as `[icon: bus]`. On a grid flyer, each bus icon
lands in its day's table cell. Icons are cropped in the text container, which has
no network; the router makes the calls on the host, cleans each reply to 1-5
plain words, and unloads the model afterwards. An icon that can't be
labelled shows as `[icon]`. Off by default: without the flag, output is
unchanged. The labels are listed in the manifest under `icons`.

## MinerU start-up: one server per job

A fresh MinerU container spends ~30 s loading models before seconds of GPU
work, so a document with several scanned stretches, a batch of files or a UAT
run was mostly model loading. `tools/mineru-session` runs one MinerU for a
whole job instead (`mineru2md.py --serve`, calling MinerU in-process so its
models stay loaded), hardened like the one-shot container and mounting only its
own queue folder. `engines/mineru/mineru.sh` hands work to it whenever
`PDF2MD_MINERU_SERVER` is set:

```bash
tools/mineru-session run -- ./pdf2md-auto.sh a.pdf -o a.md     # one job, one model load
```

The router starts one per document by itself when a document has two or more
MinerU runs; `tools/uat.py --remote` starts one for the whole run. With starts
cheap, a page with a healthy text layer is no longer folded into a
whole-document MinerU run: on a report of 35 scanned and 5 digital pages,
that run lost the digital pages outright.

## Regression testing

`tools/uat.py` re-checks every verified fix against a **private** case list
(JSONL, kept beside the documents and never in this repo; the format is in
the script's docstring). Run the routing-only tier on core after any
classifier or routing change, and the full tier on a GPU worker before
merging:

```bash
tools/uat.py CASES.jsonl --classify-only --dev-bind       # seconds per document, CPU
gpu run --kind convert --needs docker -- tools/uat.py CASES.jsonl --remote '$GPU_HOST'
```

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
