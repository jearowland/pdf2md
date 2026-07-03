#!/usr/bin/env python3
"""
pdf2md - PDF to Markdown for DIGITAL PDFs (real text layer). Fast, CPU, no
model load. One of two engines behind pdf2md-auto.sh; the other is MinerU
(../mineru/), used for scanned/image-only PDFs.

Generic, single-purpose tool: bytes in (PDF), Markdown out. No domain logic.

This container also hosts two small, engine-agnostic preprocessing utilities
used by pdf2md-auto.sh ahead of EITHER engine:
  --classify-only   is this PDF digital or scanned? (routes to text vs mineru)
  --derotate        per-page rotation check/fix via Tesseract OSD

Usage:
  pdf2md INPUT.pdf                    # markdown to stdout
  pdf2md INPUT.pdf -o OUT.md          # markdown to file
  pdf2md INPUT.pdf --classify-only    # print 'digital' or 'scan', exit
  pdf2md INPUT.pdf --derotate OUT.pdf # write a rotation-corrected copy, exit
Routing decisions and timings go to STDERR, so STDOUT stays clean markdown.
"""

import argparse
import contextlib
import os
import re
import sys
import time

# Matches a font subsetted without a proper ToUnicode CMap: PDF viewers show
# real glyphs (readable), but any programmatic text extraction -- pymupdf
# included, confirmed empirically, this isn't a poppler/pdfplumber-only
# quirk -- gets raw low-range control-code bytes instead of real characters.
# Confirmed on a real corpus: 69 of 1,719 "digital"-classified documents
# (~4%) hit this. It's invisible to a character-COUNT check (there's
# plenty of "text", it's just undecodable) -- classify() previously only
# checked how much text a page had, never whether it was real. A page like
# this would score "digital", route to this fast text-only engine, and
# silently produce garbled or wrong markdown -- the same silent-wrong-data
# failure mode this project exists to prevent, just via a trigger nobody
# had seen yet. Clean documents measured at exactly 0.0 by this check;
# affected ones ranged 5%-76% -- a wide, safe margin for the threshold.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Detects a suspiciously long unbroken run of uppercase letters -- the
# signature of two+ words losing their inter-word space when pymupdf4llm
# reconstructs text from a bold run that PDF authoring split into multiple
# adjacent text spans with unusually tight kerning between them. Confirmed
# via a real document (a "TOTAL ASSETS" row split into 'TOTAL ', 'A',
# 'SSETS' spans, the last two touching with almost no gap) and via PyMuPDF's
# own issue tracker (#3804, "Multiple tokens get concatenated into one") --
# closed by maintainers as expected behaviour: space-insertion is a
# geometric-gap heuristic, not guaranteed correct for every document's own
# kerning, and there is no library fix or config flag for it. Length 8
# chosen empirically: long enough that ordinary short acronyms (ABN, GST,
# NDIS, AASB -- all <=4 chars) never trigger it, short enough to catch
# confirmed real merges ("NETASSETS"=9, "TOTALASSETS"=11).
MERGED_WORD_RE = re.compile(r"\b[A-Z]{8,}\b")


def _letters_only(s):
    return re.sub(r"[^A-Za-z]", "", s).upper()


