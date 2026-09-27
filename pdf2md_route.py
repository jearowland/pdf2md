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
  1b. --prepare          (text hidden under opaque rectangles removed, the
                          page rendering as before; ligature glyphs given
                          their text back from the font's own tables)
  2. --classify-pages    (per-page fact rows: class text|ocr + reasons)
  3. plan runs           (contiguous same-class page runs; fast paths below)
  4. --slice + convert   (each run through its engine; MinerU calls go via
                          engines/mineru/mineru.sh and therefore inherit its
                          host-wide GPU flock)
  5. merge               (page markers renumbered to GLOBAL page numbers)
  6. manifest            (<out>.manifest.json -- per-page FACTS + intrinsic
                          warnings only; see below), including verify_text's
                          per-page text-layer recall
  7. verify_numbers      (unchanged, report-only)

Fast paths: all pages one class -> exactly today's behaviour, one engine,
no slicing. OCR share above --whole-doc-ocr-ratio -> whole-document MinerU
(slicing overhead isn't worth it to save a page or two of OCR).

The manifest is deliberately OPINION-FREE. It records what happened
(per-page class, engine used, text chars, image coverage, emitted table
rows, text-layer recall) plus only warnings that are intrinsic to ALL PDFs
regardless of domain: (a) the merged output does not reach the final page
(kind output_ends_early), (b) a page with a healthy text layer emitted
nothing (text_page_empty_output), (c) a page's output kept too little of its
text layer's wording (text_layer_content_missing -- the engine dropped
blocks; see engines/text/verify_text.py), (d) text hidden under opaque
shapes was removed before conversion (covered_text_removed -- a cosmetic
redaction, or text under a panel). Judgments like "this document
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
WHOLE_DOC_OCR_RATIO = 0.8   # see plan_runs
HEALTHY_TEXT_CHARS = 200    # a text page this full is never OCR'd whole-document
MAX_RUNS = 24


TITLE_INDEX_RE = re.compile(r"\n*<!-- pdf2md document index.*?-->\n?", re.S)


def text_engine():
    """engines/text/pdf2md.py as a module, for its document-index builder
    (it imports only the standard library at module level)."""
    import importlib.util
    if "pdf2md_text_engine" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "pdf2md_text_engine", REPO / "engines" / "text" / "pdf2md.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pdf2md_text_engine"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["pdf2md_text_engine"]


ALT_TEXT_PROMPT = ("This is a small icon cut from a document. Reply with a label of one to "
                   "five words saying what it shows (for example: bus, phone, warning sign, "
                   "tick). Reply with the label only.")


def clean_label(text: str) -> str:
    """A model reply as a 1-5 word label: first line, letters, digits,
    spaces and hyphens only, lower case. Anything else is dropped, so a
    chatty or odd reply can't inject markup into the output."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    words = re.sub(r"[^A-Za-z0-9 \-]", " ", line).lower().split()
    # "bus icon" -> "bus": the output already says [icon: ...]
    while len(words) > 1 and words[-1] in ("icon", "symbol", "pictogram"):
        words.pop()
    if len(words) > 1 and words[0] in ("a", "an", "the"):
        words.pop(0)
    return " ".join(words[:5])


def free_mineru_gpu(timeout: int = 180) -> bool:
    """Ask this job's MinerU server (if one is running) to unload its models
    and wait until it has: one GPU model at a time. With both loaded, a
    24 GB card peaked at 23.9 GB in testing, and a 12 GB card would run out."""
    import os
    q = os.environ.get("PDF2MD_MINERU_SERVER", "")
    if not q or not Path(q, "requests").is_dir() or Path(q, "exited").exists():
        return True
    Path(q, "unloaded").unlink(missing_ok=True)
    Path(q, "unload").touch()
    for _ in range(timeout):
        if Path(q, "unloaded").exists() or Path(q, "exited").exists():
            return True
        time.sleep(1)
    err("[route] alt text: the MinerU server did not unload in time; skipping alt text")
    return False


def label_icons(icon_dir: Path, icons: list[dict], url: str, model: str) -> list[dict]:
    """Ask a local vision model (Ollama at `url`) for each icon's label; an
    icon it can't label keeps an empty label (rendered "[icon]"). The model is
    unloaded afterwards (keep_alive 0) so the card is free for the next job.
    Runs on the host: the engine containers have no network."""
    import base64
    import urllib.request

    def call(payload: dict, timeout: int = 180) -> dict:
        req = urllib.request.Request(url.rstrip("/") + "/api/generate",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    out = []
    for icon in icons:
        label = ""
        try:
            img = base64.b64encode((icon_dir / icon["file"]).read_bytes()).decode()
            reply = call({"model": model, "prompt": ALT_TEXT_PROMPT, "images": [img],
                          "stream": False, "options": {"temperature": 0}})
            label = clean_label(reply.get("response", ""))
        except Exception as e:
            err(f"[route] alt text: icon {icon['file']} not labelled: {e}")
        out.append({"page": icon["page"], "rect": icon["rect"], "label": label})
    try:
        call({"model": model, "keep_alive": 0}, timeout=60)
    except Exception as e:
        err(f"[route] alt text: could not unload {model}: {e}")
    return out


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def docker_text(workdir: Path, argv: list[str], dev_bind: bool,
                capture: bool = False, entrypoint: str | None = None):
    """Run the pdf2md-text container exactly the way pdf2md.sh does
    (input dir mounted as /work, caller uid/gid), optionally overlaying the
    local pdf2md.py for pre-rebuild testing."""
    cmd = ["docker", "run", "--rm", *docker_user(), *DOCKER_HARDEN, "-e", "HOME=/tmp",
           "-v", "/etc/passwd:/etc/passwd:ro", "-v", "/etc/group:/etc/group:ro",
           "-v", f"{workdir}:/work"]
    if dev_bind:
        cmd += ["-v", f"{REPO/'engines/text/pdf2md.py'}:/usr/local/bin/pdf2md.py:ro",
                "-v", f"{REPO/'engines/text/verify_text.py'}:/usr/local/bin/verify_text.py:ro"]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    cmd += ["pdf2md-text"] + argv
    return subprocess.run(cmd, check=True, text=True,
                          capture_output=capture)


# as engines/docker-user.sh's DOCKER_HARDEN (security review F-03): documents
# come from outside, so no network, no capabilities, no privilege escalation,
# pids/memory limits
DOCKER_HARDEN = ["--network", "none", "--cap-drop", "ALL", "--security-opt",
                 "no-new-privileges", "--pids-limit", "1024", "--memory", "12g"]
_DOCKER_USER: list[str] | None = None


def docker_user() -> list[str]:
    """The run's user mapping, as engines/docker-user.sh sets it: --user
    uid:gid under rootful Docker (the GPU workers) so output files stay the
    caller's; nothing under rootless Docker (core), where the container's
    root already IS the caller and --user maps to an unrelated subordinate
    uid that can't write the output folder."""
    global _DOCKER_USER
    if _DOCKER_USER is None:
        info = subprocess.run(["docker", "info", "--format", "{{.SecurityOptions}}"],
                              capture_output=True, text=True)
        _DOCKER_USER = [] if "rootless" in info.stdout \
            else ["--user", f"{_uid()}:{_gid()}"]
    return _DOCKER_USER


def _uid():
    import os
    return os.getuid()


def _gid():
    import os
    return os.getgid()


def parse_json_report(stdout: str) -> dict:
    """Parse the container's JSON report, tolerating stray lines around it.
    A library warning printed to stdout (a deprecation notice on import, say)
    must not break routing; the skipped text is echoed to stderr, not hidden."""
    start = stdout.find("{")
    if start < 0:
        raise SystemExit(f"[route] ERROR: no JSON in classifier output: {stdout[:300]!r}")
    report, end = json.JSONDecoder().raw_decode(stdout, start)
    stray = (stdout[:start] + stdout[end:]).strip()
    if stray:
        err(f"[route] ignored non-JSON classifier stdout: {stray[:300]!r}")
    return report


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
    # Never fold a page with a healthy text layer into a whole-document OCR
    # run: on a real report of 35 scanned pages and 5 digital ones, MinerU's
    # whole-document run lost the digital pages outright (three empty, one
    # at 5% of its words). The shortcut only saved container start-ups,
    # which a document's MinerU server (tools/mineru-session) now makes cheap.
    healthy_text = any(p["class"] == "text" and p["text_chars"] >= HEALTHY_TEXT_CHARS
                       for p in per_page)
    if ocr_share >= whole_doc_ocr_ratio and not healthy_text:
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


def convert_runs(runs, pdf, pc, workdir, stem, labels, args):
    """Slice each run out and convert it through its engine; [(run, md)]."""
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
            argv = [f"/work/{piece_pdf.name}", "-o", f"/work/{piece_md.name}"]
            run_labels = [{**l, "page": l["page"] - a + 1} for l in labels
                          if a <= l["page"] <= b]
            if run_labels:
                lab_path = workdir / f"{stem}.p{a:04d}-{b:04d}.icon-labels.json"
                lab_path.write_text(json.dumps(run_labels), encoding="utf-8")
                argv += ["--icon-labels", f"/work/{lab_path.name}"]
            docker_text(workdir, argv, args.dev_bind)
        else:
            # via mineru.sh so the host-wide GPU flock applies. -o is
            # forwarded into the container verbatim and must be relative to
            # the input's directory (mounted as /work) -- an absolute host
            # path is invisible in there.
            subprocess.run([str(MINERU_SH), str(piece_pdf),
                            "-o", piece_md.name], check=True)
        parts.append(((a, b, eng), piece_md))
    return parts


def main():
    ap = argparse.ArgumentParser(description="per-page engine routing for PDF->md")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True,
                    help="output markdown path (manifest lands beside it)")
    ap.add_argument("--no-derotate", action="store_true")
    ap.add_argument("--whole-doc-ocr-ratio", type=float, default=WHOLE_DOC_OCR_RATIO)
    ap.add_argument("--max-runs", type=int, default=MAX_RUNS)
    ap.add_argument("--keep-parts", action="store_true",
                    help="keep per-run slice PDFs and chunk markdowns")
    ap.add_argument("--alt-text-ollama", metavar="URL",
                    help="label icon-sized images on text-engine pages with a local vision "
                         "model at this Ollama URL (e.g. http://localhost:11434 on a GPU "
                         "worker); written into the output as [icon: label]. Off by default.")
    ap.add_argument("--alt-text-model", default="qwen2.5vl:7b",
                    help="Ollama vision model for --alt-text-ollama (default qwen2.5vl:7b)")
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

    # 1b. the copy every engine converts: text hidden under opaque rectangles
    # (cosmetic redactions, text under a panel) removed -- an OCR engine reads
    # only what renders, a text-layer engine would read it all -- and ligature
    # glyphs given their text back. See pdf2md.py's prepare_pdf. A document
    # needing neither is copied byte for byte.
    r = docker_text(workdir, [f"/work/{pdf.name}", "--prepare",
                              f"/work/{stem}.prepared.pdf", "--quiet"],
                    args.dev_bind, capture=True)
    prepared = parse_json_report(r.stdout)
    covered = {c["page"]: c for c in prepared["pages"]}
    pdf = workdir / f"{stem}.prepared.pdf"
    if covered:
        err(f"[route] removed text hidden under opaque shapes on page(s) {sorted(covered)}")
    for f in prepared["ligature_fixes"]:
        err(f"[route] font {f['font']}: {len(f['glyphs'])} ligature glyph(s) given their text back")

    # 2. per-page classification (facts only)
    r = docker_text(workdir, [f"/work/{pdf.name}", "--classify-pages", "--quiet"],
                    args.dev_bind, capture=True)
    report = parse_json_report(r.stdout)
    per_page, pc = report["per_page"], report["pages"]

    # 3. plan
    runs = plan_runs(per_page, args.whole_doc_ocr_ratio, args.max_runs)
    err(f"[route] {pc} pages -> {len(runs)} run(s): " +
        ", ".join(f"p{a}-{b}:{e}" for a, b, e in runs))

    # 3b. alt text for icons on text-engine pages (opt-in): crop them in the
    # container, label them here on the host, hand each text run its labels
    labels: list[dict] = []
    if args.alt_text_ollama and any(eng == "text" for _, _, eng in runs):
        icon_dir = workdir / f"{stem}.icons"
        icon_dir.mkdir(exist_ok=True)
        r = docker_text(workdir, [f"/work/{pdf.name}", "--extract-icons",
                                  f"/work/{icon_dir.name}", "--quiet"],
                        args.dev_bind, capture=True)
        text_pages = {p for a, b, eng in runs if eng == "text" for p in range(a, b + 1)}
        icons = [i for i in json.loads(r.stdout[r.stdout.find("["):] or "[]")
                 if i["page"] in text_pages]
        if icons and free_mineru_gpu():
            labels = label_icons(icon_dir, icons, args.alt_text_ollama, args.alt_text_model)
            err(f"[route] alt text: {sum(1 for l in labels if l['label'])} of "
                f"{len(labels)} icon(s) labelled")
        if not args.keep_parts:
            for f in icon_dir.glob("*"):
                f.unlink()
            icon_dir.rmdir()

    # 4. convert each run. Two or more MinerU runs share one MinerU server for
    # this document (models load once), unless the caller already runs one
    # for its whole job (PDF2MD_MINERU_SERVER).
    import os
    import signal
    # a TERM (a job being stopped) unwinds through the finally below and
    # stops this document's server; a KILL leaves it to the server's own
    # idle timeout
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    own_session = None
    # PDF2MD_MINERU_SERVER=off: one-shot containers, as before (baselines)
    if sum(1 for _, _, e in runs if e == "mineru") >= 2 and not os.environ.get("PDF2MD_MINERU_SERVER"):
        own_session = subprocess.run([str(REPO / "tools" / "mineru-session"), "start"],
                                     check=True, capture_output=True, text=True).stdout.strip()
        os.environ["PDF2MD_MINERU_SERVER"] = own_session
        err(f"[route] MinerU server for this document: {own_session}")
    try:
        parts = convert_runs(runs, pdf, pc, workdir, stem, labels, args)
    finally:
        if own_session:
            subprocess.run([str(REPO / "tools" / "mineru-session"), "stop", own_session], check=False)
            del os.environ["PDF2MD_MINERU_SERVER"]

    # 5. merge
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
    # each run's engine appends its own document index with RUN-local page
    # numbers; with several runs those landed mid-document, numbered wrong
    # (reported by a caller). Drop them and index the merged document once.
    final = TITLE_INDEX_RE.sub("\n\n", "".join(merged)).rstrip("\n") + "\n"
    final += text_engine().format_title_index(text_engine().build_title_index(final))
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

    for c in covered.values():
        warnings.append({"kind": "covered_text_removed", "page": c["page"],
                         "covered_chars": c["covered_chars"],
                         "redaction_markers": c["redaction_markers"],
                         "detail": f"{c['covered_chars']} chars of text hidden under opaque "
                                   f"shapes were removed (cosmetic redaction or text under a "
                                   f"panel); {c['redaction_markers']} block(s) under dark "
                                   f"fills are marked [redacted] in the output"})

    # content-loss check: per page, how much of the text layer's wording the
    # output kept (engines/text/verify_text.py). Report-only, like
    # verify_numbers: a failure to check never fails the conversion.
    recall_by_page: dict[int, float | None] = {}
    try:
        r = docker_text(workdir, ["/usr/local/bin/verify_text.py",
                                  f"/work/{pdf.name}", f"/work/{out_md.name}", "--json"],
                        args.dev_bind, capture=True, entrypoint="python3")
        vt = parse_json_report(r.stdout)
        recall_by_page = {p["page"]: p["recall"] for p in vt["per_page"]}
        warnings.extend(vt["warnings"])
        for w in vt["warnings"]:
            err(f"[route] WARNING page {w['page']}: {w['detail']}")
    except Exception as e:
        err(f"[route] text-coverage check did not run: {e}")

    manifest = {
        "source": str(Path(args.input).name),
        "pdf_pages": pc,
        "engine_runs": [{"pages": [a, b], "engine": e} for a, b, e in runs],
        "per_page": [{**p, "engine": next(e for a, b, e in runs
                                          if a <= p["page"] <= b),
                      "table_rows_emitted": rows.get(p["page"], 0),
                      "output_chars": seg_chars.get(p["page"], 0),
                      "text_layer_recall": recall_by_page.get(p["page"]),
                      "covered_text_chars": covered.get(p["page"], {}).get("covered_chars", 0)}
                     for p in per_page],
        "ligature_fixes": prepared["ligature_fixes"],
        "icons": labels,
        "warnings": warnings,
    }
    man_path = out_md.with_suffix(".manifest.json")
    man_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    # 7. number-preservation check (unchanged from auto.sh, report-only)
    try:
        subprocess.run(["docker", "run", "--rm", "-v", f"{workdir}:/work",
                        *docker_user(), *DOCKER_HARDEN, "-e", "HOME=/tmp",
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
            # engines write provenance siblings per piece; remove those too
            Path(str(piece_md)[:-3] + ".content_list.json").unlink(missing_ok=True)
            piece_pdf = workdir / f"{stem}.p{a:04d}-{b:04d}.pdf"
            piece_pdf.unlink(missing_ok=True)
            (workdir / f"{stem}.p{a:04d}-{b:04d}.icon-labels.json").unlink(missing_ok=True)

    err(f"[route] wrote {out_md} + {man_path.name} "
        f"({len(warnings)} warning(s)) in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
