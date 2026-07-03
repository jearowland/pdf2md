# Archived: Marker OCR engine

This is the original Marker/Surya-based OCR engine, archived here after MinerU
replaced it as the scanned-document engine. Kept for reference, not maintained.

## Why archived

Marker was the first OCR engine used in this project. On dense, borderless
financial tables it has a critical failure: wherever the source shows a nil
(`-`), it frequently substitutes a small, plausible **fabricated** number. This
is silent — the row/column total is often still correct, so a totals-only check
passes — and only surfaces when each line item is re-footed against its own
stated total. Validated on a real scanned financial-report fixture: ten
confirmed fabricated cells across all six financial statements.

A long detour tried to fix this with Marker's LLM table-refinement pass
(`--use-llm`, pointed at a local Ollama vision model, with a structure-only
"preserve every value exactly" prompt — see `PRESERVE_VALUES_PROMPT` in
`pdf2md.py` below). **Not one of the ten fabrications was corrected.** The
fabrications are born in the recognition/OCR layer, upstream of formatting —
changing the formatter/LLM pass afterward cannot fix them. HTML table output
made things categorically worse on dense tables (columns dropped, values
scrambled), so that lever was also a dead end.

MinerU (`../../engines/mineru/`) was tested head-to-head on the same fixture and
correctly rendered every one of the ten cells as empty. It became the scanned-
document engine. Across every subsequent test this session (three independent
fixtures), MinerU was never beaten by Marker on any dimension — table structure,
nil-cell fidelity, or text fidelity. There is currently no known case where
falling back to Marker would help, so it was archived rather than kept wired in
as a "just in case" option.

## What's here

- `pdf2md.py` — the full router, including `to_markdown_marker()`, the Ollama
  integration, and all the table-fidelity flags (`--use-llm`, `--preserve-values`,
  `--html-tables`, `--max-table-rows`, etc.).
- `Dockerfile` — pulls in `marker-pdf` (CUDA-enabled torch, Surya models).
- `pdf2md.sh` — run wrapper, includes `--gpus all` and
  `--add-host=host.docker.internal:host-gateway` (needed for reaching a host
  Ollama instance).

## If you need to revive this

Build: `docker build -t pdf2md-marker -f archive/marker-engine/Dockerfile archive/marker-engine/`.
Run: `archive/marker-engine/pdf2md.sh INPUT.pdf --engine marker [flags]`. Requires
a local Ollama instance for `--use-llm` (`ollama pull qwen2.5vl:7b`, a vision
model — Marker's LLM table-refine needs vision, not text-only).