def repair_merged_spacing(text, page_words):
    """General, domain-agnostic repair for words that lost their inter-word
    space during pymupdf4llm's markdown reconstruction (see MERGED_WORD_RE's
    docstring for why). NOT a list of known financial-statement terms --
    this has no idea what "TOTAL ASSETS" means, only that PyMuPDF's own
    page.get_text('words') is a second, independently-computed tokenisation
    of the SAME page that (confirmed empirically -- 'TOTAL' and 'ASSETS'
    come back as two distinct word tuples with a clean x-gap between them,
    on a row where the markdown layer merges them) doesn't make the same
    mistake. Word tokens, not a raw text blob: no windowed regex search
    needed, just a walk over PyMuPDF's own word boundaries.

    page_words is the plain list of word strings from page.get_text('words')
    (dropping the bbox/block/line/word-index fields the caller doesn't need
    here), in that call's natural order -- which keeps adjacent words on the
    same line adjacent in the list, the only ordering this needs.

    For each merged run, walks page_words looking for a consecutive
    sequence whose concatenated letters are IDENTICAL (case-insensitive) to
    the merged run, and if found, joins that sequence with single spaces.
    Inherently a no-op for any genuine single long word: a real word is
    just one token here too, so the "sequence" is length 1 and nothing
    changes. Only ever touches whitespace placement, never introduces or
    removes a letter -- it can't invent content, only restore a space a
    second, independent tokenisation of the same page already had."""
    def repair(m):
        merged = m.group(0)
        n = len(page_words)
        for i in range(n):
            # A candidate can only START at a word whose own letters are a
            # PREFIX of the target -- confirmed a real false match without
            # this: a purely-numeric word ("46,339,545", zero letters) is
            # trivially a "prefix" of everything if empty words are allowed
            # to start a match, which let the search silently walk straight
            # through unrelated number cells to reach "TOTAL"+"ASSETS" much
            # later, joining the numbers into the "fix" as if they were
            # part of the merged run.
            first_letters = _letters_only(page_words[i])
            if not first_letters or not merged.startswith(first_letters):
                continue
            letters, j = first_letters, i + 1
            while j < n and letters != merged:
                word_letters = _letters_only(page_words[j])
                # a word contributing NO letters (a number, a bare "-", a
                # stray symbol) breaks the run rather than being silently
                # skipped -- the merged run this whole function repairs is
                # by definition a run of LETTERS, never letters-then-a-
                # number-then-more-letters.
                if not word_letters or not merged.startswith(letters + word_letters):
                    break
                letters += word_letters
                j += 1
            if letters == merged:
                return " ".join(page_words[i:j])
        return merged
    return MERGED_WORD_RE.sub(repair, text)


def err(*a):
    print(*a, file=sys.stderr, flush=True)


@contextlib.contextmanager
def quiet_stdout():
    """Redirect C-level stdout (fd 1) to stderr during conversion, so MuPDF
    banners never contaminate the markdown we emit on stdout."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _page_image_coverage(page):
    """Fraction of the page's area covered by embedded images, using their
    PLACED rects (post-transform, clipped to the page) -- not native pixel
    size, since what matters is how much of the visible page an image
    occupies, not its resolution. Overlapping images are double-counted
    (rare in practice, not worth the complexity of proper union area) and
    the result is clamped to 1.0."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for img in page.get_images(full=True):
        for rect in page.get_image_rects(img[0]):
            clipped = rect & page.rect
            covered += clipped.width * clipped.height
    return min(covered / page_area, 1.0)


def classify(pdf_path, min_page_chars, garbage_char_ratio=0.05, image_coverage_threshold=0.5):
    """Return (page_count, needs_ocr_pages, total_chars, garbage_pages,
    image_pages) using PyMuPDF. needs_ocr_pages is image_only_pages UNIONED
    with pages whose text is present but mostly undecodable (see
    CONTROL_CHAR_RE) UNIONED with image_pages (see below) -- all three kinds
    need the mineru OCR engine, which reads rendered pixels and doesn't care
    that the embedded text is broken or beside the point. garbage_pages and
    image_pages are each reported separately so a page can be told apart
    from a genuine scan in logs.

    image_pages: a page more than image_coverage_threshold covered by a
    single embedded image, regardless of how much "text" it has. Confirmed
    a real, distinct failure mode this doesn't overlap with garbage_pages or
    the plain low-char-count check: a financial statement table pasted into
    an otherwise-native-text annual report as a picture (a screenshot, an
    Excel export, a photographed page) -- rotated 90 degrees in this
    specific case, corrected only by the page's own display transform, so
    it LOOKS upright when rendered. The page had substantial, non-garbled
    extracted text (a search-index OCR layer auto-generated when the image
    was embedded, common in PDF authoring tools) that passed every existing
    check, yet was column-major and unusable for table reconstruction --
    the text engine had no way to know the table it just "successfully"
    extracted wasn't real digital content at all. A large-image page is a
    strong, general, domain-agnostic signal that a page's true content is
    raster, not text, independent of whatever a coincidental text layer
    claims."""
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    needs_ocr, garbage_pages, image_pages, total_chars = [], [], [], 0
    for i in range(len(doc)):
        page = doc[i]
        txt = page.get_text("text")
        n = len(txt.strip())
        total_chars += n
        if n < min_page_chars:
            needs_ocr.append(i + 1)
            continue
        garbage_ratio = len(CONTROL_CHAR_RE.findall(txt)) / max(len(txt), 1)
        if garbage_ratio > garbage_char_ratio:
            needs_ocr.append(i + 1)
            garbage_pages.append(i + 1)
            continue
        if _page_image_coverage(page) > image_coverage_threshold:
            needs_ocr.append(i + 1)
            image_pages.append(i + 1)
    pc = len(doc)
    doc.close()
    return pc, needs_ocr, total_chars, garbage_pages, image_pages


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
    touched, no text is re-rendered; every downstream tool (MinerU, any PDF
    viewer) honours /Rotate identically.

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


