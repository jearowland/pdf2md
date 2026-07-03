#!/usr/bin/env python3
"""
mineru2md - thin wrapper around MinerU's CLI so it behaves like pdf2md:
PDF in, one clean markdown file (or stdout) out.

MinerU writes a whole output *directory* tree (markdown + images + JSON) under
-o. This wrapper runs `mineru`, finds the produced .md, and emits that -- PLUS,
when -o is used:
  - a <stem>.content_list.json sibling file: MinerU's own layout model already
    computes page_idx and bbox for every text/table block (its own internal
    representation), previously silently discarded along with the rest of the
    temp output directory. It's the provenance data a downstream reviewer
    needs to render a highlighted excerpt of a specific extracted value
    without re-searching the whole document.
  - an images/ sibling directory: the markdown MinerU produces contains
    ![](images/<hash>.jpg) references -- signatures, logos, and potentially
    figures/charts MinerU chose to keep as images rather than extract as
    text/tables -- that used to point at files nowhere on disk, because the
    directory holding them was discarded too. A broken reference for a chart
    would be silent data loss, not just a cosmetic gap.
  - inline page_marker() lines and an appended title index, matching
    engines/text/pdf2md.py's text engine exactly (same marker format, same
    silent-HTML-comment title index) -- built here by locating each
    content_list.json block's own text in the markdown (searching forward
    only, so a repeated string resolves to the right occurrence in document
    order), since MinerU's raw output has no page structure of its own the
    way pymupdf4llm's page_chunks does. See insert_page_markers().

Also runs an automated spelling-reconciliation pass (on by default): hybrid-
engine's VLM stage was found to silently "correct" an unusual, repeated proper
noun toward a common English word, inconsistently, within the same document
(e.g. "Reliabilty" -> "Reliability" in running text, but left correct in
structured contexts). MinerU's plain `pipeline` backend does NOT exhibit this
(no VLM stage) but regresses on table structure, so it can't just replace
hybrid-engine outright. Instead: run pipeline as a cheap reference-only pass,
and wherever a repeated rare token in the primary output has a same-document
real-word "twin" that pipeline never produces, treat that as confirmation and
substitute -- a deterministic, evidence-based reconciliation between two
already-computed passes, not a model decision. See reconcile_spelling().

Usage:
  mineru2md INPUT.pdf                         # markdown to stdout
  mineru2md INPUT.pdf -o OUT.md               # markdown to file
  mineru2md INPUT.pdf --backend vlm-engine    # override backend
  mineru2md INPUT.pdf --method auto           # override parse method
  mineru2md INPUT.pdf --no-reconcile-spelling # skip the reference pass (faster)
Routing/timing logs go to STDERR; STDOUT stays clean markdown.
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
import time


def err(*a):
    print(*a, file=sys.stderr, flush=True)


# --- page marker / title index -----------------------------------------
# Duplicated from engines/text/pdf2md.py rather than shared via a common
# module: these are small, stable, and self-contained (no pymupdf/mineru-
# specific state), and pdf2md-text / pdf2md-mineru are separate Docker
# build contexts today, so importing across them would mean restructuring
# both Dockerfiles' COPY paths for a handful of functions. Keep these two
# copies in sync if either changes.

PAGE_MARKER_RE = re.compile(r"<!-- page (\d+) -->")


def page_marker(page_number):
    """See pdf2md.py's page_marker() for the full rationale: an HTML
    comment renders as nothing at all in any markdown viewer -- the closest
    text equivalent to a PDF's own page boundary, which is just blank
    space, nothing announced -- while still being a single, greppable line
    in the raw text for downstream tooling."""
    return f"<!-- page {page_number} -->"


TITLE_INDEX_START = "<!-- pdf2md document index"
RUNNING_HEADER_MIN_REPEATS = 3
MIN_NEEDLE_LEN = 10


def build_title_index(markdown_with_markers):
    """See pdf2md.py's build_title_index() for the full rationale
    (running-header suppression: a banner is always the FIRST heading on
    its page, a real section heading essentially never is)."""
    raw = []
    current_page = 1
    first_heading_seen_this_page = False
    for line in markdown_with_markers.split("\n"):
        stripped = line.strip()
        m = PAGE_MARKER_RE.match(stripped)
        if m:
            current_page = int(m.group(1))
            first_heading_seen_this_page = False
            continue
        h = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if h:
            text = re.sub(r"\*+", "", h.group(2)).strip()
            if text:
                is_first_on_page = not first_heading_seen_this_page
                first_heading_seen_this_page = True
                raw.append((len(h.group(1)), text, current_page, is_first_on_page))

    first_of_page_counts = {}
    for _, text, _, is_first in raw:
        if is_first:
            first_of_page_counts[text] = first_of_page_counts.get(text, 0) + 1
    banners = {text for text, count in first_of_page_counts.items()
               if count >= RUNNING_HEADER_MIN_REPEATS}

    return [(level, text, page) for level, text, page, _ in raw if text not in banners]


def format_title_index(index):
    """See pdf2md.py's format_title_index() -- one silent HTML comment
    block, appended at the very end, never rendered."""
    if not index:
        return ""
    lines = [TITLE_INDEX_START]
    for level, text, page in index:
        indent = "  " * (level - 1)
        lines.append(f"{indent}- {text} (page {page})")
    lines.append("-->")
    return "\n\n" + "\n".join(lines) + "\n"


def insert_page_markers(md, content_list_json, log):
    """Insert page_marker() lines into MinerU's markdown at page-boundary
    transitions. MinerU's own .md has no page structure at all (unlike
    pymupdf4llm, which gives whole per-page chunks to concatenate) -- what
    it DOES have is content_list.json, an ordered list of blocks each
    carrying page_idx (0-indexed, confirmed empirically) and their own
    text/table_body. This locates each block's text in the markdown by
    searching FORWARD from where the previous block was found, never
    backward -- so a string that legitimately repeats (a running header, a
    common phrase) resolves to the correct occurrence in document order,
    not always the first. MinerU's own markdown text is never altered
    otherwise, only marker lines inserted between matched positions; a
    block whose text can't be found (e.g. reformatted between the block
    record and final rendering) is silently skipped rather than guessed at
    -- a missed marker is a smaller failure than a wrong one.

    Boilerplate blocks (a running header/footer whose exact text repeats
    across 3+ blocks -- a document title reprinted on every page, "Page N
    of 14", a firm name in a footer) are excluded from anchoring entirely,
    not just deprioritized. Confirmed on a real document: content_list.json
    lists such a block once per page in page_idx order, but MinerU's final
    markdown assembly can group a multi-page table's rows together and
    push all the repeated headers/footers after them -- so the repeated
    text's NEXT forward occurrence often belongs to a much later page, not
    the one it's nominally paired with. Matching it anyway drags the
    cursor far ahead and skips right past genuine, unique content from the
    pages in between (confirmed: a table block's content sitting only a
    few hundred chars after the previous match was skipped because an
    intervening running-header match had already jumped the cursor over
    13,000 chars forward). Only each page's distinctive content is
    trustworthy as a transition anchor."""
    import json
    from collections import Counter
    try:
        blocks = json.loads(content_list_json)
    except (json.JSONDecodeError, TypeError):
        log("[mineru2md] WARNING: content_list.json unreadable -- no page markers inserted")
        return md, 0

    def block_key(block):
        text = (block.get("text") or block.get("table_body") or "").strip()
        return text

    counts = Counter(block_key(b) for b in blocks)
    boilerplate = {text for text, n in counts.items() if text and n >= RUNNING_HEADER_MIN_REPEATS}
    n_boilerplate_blocks = sum(1 for b in blocks if block_key(b) in boilerplate)
    if n_boilerplate_blocks:
        log(f"[mineru2md] excluding {n_boilerplate_blocks} boilerplate block(s) "
            f"({len(boilerplate)} distinct repeated text(s)) from marker anchoring")

    out = []
    cursor = 0
    last_pos = 0
    current_page_idx = None
    n_inserted = 0
    for block in blocks:
        page_idx = block.get("page_idx")
        if page_idx is None:
            continue
        if block_key(block) in boilerplate:
            continue
        snippet = (block.get("text") or "").strip()
        if not snippet:
            # table_body is the SAME raw HTML MinerU writes into the final
            # markdown verbatim (confirmed byte-for-byte on a real block) --
            # search with it as-is, not tag-stripped. An earlier version of
            # this function stripped tags before searching, which meant
            # every table block's needle could never match the actual
            # HTML-tagged text in the markdown at all -- confirmed the real
            # cause of a 7/13 (54%) marker-insertion rate on a real
            # document with 11 table blocks out of ~15 total.
            snippet = (block.get("table_body") or "").strip()
            # Tables need a longer needle than plain text: several small
            # NFP filings reuse an identical column-header row ("NOTES /
            # 30 June 2025 / 30 June 2024") across many different notes'
            # tables, so the first ~40 chars of table_body is often just
            # that repeated header, not anything distinctive to THIS
            # table -- confirmed the real cause of 4 consecutive missed
            # page transitions on a real document. 40 chars is plenty for
            # ordinary text blocks (a heading, a paragraph opening), so
            # only tables get the wider needle.
            needle = snippet[:120]
        else:
            needle = snippet[:40]
        if len(needle) < MIN_NEEDLE_LEN:
            # A short fragment (e.g. a 3-character "CLM" split into its own
            # block by layout detection, part of a letterhead) is too
            # common a substring to be a reliable anchor -- confirmed on a
            # real document: a 3-char needle coincidentally matched inside
            # unrelated text 6,500+ chars ahead of the cursor, dragging it
            # past an entire page's worth of genuine content (the actual
            # target text) in one leap. Skipping short needles entirely is
            # safer than accepting a plausible-looking match at the wrong
            # occurrence.
            continue
        pos = md.find(needle, cursor)
        if pos == -1:
            continue
        if current_page_idx is None:
            current_page_idx = page_idx
        elif page_idx != current_page_idx:
            out.append(md[last_pos:pos])
            out.append("\n\n" + page_marker(page_idx + 1) + "\n\n")
            last_pos = pos
            n_inserted += 1
            current_page_idx = page_idx
        cursor = pos + len(needle)
    out.append(md[last_pos:])

    max_page_idx = max((b.get("page_idx", 0) for b in blocks), default=0)
    log(f"[mineru2md] inserted {n_inserted} page marker(s) (document has {max_page_idx + 1} page(s) "
        f"-- a marker count noticeably lower than pages-1 means many blocks' text couldn't be "
        f"matched, worth checking rather than assuming full coverage)")
    return "".join(out), n_inserted


def run_mineru(input_path, method, backend, lang, log, want_content_list=False, want_images=False):
    """Run MinerU once with the given backend, return
    (markdown, content_list_json_or_None, images_dict_or_None).

    MinerU's own layout model already computes exactly what a downstream
    reader needs for provenance -- page_idx and bbox per text/table block,
    written to <stem>_content_list.json alongside the markdown -- but this
    wrapper used to glob for *.md only and let the whole output directory
    (a TemporaryDirectory) get deleted, discarding it. want_content_list=True
    reads it before that happens, so a caller can keep it as a sibling
    artifact for downstream tooling (e.g. rendering a highlighted excerpt of
    a specific extracted value for human review) without re-deriving
    position data pymupdf4llm's page_separators can't provide (page
    boundaries only, not per-block bounding boxes).

    want_images=True reads every file MinerU wrote to its images/ directory
    (always a sibling of the chosen .md, confirmed from MinerU's own
    prepare_env()) into memory before the temp directory is cleaned up.
    Confirmed a real bug this was fixing: the markdown MinerU produces
    contains ![](images/<hash>.jpg) references -- signatures, logos, and
    potentially figures/charts -- that pointed at files nowhere on disk,
    because this wrapper discarded the whole temp directory including them.
    A broken image reference for a chart would be silent data loss, not
    just a cosmetic gap."""
    t0 = time.time()
    with tempfile.TemporaryDirectory() as outdir:
        cmd = ["mineru", "-p", input_path, "-o", outdir, "-m", method, "-b", backend]
        if lang:
            cmd += ["-l", lang]
        log(f"[mineru2md] running: {' '.join(cmd)}")

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            err(f"[mineru2md] ERROR: mineru ({backend}) exited {proc.returncode}")
            err(proc.stderr[-2000:] if proc.stderr else "(no stderr)")
            sys.exit(4)

        stem = os.path.splitext(os.path.basename(input_path))[0]
        candidates = glob.glob(os.path.join(outdir, "**", "*.md"), recursive=True)
        if not candidates:
            err(f"[mineru2md] ERROR: mineru ({backend}) produced no .md under {outdir}")
            err("[mineru2md] tree: " + ", ".join(
                os.path.relpath(p, outdir) for p in
                glob.glob(os.path.join(outdir, "**", "*"), recursive=True)[:40]))
            sys.exit(5)

        exact = [c for c in candidates if os.path.splitext(os.path.basename(c))[0] == stem]
        chosen = exact[0] if exact else sorted(candidates, key=lambda p: os.path.getsize(p))[-1]
        with open(chosen, "r", encoding="utf-8") as f:
            md = f.read()
        log(f"[mineru2md] {backend}: picked {os.path.relpath(chosen, outdir)} "
            f"({len(md)} chars) in {time.time()-t0:.1f}s")

        content_list_json = None
        if want_content_list:
            cl_path = os.path.join(os.path.dirname(chosen), f"{stem}_content_list.json")
            if os.path.isfile(cl_path):
                with open(cl_path, "r", encoding="utf-8") as f:
                    content_list_json = f.read()
                log(f"[mineru2md] preserving {os.path.basename(cl_path)} "
                    f"(page_idx + bbox per block, for provenance)")
            else:
                log(f"[mineru2md] WARNING: expected {os.path.basename(cl_path)} not found -- "
                    f"no provenance data will be saved for this document")

        images = None
        if want_images:
            images_dir = os.path.join(os.path.dirname(chosen), "images")
            if os.path.isdir(images_dir):
                images = {}
                for fname in os.listdir(images_dir):
                    fpath = os.path.join(images_dir, fname)
                    if os.path.isfile(fpath):
                        with open(fpath, "rb") as f:
                            images[fname] = f.read()
                if images:
                    log(f"[mineru2md] preserving {len(images)} image(s) referenced by the markdown "
                        f"(signatures, logos, and any figures/charts MinerU kept as images rather "
                        f"than extracting as text/tables)")
        return md, content_list_json, images


_WORD_RE = re.compile(r"[A-Za-z]{5,}")


def _levenshtein(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def _load_wordlist():
    for path in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return {w.strip().lower() for w in f if w.strip()}
    return None


def reconcile_spelling(primary_md, reference_md, log):
    """Deterministically reconcile rare-token spelling drift between two MinerU
    passes of the SAME document. Never invents or infers a spelling -- only acts
    when BOTH candidate spellings already appear in the primary pass's own output
    (repeated, so not one-off OCR noise) AND the reference pass gives an
    unambiguous, uniform preference for one of them. Returns (corrected_md, log_lines).
    """
    wordlist = _load_wordlist()
    if wordlist is None:
        log("[mineru2md] reconcile-spelling: no wordlist available, skipping")
        return primary_md, []

    from collections import Counter
    primary_counts = Counter(_WORD_RE.findall(primary_md))
    reference_counts = Counter(_WORD_RE.findall(reference_md))

    # Candidates: tokens repeated (>=2) in the primary pass that are NOT common
    # English words -- i.e. plausible unusual proper nouns, not OCR one-offs.
    rare = {t: c for t, c in primary_counts.items()
            if c >= 2 and t.lower() not in wordlist}
    # Their potential "corrected toward a real word" twins: also repeated (>=2),
    # ARE common English words, close edit distance, similar length.
    dictish = {t: c for t, c in primary_counts.items()
               if c >= 2 and t.lower() in wordlist}

    changes = []
    substituted_md = primary_md
    for rare_tok, rare_n in rare.items():
        for dict_tok, dict_n in dictish.items():
            # Require SAME length: OCR/VLM misreads a glyph as a different but
            # similar-shaped one (a<->i, rn<->m) without changing the character
            # count. A length change is almost always a genuinely different word
            # (plural, verb tense, added suffix -- e.g. "Expense"/"expensed" are
            # NOT a corruption pair, just two real, different, correct words) and
            # must never be treated as a spelling-correction candidate.
            if len(rare_tok) != len(dict_tok):
                continue
            if _levenshtein(rare_tok.lower(), dict_tok.lower()) > 2:
                continue
            # Confirmation: does the reference (non-LM) pass show a clear,
            # near-exclusive preference for the rare spelling? If reference never
            # produces the dictionary-word form at all, or overwhelmingly favours
            # the rare form, that's evidence rare_tok is the literal reading.
            ref_rare = reference_counts.get(rare_tok, 0)
            ref_dict = reference_counts.get(dict_tok, 0)
            if ref_rare > 0 and ref_dict == 0:
                pattern = re.compile(r"\b" + re.escape(dict_tok) + r"\b")
                n_replaced = len(pattern.findall(substituted_md))
                if n_replaced:
                    substituted_md = pattern.sub(rare_tok, substituted_md)
                    changes.append(
                        f"[mineru2md] reconcile-spelling: '{dict_tok}' ({dict_n}x) -> "
                        f"'{rare_tok}' ({n_replaced} instance(s)) -- reference pass confirms "
                        f"'{rare_tok}' ({ref_rare}x), never produces '{dict_tok}'"
                    )

    for line in changes:
        log(line)
    if not changes:
        log("[mineru2md] reconcile-spelling: no confirmed corrections")
    return substituted_md, changes


def main():
    ap = argparse.ArgumentParser(prog="mineru2md", description="MinerU PDF->Markdown wrapper.")
    ap.add_argument("input", help="input PDF path")
    ap.add_argument("-o", "--output", help="output markdown file (default: stdout)")
    ap.add_argument("--backend", default="hybrid-engine",
                    help="MinerU backend: pipeline | vlm-engine | hybrid-engine (default) | "
                         "vlm-http-client | hybrid-http-client")
    ap.add_argument("--method", default="ocr", choices=["auto", "txt", "ocr"],
                    help="parse method: ocr (default, right for scans) | auto | txt")
    ap.add_argument("--lang", default=None,
                    help="optional language hint to improve OCR (pipeline backend only)")
    ap.add_argument("--reconcile-spelling", dest="reconcile", action="store_true", default=True,
                    help="run a cheap 'pipeline' reference pass and deterministically fix "
                         "rare-token spelling drift (default: on)")
    ap.add_argument("--no-reconcile-spelling", dest="reconcile", action="store_false",
                    help="skip the reference pass (faster, but hybrid-engine's occasional "
                         "real-word substitution of unusual proper nouns won't be corrected)")
    ap.add_argument("--reconcile-backend", default="pipeline",
                    help="backend used for the reference pass (default: pipeline)")
    ap.add_argument("--quiet", action="store_true", help="suppress stderr logs")
    args = ap.parse_args()

    def log(*a):
        if not args.quiet:
            err(*a)

    if not os.path.isfile(args.input):
        err(f"[mineru2md] no such file: {args.input}")
        sys.exit(2)

    t0 = time.time()
    log("[mineru2md] (first run lazy-downloads MinerU models to the mounted cache)")
    md, content_list_json, images = run_mineru(args.input, args.method, args.backend, args.lang, log,
                                                want_content_list=True, want_images=True)

    if args.reconcile and args.backend != args.reconcile_backend:
        ref_md, _, _ = run_mineru(args.input, args.method, args.reconcile_backend, args.lang, log)
        md, _ = reconcile_spelling(md, ref_md, log)
        # content_list_json's page_idx/bbox geometry is unaffected by spelling
        # reconciliation (word-for-word text substitution only, layout untouched)
        # -- still valid to keep from the primary pass, not the reference pass.

    if content_list_json:
        md, _ = insert_page_markers(md, content_list_json, log)
        index = build_title_index(md)
        md += format_title_index(index)
    else:
        log("[mineru2md] no content_list.json available -- skipping page markers and title index")

    log(f"[mineru2md] total {time.time()-t0:.1f}s ({len(md)} chars)")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        log(f"[mineru2md] wrote {args.output}")
        if content_list_json:
            cl_out = os.path.splitext(args.output)[0] + ".content_list.json"
            with open(cl_out, "w", encoding="utf-8") as f:
                f.write(content_list_json)
            log(f"[mineru2md] wrote {cl_out} (page_idx + bbox per block, for provenance)")
        if images:
            # Per-document subfolder (images/<stem>/), not one shared flat
            # images/ folder -- confirmed a real usability problem in a
            # drop-and-forget watch.sh workflow: content-hash filenames avoid
            # NAME collisions across documents, but a shared folder still
            # mixes every scanned document's signatures/logos/figures
            # together with no way to tell which image belongs to which
            # report. The markdown's own ![](images/<hash>.jpg) references
            # (baked in by MinerU itself) are rewritten to match.
            out_stem = os.path.splitext(os.path.basename(args.output))[0]
            images_out_dir = os.path.join(os.path.dirname(os.path.abspath(args.output)), "images", out_stem)
            os.makedirs(images_out_dir, exist_ok=True)
            for fname, data in images.items():
                with open(os.path.join(images_out_dir, fname), "wb") as f:
                    f.write(data)
            md = md.replace("](images/", f"](images/{out_stem}/")
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(md)
            log(f"[mineru2md] wrote {len(images)} image(s) to {images_out_dir} "
                f"(content-hash filenames within this document's own subfolder)")
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
