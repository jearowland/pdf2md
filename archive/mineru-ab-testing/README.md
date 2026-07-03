# Archived: Marker vs. MinerU A/B harness

`compare.sh` ran one PDF through both engines side by side to decide which
should be the scanned-document engine. That comparison is settled — MinerU
won on every fixture tested (see `../marker-engine/README.md`) and is wired
into `pdf2md-auto.sh`.

Kept for reference if a future OCR engine needs the same head-to-head
treatment. Paths inside it (`MARKER_DIR`/`MINERU_DIR`) predate the current
`engines/text/` + `engines/mineru/` layout and would need updating to reuse.
