#!/usr/bin/env python3
"""
pdf2md - PDF to Markdown, GPU-accelerated for scanned docs.

Generic, single-purpose tool: bytes in (PDF), Markdown out. No domain logic.

Routing (automatic):
  - Digital PDF with a real text layer  -> pymupdf4llm  (fast, CPU, no model load)
  - Scanned / image-only PDF            -> Marker/Surya (GPU OCR, tables preserved)

The split matters: don't burn GPU OCR on a PDF that already has selectable text,
and don't feed a scanned image to a text extractor (you get nothing).

Table fidelity on dense, borderless scans is Marker's known weak spot (it can
merge columns and bleed values sideways into cells that should be nil). Two levers
target that, both exposed as flags below:
  --dpi        higher DPI gives the layout model cleaner column boundaries
  --use-llm    Marker's LLM table-refinement pass, run against a LOCAL Ollama model

Usage:
  pdf2md INPUT.pdf                              # markdown to stdout
  pdf2md INPUT.pdf -o OUT.md                    # markdown to file
  pdf2md INPUT.pdf --format json                # Marker structured JSON (forces OCR)
  pdf2md INPUT.pdf --engine marker              # force OCR even if a text layer exists
  pdf2md INPUT.pdf --engine text                # force fast path (no GPU)
  pdf2md INPUT.pdf --dpi 300                    # higher-res render for dense tables
  pdf2md INPUT.pdf --use-llm                    # LLM table refinement via local Ollama
  pdf2md INPUT.pdf --use-llm --ollama-model qwen2.5:7b
Routing decisions and timings go to STDERR, so STDOUT stays clean markdown.
"""

import argparse
import contextlib
import os
import sys
import time


def err(*a):
    print(*a, file=sys.stderr, flush=True)