PAGE_MARKER_RE = re.compile(r"<!-- page (\d+) -->")


def page_marker(page_number):
    """A page boundary needs to be visible in the raw text (without it,
    there's no way to know which page a table or value came from -- breaks
    both the "statement map" and rendering a highlighted excerpt for human
    review, which needs to search a KNOWN page rather than the whole
    document). But it must not read like a log line stapled into the
    document. A PDF's own page boundary is just blank space -- nothing
    announces it. An HTML comment is the closest text equivalent: invisible
    in any rendered markdown view (renders as nothing at all, same as a real
    page transition), while still a plain, greppable, single line in the
    raw text pdf2md-auto.sh and downstream tooling actually read."""
    return f"<!-- page {page_number} -->"


TITLE_INDEX_START = "<!-- pdf2md document index"


RUNNING_HEADER_MIN_REPEATS = 3


def build_title_index(markdown_with_markers):
    """Scan markdown that ALREADY has page_marker() lines embedded for
    heading lines, paired with the page each one falls on -- page 1 is
    implicit until the first marker. Returns a list of (level, text, page).

    Running headers/letterheads are suppressed: pymupdf4llm's own layout
    model classifies a page's bold masthead banner (organisation name + ABN,
    repeated near-verbatim at the top of nearly every page) as a heading,
    same as a real section title -- confirmed on a real document, 51 of
    ~417 entries were one repeated banner string, not genuine structure.
    Deleting every exact-duplicate heading anywhere in the document is NOT
    safe, though -- this same document legitimately repeats "Statement of
    Comprehensive Income" as a sub-heading for four different segment notes
    (four differently-named operating segments), and collapsing those would
    silently lose which page each segment's own statement is on. The
    specific, safe signature: a banner is always the FIRST heading
    encountered on its page (nothing preceded it since the last page
    transition) -- a real section heading essentially never is, since real
    section headings follow other content. Only text repeating in that
    specific first-of-page position, RUNNING_HEADER_MIN_REPEATS times or
    more, is suppressed; a heading appearing elsewhere on a page, however
    many times its text repeats, is left untouched."""
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
    """Render the heading+page index as ONE silent HTML comment block,
    appended at the very end of the document -- never inline, and never
    rendered. This is the same "invisible unless you're tooling, not a human
    reading the rendered page" choice as page_marker(), just for a bigger
    block: a rendered view of this file should show nothing a human
    wouldn't already see reading the source PDF; a document index that DID
    render would be exactly that kind of addition. Raw text / grep still
    finds it via TITLE_INDEX_START, a single stable anchor regardless of how
    many headings the document has."""
    if not index:
        return ""
    lines = [TITLE_INDEX_START]
    for level, text, page in index:
        indent = "  " * (level - 1)
        lines.append(f"{indent}- {text} (page {page})")
    lines.append("-->")
    return "\n\n" + "\n".join(lines) + "\n"


