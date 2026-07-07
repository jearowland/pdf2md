#!/usr/bin/env python3
"""
pdf2md_route.py -- per-PAGE engine routing for PDF -> Markdown conversion.

Why this exists: pdf2md-auto.sh routes each *document* wholesale to either
the fast CPU text engine (real text layer) or the MinerU GPU OCR engine
(scanned). Real documents are frequently MIXED, and whole-document routing
cannot express that:

  - a digital report with a few scanned pages at the end (signed letters,
    certificates, stamped appendices) routes 'digital' and silently emits
    nothing for those pages;
  - an image-dense but fully DTP-authored document (a designed annual
    report, a brochure) can route 'scan' and burn GPU minutes OCR-ing text
    that already exists as a perfect text layer -- usually producing WORSE
    text than the layer it ignored;
  - a digital document whose tables are pasted as pictures gets its tables
    flattened by the text engine (the classify() docstring documents this
    as a known, previously-unfixed case).

The unit of failure is the page, so the unit of routing must be the page.
Flow (each step is one of the same containers pdf2md-auto.sh already uses):

  1. derotate            (unchanged, whole file -- geometry only)
  2. --classify-pages    (per-page fact rows: class text|ocr + reasons)
  3. plan runs           (contiguous same-class page runs; fast paths below)
  4. --slice + convert   (each run through its engine; MinerU calls go via
                          engines/mineru/mineru.sh and therefore inherit its
                          host-wide GPU flock)
  5. merge               (page markers renumbered to GLOBAL page numbers)
  6. manifest            (<out>.manifest.json -- per-page FACTS + intrinsic
                          warnings only; see below)
  7. verify_numbers      (unchanged, report-only)

Fast paths: all pages one class -> exactly today's behaviour, one engine,
no slicing. OCR share above --whole-doc-ocr-ratio -> whole-document MinerU
(slicing overhead isn't worth it to save a page or two of OCR).

The manifest is deliberately OPINION-FREE. It records what happened
(per-page class, engine used, text chars, image coverage, emitted table
rows) plus only two warnings that are intrinsic to ALL PDFs regardless of
domain: (a) the merged output does not reach the final page, (b) a page
with a healthy text layer emitted nothing. Judgments like "this document
should contain tables" belong to callers, who know what kind of document
they gave us -- this tool does not.

Usage:
  pdf2md_route.py report.pdf -o report.md
  pdf2md_route.py report.pdf -o report.md --keep-parts   # keep run artifacts
  pdf2md_route.py report.pdf -o report.md --dev-bind     # bind-mount local
        engines/text/pdf2md.py over the baked one (test new classifier code
        without rebuilding the image)
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
MINERU_SH = REPO / "engines" / "mineru" / "mineru.sh"
PAGE_MARKER_RE = re.compile(r"<!-- page (\d+) -->")


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def docker_text(workdir: Path, argv: list[str], dev_bind: bool,
                capture: bool = False):
    """Run the pdf2md-text container exactly the way pdf2md.sh does
    (input dir mounted as /work, caller uid/gid), optionally overlaying the
    local pdf2md.py for pre-rebuild testing."""
    cmd = ["docker", "run", "--rm",
           "--user", f"{_uid()}:{_gid()}", "-e", "HOME=/tmp",
           "-v", "/etc/passwd:/etc/passwd:ro", "-v", "/etc/group:/etc/group:ro",
           "-v", f"{workdir}:/work"]
    if dev_bind:
        cmd += ["-v", f"{REPO/'engines/text/pdf2md.py'}:/usr/local/bin/pdf2md.py:ro"]
    cmd += ["pdf2md-text"] + argv
    return subprocess.run(cmd, check=True, text=True,
                          capture_output=capture)


def _uid():
    import os
    return os.getuid()


def _gid():
    import os
    return os.getgid()


def plan_runs(per_page: list[dict], whole_doc_ocr_ratio: float,
              max_runs: int) -> list[tuple[int, int, str]]:
    """Contiguous same-class runs [(first_page, last_page, engine)].

    No absorption of minority pages into neighbouring runs: converting a
    text page with OCR risks regressing content that was already perfect,
    and converting an OCR page with the text engine emits nothing -- both
    directions of 'rounding' cost accuracy to save container spins.
    Two guards keep pathological inputs off the slow path:
      - OCR share >= whole_doc_ocr_ratio -> one whole-document MinerU run
      - more than max_runs alternations  -> ditto (page-level alternation
        that fine usually means the classifier is fighting the document;
        MinerU handles every page kind, just slowly)
    """
    n = len(per_page)
    ocr_share = sum(1 for p in per_page if p["class"] == "ocr") / max(n, 1)
    if ocr_share >= whole_doc_ocr_ratio:
        return [(1, n, "mineru")]
    runs: list[tuple[int, int, str]] = []
    for p in per_page:
        eng = "mineru" if p["class"] == "ocr" else "text"
        if runs and runs[-1][2] == eng and runs[-1][1] == p["page"] - 1:
            runs[-1] = (runs[-1][0], p["page"], eng)
        else:
            runs.append((p["page"], p["page"], eng))
    if len(runs) > max_runs:
        return [(1, n, "mineru")]
    return runs


def renumber(md: str, first_global_page: int) -> str:
    """Chunk-local page markers -> global page numbers."""
    return PAGE_MARKER_RE.sub(
        lambda m: f"<!-- page {int(m.group(1)) + first_global_page - 1} -->", md)


def table_rows_per_page(md: str, total_pages: int) -> dict[int, int]:
    """Count emitted markdown table rows per global page. Markers sit
    BETWEEN pages, so text before the first marker belongs to page 1."""
    counts = {p: 0 for p in range(1, total_pages + 1)}
    page = 1
    for line in md.splitlines():
        m = PAGE_MARKER_RE.match(line.strip())
        if m:
            page = int(m.group(1))
            continue
        if line.lstrip().startswith("|"):
            counts[page] = counts.get(page, 0) + 1
    return counts


def main():
    ap = argparse.ArgumentParser(description="per-page engine routing for PDF->md")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True,
                    help="output markdown path (manifest lands beside it)")
    ap.add_argument("--no-derotate", action="store_true")
    ap.add_argument("--whole-doc-ocr-ratio", type=float, default=0.8)
    ap.add_argument("--max-runs", type=int, default=24)
    ap.add_argument("--keep-parts", action="store_true",
                    help="keep per-run slice PDFs and chunk markdowns")
    ap.add_argument("--dev-bind", action="store_true",
                    help="overlay local engines/text/pdf2md.py into the "
                         "container (test classifier changes pre-rebuild)")
    args = ap.parse_args()

    t0 = time.time()
    pdf = Path(args.input).resolve()
    out_md = Path(args.output).resolve()
    workdir = pdf.parent
    if out_md.parent != workdir:
        err("[route] ERROR: -o must sit beside the input PDF (single /work mount)")
        sys.exit(6)
    stem = pdf.stem

    # 1. derotate (geometry only; same sibling-artifact convention as auto.sh)
    if not args.no_derotate:
        err("[route] checking page rotation...")
        docker_text(workdir, [f"/work/{pdf.name}", "--derotate",
                              f"/work/{stem}.derotated.pdf"], args.dev_bind)
        pdf = workdir / f"{stem}.derotated.pdf"

    # 2. per-page classification (facts only)
    r = docker_text(workdir, [f"/work/{pdf.name}", "--classify-pages", "--quiet"],
                    args.dev_bind, capture=True)
    report = json.loads(r.stdout)
    per_page, pc = report["per_page"], report["pages"]

    # 3. plan
    runs = plan_runs(per_page, args.whole_doc_ocr_ratio, args.max_runs)
    err(f"[route] {pc} pages -> {len(runs)} run(s): " +
        ", ".join(f"p{a}-{b}:{e}" for a, b, e in runs))

    # 4. convert each run
    parts: list[tuple[tuple[int, int, str], Path]] = []
    for a, b, eng in runs:
        if (a, b) == (1, pc):
            piece_pdf = pdf                      # fast path: no slice needed
        else:
            piece_pdf = workdir / f"{stem}.p{a:04d}-{b:04d}.pdf"
            docker_text(workdir, [f"/work/{pdf.name}", "--slice", f"{a}-{b}",
                                  "-o", f"/work/{piece_pdf.name}"], args.dev_bind)
        piece_md = workdir / f"{stem}.p{a:04d}-{b:04d}.md"
        if eng == "text":
            docker_text(workdir, [f"/work/{piece_pdf.name}",
                                  "-o", f"/work/{piece_md.name}"], args.dev_bind)
        else:
            # via mineru.sh so the host-wide GPU flock applies. -o is
            # forwarded into the container verbatim and must be relative to
            # the input's directory (mounted as /work) -- an absolute host
            # path is invisible in there.
            subprocess.run([str(MINERU_SH), str(piece_pdf),
                            "-o", piece_md.name], check=True)
        parts.append(((a, b, eng), piece_md))

    # 5. merge with global page numbering. Markers sit BETWEEN pages inside a
    # chunk; at each chunk boundary we add the boundary page's marker
    # explicitly (except before global page 1) so global numbering never
    # depends on which engine produced the previous chunk.
    merged: list[str] = []
    for (a, b, eng), piece_md in parts:
        chunk = piece_md.read_text(encoding="utf-8", errors="replace")
        chunk = renumber(chunk, a)
        if a > 1:
            merged.append(f"\n\n<!-- page {a} -->\n\n")
        merged.append(chunk)
    final = "".join(merged)
    out_md.write_text(final, encoding="utf-8")

    # 6. manifest: facts + the two domain-free warnings
    rows = table_rows_per_page(final, pc)
    emitted_last = max([int(m.group(1)) for m in
                        PAGE_MARKER_RE.finditer(final)] + [1])
    warnings = []
    if emitted_last < pc:
        warnings.append({"kind": "output_ends_early",
                         "detail": f"last emitted page marker {emitted_last} "
                                   f"of {pc} PDF pages"})
    seg_chars = {pnum: 0 for pnum in range(1, pc + 1)}
    page = 1
    for line in final.splitlines():
        m = PAGE_MARKER_RE.match(line.strip())
        if m:
            page = int(m.group(1))
            continue
        seg_chars[page] = seg_chars.get(page, 0) + len(line)
    for p in per_page:
        if p["class"] == "text" and p["text_chars"] > 200 \
                and seg_chars.get(p["page"], 0) < 20:
            warnings.append({"kind": "text_page_empty_output",
                             "page": p["page"],
                             "detail": f"text layer has {p['text_chars']} chars "
                                       f"but output segment is near-empty"})

    manifest = {
        "source": str(Path(args.input).name),
        "pdf_pages": pc,
        "engine_runs": [{"pages": [a, b], "engine": e} for a, b, e in runs],
        "per_page": [{**p, "engine": next(e for a, b, e in runs
                                          if a <= p["page"] <= b),
                      "table_rows_emitted": rows.get(p["page"], 0),
                      "output_chars": seg_chars.get(p["page"], 0)}
                     for p in per_page],
        "warnings": warnings,
    }
    man_path = out_md.with_suffix(".manifest.json")
    man_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    # 7. number-preservation check (unchanged from auto.sh, report-only)
    try:
        subprocess.run(["docker", "run", "--rm", "-v", f"{workdir}:/work",
                        "--user", f"{_uid()}:{_gid()}", "-e", "HOME=/tmp",
                        "-v", "/etc/passwd:/etc/passwd:ro",
                        "-v", "/etc/group:/etc/group:ro",
                        "--entrypoint", "python3", "pdf2md-text",
                        "/usr/local/bin/verify_numbers.py",
                        f"/work/{pdf.name}", f"/work/{out_md.name}"],
                       check=False)
    except Exception:
        pass

    if not args.keep_parts:
        for (a, b, eng), piece_md in parts:
            if piece_md != out_md:
                piece_md.unlink(missing_ok=True)
            piece_pdf = workdir / f"{stem}.p{a:04d}-{b:04d}.pdf"
            piece_pdf.unlink(missing_ok=True)

    err(f"[route] wrote {out_md} + {man_path.name} "
        f"({len(warnings)} warning(s)) in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