@contextlib.contextmanager
def quiet_stdout():
    """Redirect C-level stdout (fd 1) to stderr during conversion, so MuPDF /
    Tesseract / Marker banners never contaminate the markdown we emit on stdout."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def classify(pdf_path, min_page_chars):
    """Return (page_count, image_only_pages, total_chars) using PyMuPDF."""
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    image_only, total_chars = [], 0
    for i in range(len(doc)):
        txt = doc[i].get_text("text")
        n = len(txt.strip())
        total_chars += n
        if n < min_page_chars:
            image_only.append(i + 1)
    pc = len(doc)
    doc.close()
    return pc, image_only, total_chars


def detect_and_fix_rotation(pdf_path, output_path, dpi, min_confidence, log):
    """Correct pages whose content is rotated but whose PDF /Rotate flag reads 0
    (or is otherwise wrong) — the exact defect that caused a whole comparative
    table to be silently dropped on a real fixture (MinerU's layout model loses
    the second of two stacked tables at a footnote seam specifically when the
    page is rotated; correcting orientation upstream fixed it).

    This is a PURE GEOMETRY fix: only the page's /Rotate flag is changed, via
    Tesseract's orientation-and-script-detection (OSD) mode. OSD detects the
    dominant text angle from stroke geometry alone — it does not read, recognise,
    or interpret content, so this cannot introduce the kind of silent content
    decision this project treats as unacceptable (see the nil-fabrication and
    entity-name-substitution defects this tool exists to avoid). No pixel is
    touched, no text is re-rendered; every downstream tool (MinerU, Marker,
    any PDF viewer) honours /Rotate identically.

    Tesseract's OSD 'rotate' field was found EMPIRICALLY to not map onto PDF's
    /Rotate direction consistently -- two genuinely-rotated pages on the same
    real fixture needed opposite corrections despite both being clearly rotated
    (confirmed visually). Rather than trust the field's sign, this tries BOTH
    candidate corrections and keeps whichever one a FRESH OSD pass confirms is
    upright (rotate==0 on recheck). If neither candidate verifies, the page is
    left untouched and flagged loudly for manual review -- never guessed.

    Returns (fixed, unresolved):
      fixed      -- list of (page_num_1indexed, degrees_applied)
      unresolved -- list of (page_num_1indexed, osd_rotate, confidence) where a
                    rotation was suspected but could not be verified
    """
    import io
    import fitz
    import pytesseract
    from PIL import Image

    def osd_of(page):
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            return pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractError:
            return None  # no text detected (blank/logo-only page) -- nothing to orient against

    doc = fitz.open(pdf_path)
    fixed, unresolved = [], []
    for i in range(len(doc)):
        page = doc[i]
        base_rotation = page.rotation
        result = osd_of(page)
        if result is None:
            continue
        rotate = result.get("rotate", 0) or 0
        conf = result.get("orientation_conf", 0) or 0
        if not rotate or conf < min_confidence:
            continue

        candidates = sorted({(base_rotation + rotate) % 360,
                              (base_rotation - rotate) % 360})
        chosen = None
        for cand in candidates:
            page.set_rotation(cand)
            recheck = osd_of(page)
            if recheck is not None and (recheck.get("rotate", 0) or 0) == 0:
                chosen = cand
                break
        if chosen is not None:
            page.set_rotation(chosen)
            fixed.append((i + 1, chosen))
            log(f"[pdf2md] --derotate: page {i+1} corrected to /Rotate={chosen} "
                f"(verified upright by a fresh OSD pass)")
        else:
            page.set_rotation(base_rotation)
            unresolved.append((i + 1, rotate, conf))
            log(f"[pdf2md] --derotate: WARNING page {i+1} looks rotated "
                f"(OSD rotate={rotate}°, confidence {conf:.1f}) but no candidate "
                f"correction verified upright -- left unmodified, review manually")
    doc.save(output_path)
    doc.close()
    return fixed, unresolved


def ollama_reachable(base_url, model):
    """Preflight for --use-llm: confirm the Ollama server answers AND has the model.
    Returns (ok: bool, message: str). Cheap; avoids a long silent no-op run where
    Marker falls back per-table on every LLM call and still exits 0."""
    import json
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=4) as r:
            tags = json.loads(r.read().decode())
    except Exception as e:
        return False, f"cannot reach Ollama at {base_url} ({e})"
    names = [m.get("name", "") for m in tags.get("models", [])]
    # accept exact or ':latest'-stripped match
    have = any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
    if not have:
        return False, (f"Ollama is up but model '{model}' not found. "
                       f"Available: {names or 'none'}. Run: ollama pull {model}")
    return True, f"Ollama OK at {base_url}, model '{model}' present"


def to_markdown_text(pdf_path):
    """Fast path: digital PDF -> markdown via pymupdf4llm (CPU, no model load)."""
    import pymupdf4llm
    return pymupdf4llm.to_markdown(pdf_path)


# Structure-only rewrite instruction: let the LLM fix table LAYOUT but forbid it
# from changing any printed VALUE. Targets both failure modes seen on scanned
# financial reports: (1) fabricated numbers in cells that were nil, and (2)
# silent "correction" of genuine source inconsistencies (e.g. two figures
# for the same line item differing by a rounding increment across tables).
PRESERVE_VALUES_PROMPT = (
    "You are given an image of a table and its current text representation. "
    "Correct the STRUCTURE of the table so rows, columns, and cell alignment "
    "match the image exactly. Follow these rules strictly:\n"
    "1. Reproduce every printed value EXACTLY as it appears in the image. Do not "
    "correct, round, reformat, or reconcile any number or word, even if it looks "
    "like a typo or seems inconsistent with another cell.\n"
    "2. If a cell is blank or shows only a dash '-', output an EMPTY cell. Never "
    "infer, guess, or carry a value across from an adjacent cell to fill a blank.\n"
    "3. You MAY fix: column/row alignment, splitting merged cells into the correct "
    "columns, and clearly non-numeric OCR garble that is obviously structural noise.\n"
    "4. You MAY NOT fix: apparent inconsistencies between values, numbers that look "
    "wrong, or the spelling of names. Preserve them exactly.\n"
    "Return only the corrected table."
)


def to_markdown_marker(pdf_path, as_json, dpi, use_llm, ollama_model, ollama_url,
                       max_output_tokens, max_table_rows, max_table_iterations,
                       html_tables, preserve_values):
    """OCR path: scanned PDF -> markdown (or structured JSON) via Marker/Surya.

    dpi                 : DPI of the high-res render (cleaner column segmentation).
    use_llm             : LLM refinement pass, via a LOCAL Ollama vision model.
    max_output_tokens   : LLM output budget; raise to avoid truncated (unterminated)
                          JSON on big dense tables.
    max_table_rows      : tables with more rows than this are SKIPPED by the LLM; raise
                          so large financial tables aren't silently dropped.
    max_table_iterations: retries for a table the LLM rewrite fails/ truncates on.
    html_tables         : emit tables as HTML (explicit cell boundaries -> better for a
                          downstream extractor, distinguishes blank cells from values).
    preserve_values     : install the structure-only rewrite prompt (no value edits).
    """
    from marker.models import create_model_dict
    from marker.converters.pdf import PdfConverter
    from marker.config.parser import ConfigParser

    config = {
        "output_format": "json" if as_json else "markdown",
        "highres_image_dpi": dpi,          # render DPI for OCR/layout/table detection
    }
    if html_tables and not as_json:
        config["html_tables_in_markdown"] = True   # explicit cell structure in the md
    if use_llm:
        # Use Marker's OpenAIService pointed at Ollama's OpenAI-compatible /v1
        # endpoint. Marker's native OllamaService is fragile (it assumes response
        # fields like prompt_eval_count that Ollama doesn't always return, causing
        # per-table KeyErrors). The OpenAI chat-completions path is far more stable.
        config["use_llm"] = True
        config["llm_service"] = "marker.services.openai.OpenAIService"
        config["openai_base_url"] = ollama_url.rstrip("/") + "/v1"
        config["openai_model"] = ollama_model
        config["openai_api_key"] = "ollama"          # required by the client, ignored by Ollama
        config["openai_image_format"] = "png"        # help says png is more compatible than webp
        # --- table-fidelity knobs (LLMTableProcessor + service) ---
        config["max_output_tokens"] = max_output_tokens      # avoid truncated JSON on big tables
        config["max_table_rows"] = max_table_rows            # don't silently skip large tables
        config["max_table_iterations"] = max_table_iterations
        if preserve_values:
            config["table_rewriting_prompt"] = PRESERVE_VALUES_PROMPT

    cp = ConfigParser(config)
    converter = PdfConverter(
        config=cp.generate_config_dict(),
        artifact_dict=create_model_dict(),
        processor_list=cp.get_processors(),
        renderer=cp.get_renderer(),
        llm_service=cp.get_llm_service() if use_llm else None,
    )
    rendered = converter(pdf_path)

    if as_json:
        return rendered.model_dump_json(indent=2)
    from marker.output import text_from_rendered
    text, _, _ = text_from_rendered(rendered)
    return text


def main():
    ap = argparse.ArgumentParser(prog="pdf2md", description="PDF to Markdown (GPU OCR for scans).")
    ap.add_argument("input", help="input PDF path")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--format", choices=["md", "json"], default="md",
                    help="md (default) or json (Marker structured; forces OCR engine)")
    ap.add_argument("--engine", choices=["auto", "marker", "text"], default="auto",
                    help="auto routes by text-layer presence; marker=force OCR; text=force fast path")
    ap.add_argument("--image-threshold", type=float, default=0.2,
                    help="fraction of image-only pages above which to OCR (default 0.2)")
    ap.add_argument("--min-page-chars", type=int, default=20,
                    help="a page with fewer stripped chars counts as image-only (default 20)")
    ap.add_argument("--classify-only", action="store_true",
                    help="print 'digital' or 'scan' to stdout and exit (no model load); "
                         "used by pdf2md-auto.sh to route between the text/marker/mineru engines")
    ap.add_argument("--derotate", metavar="OUTPUT.pdf",
                    help="detect per-page rotation via Tesseract OSD and write a corrected copy "
                         "to OUTPUT.pdf, then exit. Only the /Rotate flag is changed -- no pixel "
                         "or content is altered. Used by pdf2md-auto.sh ahead of every conversion.")
    ap.add_argument("--rotate-dpi", type=int, default=150,
                    help="render DPI used for --derotate's OSD pass (default 150)")
    ap.add_argument("--rotate-min-confidence", type=float, default=1.0,
                    help="minimum OSD confidence required to apply a rotation correction (default 1.0)")
    # --- table-fidelity levers (Marker engine only) ---
    ap.add_argument("--dpi", type=int, default=192,
                    help="render DPI for the OCR/layout model; raise (e.g. 300) for cleaner "
                         "column segmentation on dense borderless tables (default 192)")
    ap.add_argument("--use-llm", action="store_true",
                    help="run Marker's LLM table-refinement pass (needs a local Ollama model)")
    ap.add_argument("--ollama-model", default="qwen2.5:7b",
                    help="Ollama model name for --use-llm (default qwen2.5:7b)")
    ap.add_argument("--ollama-url", default="http://host.docker.internal:11434",
                    help="Ollama base URL; host.docker.internal reaches Ollama on the host from "
                         "inside the container (default http://host.docker.internal:11434)")
    # --- table-fidelity knobs (Marker LLM pass) ---
    ap.add_argument("--llm-max-tokens", type=int, default=8192,
                    help="LLM output token budget per table; raise if you see 'Unterminated "
                         "string' truncations on big tables (default 8192)")
    ap.add_argument("--max-table-rows", type=int, default=200,
                    help="tables larger than this are SKIPPED by the LLM; raised so big "
                         "financial tables aren't silently dropped (default 200)")
    ap.add_argument("--max-table-iterations", type=int, default=3,
                    help="retries for a table the LLM fails/truncates on (default 3)")
    ap.add_argument("--html-tables", action="store_true",
                    help="emit tables as HTML (explicit cell boundaries; better for a "
                         "downstream LLM extractor, and distinguishes blank cells from values)")
    ap.add_argument("--preserve-values", action="store_true",
                    help="install a structure-only rewrite prompt: LLM may fix table layout "
                         "but must reproduce every printed value verbatim and keep nil cells empty")
    ap.add_argument("--quiet", action="store_true", help="suppress stderr routing logs")
    args = ap.parse_args()

    def log(*a):
        if not args.quiet:
            err(*a)

    t0 = time.time()

    if args.derotate:
        try:
            fixed, unresolved = detect_and_fix_rotation(
                args.input, args.derotate, args.rotate_dpi, args.rotate_min_confidence, log
            )
        except Exception as e:
            err(f"[pdf2md] ERROR during --derotate: {e}")
            sys.exit(6)
        if fixed:
            log(f"[pdf2md] --derotate: corrected {len(fixed)} page(s) in {time.time()-t0:.1f}s "
                f"-> {args.derotate}")
        else:
            log(f"[pdf2md] --derotate: no rotated pages corrected ({time.time()-t0:.1f}s) "
                f"-> {args.derotate}")
        if unresolved:
            err(f"[pdf2md] --derotate: {len(unresolved)} page(s) flagged rotated but UNRESOLVED "
                f"-- see WARNING lines above, review manually: "
                f"{[p for p, _, _ in unresolved]}")
        return

    try:
        pc, image_only, total_chars = classify(args.input, args.min_page_chars)
    except Exception as e:
        err(f"[pdf2md] ERROR opening/classifying PDF: {e}")
        sys.exit(2)

    ratio = (len(image_only) / pc) if pc else 1.0
    log(f"[pdf2md] {args.input}: {pc} pages, {len(image_only)} image-only "
        f"({ratio:.0%}), {total_chars} text chars")

    if args.classify_only:
        print("scan" if ratio > args.image_threshold else "digital")
        return

    # ---- decide engine ----
    engine = args.engine
    if args.format == "json" and engine != "marker":
        log("[pdf2md] --format json requires the Marker engine; switching engine=marker.")
        engine = "marker"
    if engine == "auto":
        engine = "marker" if ratio > args.image_threshold else "text"
    log(f"[pdf2md] engine: {engine}")

    # ---- run ----
    try:
        if engine == "text":
            if args.use_llm:
                log("[pdf2md] note: --use-llm only applies to the Marker engine; ignoring on text path.")
            with quiet_stdout():
                out = to_markdown_text(args.input)
            # Guard: if the 'digital' PDF actually yielded almost nothing, it was
            # really a scan -> tell the user rather than emit empty markdown.
            if len(out.strip()) < args.min_page_chars * max(pc, 1) * 0.2:
                err(f"[pdf2md] WARNING: text engine produced very little output "
                    f"({len(out.strip())} chars). This PDF looks scanned; "
                    f"re-run with --engine marker for OCR.")
        else:
            if args.use_llm:
                ok, msg = ollama_reachable(args.ollama_url, args.ollama_model)
                if not ok:
                    err(f"[pdf2md] ERROR: --use-llm requested but {msg}")
                    err("[pdf2md] Start Ollama on the host (ollama serve) and ensure the "
                        "container can reach it (pdf2md.sh passes --add-host=host-gateway), "
                        "or drop --use-llm.")
                    sys.exit(5)
                log(f"[pdf2md] {msg}")
            extras = f"dpi={args.dpi}" + (f", use_llm via ollama:{args.ollama_model}" if args.use_llm else "")
            log(f"[pdf2md] loading Marker models ({extras}; first run downloads weights to /models)...")
            with quiet_stdout():
                out = to_markdown_marker(
                    args.input,
                    as_json=(args.format == "json"),
                    dpi=args.dpi,
                    use_llm=args.use_llm,
                    ollama_model=args.ollama_model,
                    ollama_url=args.ollama_url,
                    max_output_tokens=args.llm_max_tokens,
                    max_table_rows=args.max_table_rows,
                    max_table_iterations=args.max_table_iterations,
                    html_tables=args.html_tables,
                    preserve_values=args.preserve_values,
                )
    except ImportError as e:
        err(f"[pdf2md] ERROR: engine '{engine}' unavailable: {e}")
        sys.exit(3)
    except Exception as e:
        err(f"[pdf2md] ERROR during conversion: {e}")
        sys.exit(4)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        log(f"[pdf2md] wrote {args.output} ({len(out)} chars) in {time.time()-t0:.1f}s")
    else:
        sys.stdout.write(out)
        log(f"[pdf2md] done in {time.time()-t0:.1f}s ({len(out)} chars)")


if __name__ == "__main__":
    main()