def to_markdown_text(pdf_path):
    """Digital PDF -> markdown via pymupdf4llm (CPU, no model load).
    Returns (markdown, page_boxes) -- page_boxes is a list of per-page block
    metadata (class, bbox, character position, and the block's own text) for
    provenance, or None if unavailable.

    use_ocr=False is required, not optional. pymupdf4llm >=1.28's default
    "layout" engine has its own internal per-page heuristic that silently
    invokes Tesseract OCR on pages it guesses might need it (e.g. a page with
    a background design image alongside real text) -- and, confirmed on a
    real filing (a dense financial statement page with a background design
    image), that OCR pass can fail to reconstruct a dense financial table at
    all, silently dropping it entirely, even though the page's real text
    layer -- extractable directly, no OCR needed -- has every figure intact.
    classify() has already routed
    this whole document down the "digital PDF" path specifically because it
    has a usable text layer; a scanned page that genuinely needs OCR belongs
    on the mineru path instead. Tesseract being present in this image (for
    the unrelated derotation OSD check) must not let pymupdf4llm reach for it
    on its own initiative.

    page_chunks=True (not page_separators=True) is used to get page
    boundaries: reconstructing the body by concatenating each chunk's own
    text is confirmed byte-identical to the plain single-string call, so
    nothing about the actual content changes -- but it also exposes
    page_boxes (per-block class/bbox/character-position within the page),
    the text engine's own answer to what MinerU's content_list.json already
    provides. Discarding that would mean re-deriving position data we
    already have for free. page_marker() is inserted only BETWEEN chunks
    (page 1 needs no marker -- it's implicitly page 1 from the start of the
    document), so the body reads as one continuous document, not a stream
    interrupted every page.

    A heading + page-number index is appended at the very end, inside a
    single silent HTML comment (see format_title_index()) -- the raw
    material for a "statement map" (locate the Statement of Financial
    Position, Cash Flow Statement etc. by page, once, before extracting
    anything downstream), built here because heading detection is a generic
    property of the document's own markdown, not any particular caller's
    concern; matching which heading text means which target statement stays
    in the downstream pipeline that actually knows what it's looking for.
    """
    import fitz
    import pymupdf4llm
    chunks = pymupdf4llm.to_markdown(pdf_path, use_ocr=False, page_chunks=True)
    plain_doc = fitz.open(pdf_path)  # second, independent tokenisation -- see repair_merged_spacing()

    parts = []
    page_boxes = []
    offset = 0
    for i, chunk in enumerate(chunks):
        text = chunk.get("text", "")
        page_number = chunk.get("metadata", {}).get("page_number", i + 1)
        page_words = ([w[4] for w in plain_doc[page_number - 1].get_text("words")]
                      if 0 <= page_number - 1 < len(plain_doc) else [])
        if i > 0:
            marker = "\n\n" + page_marker(page_number) + "\n\n"
            parts.append(marker)
            offset += len(marker)
        # Slicing for page_boxes happens against the ORIGINAL (unrepaired)
        # text, before repair_merged_spacing can change its length -- box
        # positions come from pymupdf4llm itself and would drift out of
        # alignment with a longer/shorter string. Each box's own extracted
        # text is then repaired independently (a short, standalone string,
        # no position dependency), and the chunk's text used for the actual
        # body is repaired separately. doc_pos may end up a few characters
        # approximate on a chunk that had a repair; nothing currently reads
        # doc_pos for exact slicing (block matching is substring-based,
        # highlighting is bbox-based, not text-length-based), so this is an
        # acceptable, bounded tradeoff for fixing the actual content.
        for box in chunk.get("page_boxes", []) or []:
            # "text" is added here (pymupdf4llm's own page_boxes don't include
            # it, only position) so this sidecar is self-contained the same
            # way MinerU's content_list.json already is -- a downstream
            # reader for either engine should be able to get a block's text
            # straight from this file, not need engine-specific logic to
            # know pymupdf4llm's needs slicing chunk['text'][pos[0]:pos[1]]
            # while MinerU's is already a plain field.
            block_text = text[box["pos"][0]:box["pos"][1]] if box.get("pos") else None
            if block_text:
                block_text = repair_merged_spacing(block_text, page_words)
            page_boxes.append({**box, "text": block_text,
                                "page_number": page_number,
                                "doc_pos": (offset + box["pos"][0], offset + box["pos"][1]) if box.get("pos") else None})
        repaired_text = repair_merged_spacing(text, page_words)
        parts.append(repaired_text)
        offset += len(repaired_text)

    plain_doc.close()
    body = "".join(parts)
    body += format_title_index(build_title_index(body))
    return body, (page_boxes or None)


def main():
    ap = argparse.ArgumentParser(prog="pdf2md", description="PDF to Markdown for digital PDFs.")
    ap.add_argument("input", help="input PDF path")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--image-threshold", type=float, default=0.2,
                    help="fraction of pages needing OCR (image-only, undecodable text -- see "
                         "--garbage-char-ratio, OR mostly-one-image -- see "
                         "--image-coverage-threshold) above which a PDF classifies as 'scan' "
                         "(default 0.2)")
    ap.add_argument("--min-page-chars", type=int, default=20,
                    help="a page with fewer stripped chars counts as image-only (default 20)")
    ap.add_argument("--image-coverage-threshold", type=float, default=0.5,
                    help="a page more than this fraction covered by a single embedded image "
                         "counts as needing OCR too, regardless of its text layer -- confirmed a "
                         "real case (a pasted screenshot of a financial table, rotated, with an "
                         "auto-generated column-major OCR text layer that passed every other "
                         "check while being structurally unusable) (default 0.5)")
    ap.add_argument("--garbage-char-ratio", type=float, default=0.05,
                    help="a page whose extracted text is more than this fraction control "
                         "characters counts as needing OCR too -- a font subsetted without a "
                         "proper ToUnicode CMap looks like plenty of 'text' by character count but "
                         "is undecodable garbage; confirmed on a real corpus that clean pages "
                         "measure exactly 0.0 here, affected ones 5%-76%, so this default has a "
                         "wide safety margin (default 0.05)")
    ap.add_argument("--classify-only", action="store_true",
                    help="print 'digital' or 'scan' to stdout and exit (no model load); "
                         "used by pdf2md-auto.sh to route between the text/mineru engines")
    ap.add_argument("--derotate", metavar="OUTPUT.pdf",
                    help="detect per-page rotation via Tesseract OSD and write a corrected copy "
                         "to OUTPUT.pdf, then exit. Only the /Rotate flag is changed -- no pixel "
                         "or content is altered. Used by pdf2md-auto.sh ahead of every conversion.")
    ap.add_argument("--rotate-dpi", type=int, default=150,
                    help="render DPI used for --derotate's OSD pass (default 150)")
    ap.add_argument("--rotate-min-confidence", type=float, default=1.0,
                    help="minimum OSD confidence required to apply a rotation correction (default 1.0)")
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
        pc, needs_ocr, total_chars, garbage_pages, image_pages = classify(
            args.input, args.min_page_chars, args.garbage_char_ratio, args.image_coverage_threshold)
    except Exception as e:
        err(f"[pdf2md] ERROR opening/classifying PDF: {e}")
        sys.exit(2)

    ratio = (len(needs_ocr) / pc) if pc else 1.0
    log(f"[pdf2md] {args.input}: {pc} pages, {len(needs_ocr)} need OCR "
        f"({ratio:.0%}), {total_chars} text chars")
    if garbage_pages:
        log(f"[pdf2md] {len(garbage_pages)} page(s) have a broken/undecodable text layer "
            f"(font subsetted without a proper ToUnicode CMap -- real text exists but "
            f"pymupdf can't decode it): {garbage_pages}")
        log(f"[pdf2md] ANY garbage-text page forces 'scan' classification for the whole "
            f"document, regardless of --image-threshold: unlike a genuinely blank/scanned "
            f"page (which the text engine renders as an honest gap), a garbage page produces "
            f"actively WRONG text that a fast per-page ratio check could still let slip through "
            f"un-flagged if it were a small fraction of a large document.")
    if image_pages:
        log(f"[pdf2md] {len(image_pages)} page(s) are mostly covered by a single embedded "
            f"image (a pasted screenshot/scan/export, not native text) despite having a "
            f"real, non-garbled text layer -- confirmed a real case where that text layer "
            f"was column-major OCR output, structurally unusable for table reconstruction "
            f"even though it read as legitimate 'digital' text: {image_pages}. These are "
            f"counted in needs_ocr/the ratio above like any other page needing OCR, but "
            f"(unlike garbage_pages) do NOT force whole-document 'scan' classification on "
            f"their own -- routing a document with a few such pages entirely through the "
            f"slow MinerU path is a bigger tradeoff than this check alone should make; a "
            f"document where they push the ratio over --image-threshold routes to mineru "
            f"the normal way, one where they don't will still convert this page's table "
            f"incorrectly via the text engine -- known, logged, not yet fixed further.")

    if args.classify_only:
        print("scan" if garbage_pages or ratio > args.image_threshold else "digital")
        return

    try:
        with quiet_stdout():
            out, page_boxes = to_markdown_text(args.input)
        # Guard: if this 'digital' PDF actually yielded almost nothing, it was
        # really a scan -> tell the user rather than emit near-empty markdown.
        if len(out.strip()) < args.min_page_chars * max(pc, 1) * 0.2:
            err(f"[pdf2md] WARNING: produced very little output ({len(out.strip())} chars). "
                f"This PDF looks scanned; convert it with the mineru engine instead "
                f"(pdf2md-auto.sh routes this automatically).")
    except Exception as e:
        err(f"[pdf2md] ERROR during conversion: {e}")
        sys.exit(4)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        log(f"[pdf2md] wrote {args.output} ({len(out)} chars) in {time.time()-t0:.1f}s")
        if page_boxes:
            import json
            boxes_path = os.path.splitext(args.output)[0] + ".content_list.json"
            with open(boxes_path, "w", encoding="utf-8") as f:
                json.dump(page_boxes, f, ensure_ascii=False, indent=2)
            log(f"[pdf2md] wrote {boxes_path} (page_number + bbox per block, for provenance -- "
                f"same sibling-artifact convention as the mineru engine)")
    else:
        sys.stdout.write(out)
        log(f"[pdf2md] done in {time.time()-t0:.1f}s ({len(out)} chars)")


if __name__ == "__main__":
    main()
