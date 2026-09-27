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


BARE_NUMBER_RE = re.compile(r"(?<![\w,.])\(?-?\d{4,}(?:\.\d+)?\)?(?![\w,.])")
GROUPED_NUMBER_RE = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?")


def repair_table_numbers(text, page_words):
    """Put back thousands separators the table-cell path dropped. On tightly
    spaced statement rows the table's row boundary can cut through a
    number's low-sitting commas: "(1,207,513)" came out "(1207513)" (its
    commas landing in the next row as ",,"), and "66,529" as "66529" -- on a
    real report, where MinerU had kept them. A bare number is rewritten only
    when it appears nowhere on the page as written, and exactly ONE word in
    the page's own text layer has the same digits, sign and decimals with
    separators; digits never change."""
    grouped = {}
    for w in page_words:
        for m in GROUPED_NUMBER_RE.finditer(w):
            grouped.setdefault(m.group(0).replace(",", ""), set()).add(m.group(0))
    bare_on_page = {w for w in page_words if BARE_NUMBER_RE.fullmatch(w)}
    if not grouped:
        return text

    def fix(m):
        tok = m.group(0)
        if tok in bare_on_page:
            return tok
        found = grouped.get(tok)
        return next(iter(found)) if found and len(found) == 1 else tok
    return BARE_NUMBER_RE.sub(fix, text)


JUNK_CELL_LINE_RE = re.compile(r"^[,.\s]*$")
AMOUNT_LINE_RE = re.compile(r"^\s*[($]?\s*[-\u2010\u2011\u2012\u2013\u2014]?\s*"
                            r"(?:\d{1,3}(?:,\d{3})*|\d+)?(?:\.\d+)?\s*\)?\s*%?\s*$")


def split_merged_rows(text):
    """Split a merged statement row back into its rows. The table-cell path
    merged pairs of statement rows on a real report ("Additions<br>Disposals
    | 35,156<br>(1,207,513) | 1,320<br>-"). A row is split only when all of
    these hold: it is a body row (never a table's header row); its first
    cell holds 2+ label lines; every other cell holds the SAME number of
    lines, each an amount or a nil mark; lines that are empty or only
    commas/stops (separators pushed out of a number, see
    repair_table_numbers) don't count. Anything else -- a wrapped label
    beside one value, a header with year and unit, prose -- is left as is."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        is_header = i + 1 < len(lines) and lines[i + 1].startswith("|---")
        if is_header or not (line.startswith("|") and line.endswith("|") and "<br>" in line):
            out.append(line)
            continue
        cells = line[1:-1].split("|")
        parts = [[x for x in c.split("<br>") if not JUNK_CELL_LINE_RE.match(x)] for c in cells]
        k = len(parts[0])
        ok = (k >= 2 and len(cells) >= 2 and all(len(p) == k for p in parts)
              and not any(AMOUNT_LINE_RE.match(x) for x in parts[0])
              and all(AMOUNT_LINE_RE.match(x) for p in parts[1:] for x in p))
        if ok:
            for j in range(k):
                out.append("|" + "|".join(p[j].strip() for p in parts) + "|")
        else:
            out.append(line)
    return "\n".join(out)


UNDECODABLE_RUN_RE = re.compile(r"(?:<mark>)?\ufffd{4,}(?:</mark>)?")


def mark_undecodable(text):
    """Replace a run of 4+ U+FFFD (text whose font has no Unicode map and no
    embedded program to recover one from) with "[undecodable text]". Such a
    run carries no information, and a reader or caller can't tell it from
    corruption: on a real report, a 58-character stamp in a non-embedded,
    unmapped font came out as a highlighted run of U+FFFD at the top of
    nearly every page. Shorter runs stay as they are (a lone U+FFFD sits
    inside a word, where the word's other letters still carry it)."""
    return UNDECODABLE_RUN_RE.sub("[undecodable text]", text)


# Letter sequences fonts commonly draw as one ligature glyph.
LIGATURE_SEQUENCES = ("ffi", "ffl", "tti", "ff", "fi", "fl", "fj", "ft", "st", "ct", "ch",
                      "ti", "tt", "th")
WORD_TOKEN_RE = re.compile(r"[^\W\d_]+")


def repair_ligature_letters(text, page_words):
    """Put back letters lost after a ligature glyph. When one glyph stands
    for several letters ("fl", "ti"), PyMuPDF gives the letters after the
    first zero-width boxes, and the table-cell path clips them away:
    "snowflakes" came out "snowfakes" (confirmed on a test flyer, once its
    ligature glyphs had their text back). The page's own words, from a
    second independent tokenisation (see repair_merged_spacing), still have
    them.

    Touches a word only when it appears nowhere among the page's words and
    exactly ONE page word equals it with a ligature sequence's trailing
    letters restored at one position. It can only restore letters the page's
    text layer has at that spot; it never invents a word."""
    page_set = {w for pw in page_words for w in WORD_TOKEN_RE.findall(pw)}
    if not page_set:
        return text

    def candidates(word):
        found = set()
        for i in range(1, len(word) + 1):
            head, tail = word[:i], word[i:]
            for seq in LIGATURE_SEQUENCES:
                if head.endswith(seq[0]):
                    fixed = head + seq[1:] + tail
                    if fixed in page_set:
                        found.add(fixed)
        return found

    def fix(m):
        word = m.group(0)
        if word in page_set or len(word) < 3:
            return word
        found = candidates(word)
        return found.pop() if len(found) == 1 else word
    return WORD_TOKEN_RE.sub(fix, text)


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


# A background image is one with VISIBLE text drawn on top of it at a density
# of at least this many chars per full page of image area (and at least
# BACKGROUND_MIN_CHARS in absolute terms). A normal page of body text is
# ~2,000 chars, so 500 means "a quarter of a normal page's text density".
# The thresholds only need to separate two very different shapes: designed
# pages (text set over a photo or tinted panel: every day box of a grid flyer
# sits on a background strip) from scans with a few visible words stamped on
# top (a Bates number, a "page 3 of 10" footer) -- tens of chars over a
# full-page image, which stays well under both.
BACKGROUND_MIN_CHARS_PER_PAGE_AREA = 500
BACKGROUND_MIN_CHARS = 20


@contextlib.contextmanager
def _restore_none_refs():
    """Give back the references to None that PyMuPDF 1.28.x's get_bboxlog(),
    get_texttrace() and get_drawings() drop (see _drawing_facts). Copy what
    you need out of their results and drop them inside the block."""
    import ctypes
    before = sys.getrefcount(None)
    try:
        yield
    finally:
        for _ in range(max(before - sys.getrefcount(None), 0)):
            ctypes.pythonapi.Py_IncRef(ctypes.py_object(None))


# A digital page reads upright when at least this share of its visible text
# (and at least UPRIGHT_MIN_CHARS of it) runs left to right as displayed.
UPRIGHT_MIN_SHARE = 0.9
UPRIGHT_MIN_CHARS = 100


def _text_reads_upright(page):
    """True when the page's VISIBLE text layer says the page is upright as
    displayed: most of it runs left to right after the page's /Rotate. The
    text layer knows its own direction exactly; Tesseract's OSD guesses it
    from pixels, and on a real report it called four upright notes pages
    "rotated 180", "verified" the flip, and the text engine then emitted
    nothing for them. Invisible text (a scan's OCR layer) doesn't count, so
    scans still go to OSD."""
    import pymupdf
    with _restore_none_refs():
        trace = page.get_texttrace()
        spans = [(tuple(s["dir"]), sum(1 for c in s["chars"] if not chr(c[0]).isspace()))
                 for s in trace if s["type"] != 3 and s["opacity"] > 0]
        del trace
    total = sum(n for _, n in spans)
    if total < UPRIGHT_MIN_CHARS:
        return False
    m = page.rotation_matrix
    upright = 0
    for (dx, dy), n in spans:
        v = pymupdf.Point(dx, dy) * m - pymupdf.Point(0, 0) * m
        if v.x > 0.9 and abs(v.y) < 0.2:
            upright += n
    return upright >= UPRIGHT_MIN_SHARE * total


def _drawing_facts(page):
    """(images, text_spans, opaque_rects) in drawing order, as plain Python
    values: images [(Rect, seqno)] for every fill-image command; text_spans
    [(seqno, visible, Rect, nonspace_chars)] for every text span; and
    opaque_rects [(seqno, Rect, fill_rgb)] for every opaque filled RECTANGLE
    (see _opaque_rect).

    PyMuPDF 1.28.x's get_bboxlog(), get_texttrace() and get_drawings() drop
    references to None on every call (measured: about a thousand per 28-page
    document for the first two, still present in 1.28.2). On Python 3.11,
    where None is not immortal, that frees None after a few thousand pages in
    one process and the interpreter aborts ("none_dealloc") -- confirmed on
    the fourth PDF of a corpus run. So copy out what we need, drop PyMuPDF's
    objects, and give back whatever references the calls took.
    Over-restoring only keeps None alive, which it always is; under-restoring
    is the crash. Remove this once the image runs a fixed PyMuPDF or Python
    3.12+."""
    import ctypes
    import pymupdf
    before = sys.getrefcount(None)
    log = page.get_bboxlog()
    images = [(pymupdf.Rect(b), i) for i, (kind, b) in enumerate(log)
              if kind == "fill-image"]
    trace = page.get_texttrace()
    # render mode 3 is invisible text: the search-index OCR layer of a scan
    # or a pasted screenshot sits over the image but isn't what a reader sees
    spans = [(s["seqno"], s["type"] != 3 and s["opacity"] > 0, pymupdf.Rect(s["bbox"]),
              sum(1 for c in s["chars"] if not chr(c[0]).isspace()))
             for s in trace]
    drawings = page.get_drawings()
    rects = [(d["seqno"], pymupdf.Rect(d["rect"]), tuple(d["fill"]))
             for d in drawings if _opaque_rect(d)]
    del log, trace, drawings
    for _ in range(max(before - sys.getrefcount(None), 0)):
        ctypes.pythonapi.Py_IncRef(ctypes.py_object(None))
    return images, spans, rects


def _opaque_rect(d):
    """True for a fully opaque, filled, axis-aligned rectangle: a 're' item,
    or lines/quads whose every point is a corner of the path's bounding box
    (redaction bars are often drawn as three lines and a close). Anything
    curved or slanted is out: a decorative swoosh's bounding box can overlap
    text it doesn't actually cover, and treating that as cover would delete
    real content."""
    if d.get("fill") is None or (d.get("fill_opacity") or 0) < 0.99 \
            or d.get("type") not in ("f", "fs"):
        return False
    r = d["rect"]
    if r.width <= 0 or r.height <= 0:
        return False
    corners = [(r.x0, r.y0), (r.x1, r.y0), (r.x0, r.y1), (r.x1, r.y1)]
    for item in d["items"]:
        if item[0] == "re":
            continue
        if item[0] == "l":
            pts = item[1:3]
        elif item[0] == "qu":
            q = item[1]
            pts = (q.ul, q.ur, q.ll, q.lr)
        else:
            return False
        if not all(any(abs(p.x - cx) < 0.5 and abs(p.y - cy) < 0.5 for cx, cy in corners)
                   for p in pts):
            return False
    return True


def _visible_chars_over(spans, rect, seqno):
    """Chars of visible text drawn AFTER drawing command `seqno` with the
    span's centre inside `rect`. Text drawn before the image is underneath
    it and hidden, so it doesn't count; nor does invisible text."""
    return sum(n for sq, visible, box, n in spans
               if visible and sq > seqno
               and rect.contains(((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)))


# A span counts as covered when a single opaque rectangle drawn after it hides
# at least this share of its box. Redaction bars are cut to the line, so they
# cover about all of it; a panel that merely touches a line's edge doesn't.
COVERED_MIN_SHARE = 0.8


# The drawing list alone can't be trusted for cover: on real annual reports it
# reported opaque rectangles "over" plainly visible text -- black page-sized
# fills (clipped or masked in ways get_drawings() doesn't expose) and a
# near-white table fill over income-statement rows. Nor can "the box renders
# mostly in the fill colour": a wide, sparse table row is mostly background.
# The one test that holds: remove the candidate text from a copy of the page
# and render both. Text that is really hidden changes nothing; visible text
# takes its ink with it. A span is covered only if at most COVERED_MAX_PIXELS
# pixels in its box change (at COVERED_RENDER_DPI). Measured: really hidden
# spans change 0-1 pixels; a single visible nil dash in a wide table row
# changes 20+, so a share of the box (a first attempt, 1%) let visible dashes
# through. Any doubt keeps the text -- deleting real content is the worse
# failure.
COVERED_MAX_PIXELS = 4
COVERED_PIXEL_TOLERANCE = 32
COVERED_RENDER_DPI = 144


def _redaction_rect(box):
    """The rect a covered span is redacted with: inset vertically, so a
    line's redaction can't clip the lines above and below it."""
    import pymupdf
    inset = box.height * 0.25
    return pymupdf.Rect(box.x0, box.y0 + inset, box.x1, box.y1 - inset)


def _render(page, dpi):
    """(pixmap, had_errors) for the page, MuPDF's error printing silenced."""
    import pymupdf
    display = pymupdf.TOOLS.mupdf_display_errors()
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_warnings(reset=True)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    errors = "error" in pymupdf.TOOLS.mupdf_warnings(reset=True)
    pymupdf.TOOLS.mupdf_display_errors(display)
    return pix, errors


def find_covered_text(page):
    """find_covered_text on an unrotated view of the page (restored after):
    span boxes, drawings and redaction rects must share one coordinate space,
    and on a rotated page they didn't -- a redaction that removed nothing
    changed nothing, which read as "hidden" (confirmed: whole visible pages of
    a rotated report flagged). Returned rects are in unrotated coordinates;
    prepare_pdf redacts with the page unrotated too."""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        return _find_covered_unrotated(page)
    finally:
        if rot:
            page.set_rotation(rot)


def _find_covered_unrotated(page):
    """Visible text spans hidden by an opaque rectangle drawn on top of them:
    cosmetic redactions (black bars over text that is still in the file) and
    text tucked under a panel. A span counts only if the geometry says a
    later opaque rectangle covers it AND removing it leaves the rendered page
    unchanged (see COVERED_MAX_PIXELS). [(span_rect, nonspace_chars, fill_rgb)]."""
    import pymupdf
    _, spans, rects = _drawing_facts(page)
    candidates = []
    for sq, visible, box, n in spans:
        if not visible or n == 0 or box.is_empty:
            continue
        for rsq, rect, fill in rects:
            if rsq > sq and (box & rect).get_area() >= COVERED_MIN_SHARE * box.get_area():
                candidates.append((box, n, fill))
                break
    if not candidates:
        return []
    before, errors = _render(page, COVERED_RENDER_DPI)
    # a page MuPDF can't render faithfully proves nothing (a pattern fill it
    # doesn't understand renders as solid black over a readable page)
    if errors:
        return []
    try:   # a malformed file that can't be copied or redacted proves nothing
        copy = pymupdf.open()
        copy.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        cpage = copy[0]
        for box, _, _ in candidates:
            cpage.add_redact_annot(_redaction_rect(box), fill=False)
        cpage.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                               graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                               text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        after, errors = _render(cpage, COVERED_RENDER_DPI)
        # "unchanged" only means hidden if the text really was removed
        still_there = [bool(cpage.get_textbox(_redaction_rect(box)).strip())
                       for box, _, _ in candidates]
        copy.close()
    except Exception:
        return []
    if errors or (after.width, after.height) != (before.width, before.height):
        return []
    scale = COVERED_RENDER_DPI / 72
    covered = []
    for (box, n, fill), kept in zip(candidates, still_there):
        if kept:
            continue
        r = box & page.rect
        x0, y0 = max(int(r.x0 * scale), 0), max(int(r.y0 * scale), 0)
        x1, y1 = min(int(r.x1 * scale) + 1, before.width), min(int(r.y1 * scale) + 1, before.height)
        total = changed = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                total += 1
                pb, pa = before.pixel(x, y), after.pixel(x, y)
                if any(abs(pb[k] - pa[k]) > COVERED_PIXEL_TOLERANCE for k in range(3)):
                    changed += 1
        if total and changed <= COVERED_MAX_PIXELS:
            covered.append((box, n, fill))
    return covered


def _merge_blocks(rects):
    """Merge rects that overlap horizontally and sit within a few line heights
    of each other vertically, until nothing more merges: the lines (and
    superscripts) of one redacted paragraph or box become one block."""
    rects = [r for r in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                h = min(a.height, b.height)
                gap = max(a.y0, b.y0) - min(a.y1, b.y1)
                if a.x0 < b.x1 and b.x0 < a.x1 and gap < 4 * h:
                    rects[i] = a | b
                    del rects[j]
                    merged = True
                    break
            if merged:
                break
    return rects


CMAP_CODE_RE = re.compile(rb"<([0-9A-Fa-f]+)>")


def _cmap_codes(cmap: bytes) -> set[int]:
    """Source codes a ToUnicode CMap maps (bfchar entries and bfrange spans)."""
    codes = set()
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap, re.S):
        hexes = CMAP_CODE_RE.findall(block)
        codes.update(int(h, 16) for h in hexes[0::2])
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap, re.S):
        for line in block.splitlines():
            hexes = CMAP_CODE_RE.findall(line)
            if len(hexes) >= 2:
                codes.update(range(int(hexes[0], 16), int(hexes[1], 16) + 1))
    return codes


# Unicode's ligature presentation forms (U+FB00-FB06), spelled out: the same
# text in the letters every engine and caller expects. A ToUnicode map that
# points a glyph at one of these is rewritten to the letters -- the text
# engine's table path drops U+FB02 outright ("snowﬂakes" -> "snowfakes",
# confirmed on a test flyer).
LIGATURE_CHARS = {0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi",
                  0xFB04: "ffl", 0xFB05: "st", 0xFB06: "st"}


def _spell_out_ligature_chars(cmap: bytes) -> tuple[bytes, int]:
    """(cmap, n): bfchar destinations that are a single ligature character
    replaced by their letters."""
    n = 0

    def fix_block(m):
        nonlocal n

        def fix_pair(pm):
            nonlocal n
            dst = int(pm.group(2), 16)
            if len(pm.group(2)) == 4 and dst in LIGATURE_CHARS:
                n += 1
                letters = LIGATURE_CHARS[dst].encode("utf-16-be").hex().upper().encode()
                return b"<" + pm.group(1) + b"> <" + letters + b">"
            return pm.group(0)
        return re.sub(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", fix_pair, m.group(0))
    cmap = re.sub(rb"beginbfchar.*?endbfchar", fix_block, cmap, flags=re.S)
    return cmap, n


def repair_ligature_unicode(doc):
    """Give ligature glyphs their text back. [{"font", "glyphs": {gid: text}}].

    Word-exported PDFs (Calibri and friends) often draw "ti", "ff" and the
    like as single ligature glyphs and leave them out of the font's
    ToUnicode CMap. Readers then fall back to the glyph code as a character:
    "Operating" extracts as "OperaƟng" (U+019F) or "Opera\ufffdng" --
    confirmed on a test flyer. The embedded font says exactly what each
    ligature glyph stands for, in its own GSUB ligature table, so this adds
    the missing CMap entries from there. Nothing is inferred from the text.

    Only Type0 fonts with Identity-H encoding, an identity CID-to-glyph map and
    an embedded TrueType program qualify (code == glyph id there); only
    glyphs the CMap leaves unmapped are added, never an existing mapping
    changed -- except that a mapping to a ligature presentation character
    (U+FB00-FB06) is spelled out as its letters (see LIGATURE_CHARS). Any font
    that can't be read is left as it is."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return []
    import io
    fixes = []
    seen = set()
    for pno in range(len(doc)):
        for xref, ext, ftype, name, _, enc in doc[pno].get_fonts():
            if xref in seen or ftype != "Type0" or enc != "Identity-H":
                continue
            seen.add(xref)
            try:
                tu = doc.xref_get_key(xref, "ToUnicode")
                desc = doc.xref_get_key(xref, "DescendantFonts")
                if tu[0] != "xref" or "CIDToGIDMap/Identity" not in desc[1].replace(" ", "") \
                        and "CIDToGIDMap" in desc[1]:
                    continue
                tu_xref = int(tu[1].split()[0])
                buf = doc.extract_font(xref)[3]
                if not buf:
                    continue
                font = TTFont(io.BytesIO(buf), lazy=True)
                if "GSUB" not in font or "cmap" not in font:
                    continue
                order = font.getGlyphOrder()
                gid_of = {g: i for i, g in enumerate(order)}
                # glyph name -> character, from the font's own cmap
                char_of = {}
                for table in font["cmap"].tables:
                    if table.isUnicode():
                        for cp, g in table.cmap.items():
                            char_of.setdefault(g, chr(cp))
                ligatures = {}
                for lookup in font["GSUB"].table.LookupList.Lookup:
                    for st in lookup.SubTable:
                        st = getattr(st, "ExtSubTable", st)
                        for first, ligs in getattr(st, "ligatures", {}).items():
                            for lg in ligs:
                                parts = [first, *lg.Component]
                                if all(p in char_of for p in parts):
                                    ligatures.setdefault(gid_of[lg.LigGlyph],
                                                         "".join(char_of[p] for p in parts))
                cmap = doc.xref_stream(tu_xref)
                missing = {g: t for g, t in ligatures.items()
                           if g not in _cmap_codes(cmap) and g <= 0xFFFF}
                cmap, spelled = _spell_out_ligature_chars(cmap)
                if not missing and not spelled:
                    continue
                if not missing:
                    doc.update_stream(tu_xref, cmap)
                    fixes.append({"font": name, "glyphs": {}, "spelled_out": spelled})
                    continue
                entries = b"".join(b"<%04X> <%s>\n" % (g, t.encode("utf-16-be").hex().upper().encode())
                                   for g, t in sorted(missing.items()))
                block = b"%d beginbfchar\n%sendbfchar\n" % (len(missing), entries)
                if b"endcmap" not in cmap:
                    continue
                doc.update_stream(tu_xref, cmap.replace(b"endcmap", block + b"endcmap", 1))
                fixes.append({"font": name, "glyphs": {str(g): t for g, t in sorted(missing.items())},
                              "spelled_out": spelled})
            except Exception:
                continue
    return fixes


def prepare_pdf(pdf_path, output_path):
    """Write the copy every engine converts, and return what was changed:
    {"pages": [covered-text rows], "ligature_fixes": [...]}.

    1. Covered text REMOVED (a real PDF redaction, not another box on top).
       A cosmetic redaction keeps the original text in the file; an OCR
       engine reads only what renders, but a text-layer engine reads it all
       -- confirmed on a test flyer, where the text engine emitted four
       blacked-out day boxes and a name hidden under the header band, with
       no warning. Graphics and images are untouched, so the page renders
       as before (the bars stay black; OCR sees the same page). Text under a
       dark fill is marked with one invisible "[redacted]" per block (render
       mode 3: in the text layer, not on the page), so the text output says
       something was there; text hidden under any other fill is dropped
       without a marker. See find_covered_text for how "covered" is proven.
    2. Ligature glyphs given their text back (repair_ligature_unicode).

    A document needing neither is copied byte for byte, so it converts
    exactly as it did before this step existed."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    report = []
    for page in doc:
        rot = page.rotation
        if rot:
            page.set_rotation(0)   # find_covered_text's coordinates are unrotated
        try:
            covered = _strip_page(page)
        finally:
            if rot:
                page.set_rotation(rot)
        if covered:
            report.append(covered)
    ligature_fixes = repair_ligature_unicode(doc)
    if report or ligature_fixes:
        doc.save(output_path, garbage=3, deflate=True)
        doc.close()
    else:
        # nothing to change: hand the engines the original bytes, not a
        # re-save, so these documents convert exactly as before
        doc.close()
        import shutil
        shutil.copyfile(pdf_path, output_path)
    return {"pages": report, "ligature_fixes": ligature_fixes}


def _strip_page(page):
    """Covered-text removal for one unrotated page: its report row, or None."""
    import pymupdf
    try:
        covered = _find_covered_unrotated(page)
    except Exception as e:   # never fail a conversion over this check
        err(f"[pdf2md] --prepare: page {page.number + 1} not checked for covered text: {e}")
        return None
    if not covered:
        return None
    markers = []   # dark-covered spans, merged below into blocks
    for box, n, fill in covered:
        page.add_redact_annot(_redaction_rect(box), fill=False)
        if sum(fill) / 3 < 0.2:
            markers.append(pymupdf.Rect(box))
    markers = _merge_blocks(markers)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for m in markers:
        page.insert_text((m.x0, m.y0 + min(m.height, 10) * 0.8), "[redacted]",
                         fontsize=min(m.height, 10) * 0.8, render_mode=3)
    return {"page": page.number + 1, "covered_spans": len(covered),
            "covered_chars": sum(n for _, n, _ in covered),
            "redaction_markers": len(markers)}


def _page_image_coverage(page):
    """(coverage, background_coverage): the fraction of the page's area
    covered by embedded images, using their PLACED rects (post-transform,
    clipped to the page) -- not native pixel size, since what matters is how
    much of the visible page an image occupies, not its resolution -- and
    how much of that is BACKGROUND (see BACKGROUND_MIN_CHARS_PER_PAGE_AREA):
    images with enough visible text drawn over them that the text, not the
    image, is the page's content. Overlapping images are double-counted
    (rare in practice, not worth the complexity of proper union area) and
    both results are clamped to 1.0.

    Background images are reported apart so image_page ignores them.
    Confirmed case: a one-page grid flyer (day boxes of visible text over a
    background photo stored as six full-width strips) measured 67.5% image
    coverage and routed to MinerU despite a complete text layer; MinerU lost
    the title (white text on a coloured band) and scrambled the box order.
    (Some of its boxes were also blacked out -- cosmetic redactions, handled
    separately by prepare_pdf, so the text engine can't leak them.)"""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0, 0.0
    # each placed image's drawing-sequence number, to tell text drawn over it
    # from text underneath it; texttrace seqnos index the same sequence
    image_seqnos, spans, _ = _drawing_facts(page)
    covered = background = 0.0
    for img in page.get_images(full=True):
        for rect in page.get_image_rects(img[0]):
            clipped = rect & page.rect
            area = clipped.width * clipped.height
            covered += area
            # no matching draw command (shouldn't happen): count it as a
            # plain image -- the pre-existing, OCR-leaning behaviour
            seqno = next((i for r, i in image_seqnos
                          if abs(r.x0 - rect.x0) < 1 and abs(r.y0 - rect.y0) < 1
                          and abs(r.x1 - rect.x1) < 1 and abs(r.y1 - rect.y1) < 1), None)
            if seqno is None or area <= 0:
                continue
            chars = _visible_chars_over(spans, clipped, seqno)
            if chars >= max(BACKGROUND_MIN_CHARS,
                            BACKGROUND_MIN_CHARS_PER_PAGE_AREA * area / page_area):
                background += area
    return min(covered / page_area, 1.0), min(background / page_area, 1.0)


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
    import pymupdf
    doc = pymupdf.open(pdf_path)
    needs_ocr, garbage_pages, image_pages, total_chars = [], [], [], 0
    per_page = []  # one fact-row per page, consumed by --classify-pages routing
    for i in range(len(doc)):
        page = doc[i]
        txt = page.get_text("text")
        n = len(txt.strip())
        total_chars += n
        cov, bg_cov = _page_image_coverage(page)
        reasons = []
        if n < min_page_chars:
            reasons.append("low_text")
        else:
            garbage_ratio = len(CONTROL_CHAR_RE.findall(txt)) / max(len(txt), 1)
            if garbage_ratio > garbage_char_ratio:
                reasons.append("garbage_text")
            if cov - bg_cov > image_coverage_threshold:
                reasons.append("image_page")
        per_page.append({"page": i + 1, "text_chars": n,
                         "image_coverage": round(cov, 3),
                         "background_image_coverage": round(bg_cov, 3),
                         "class": "ocr" if reasons else "text",
                         "reasons": reasons})
        if reasons:
            needs_ocr.append(i + 1)
            if "garbage_text" in reasons:
                garbage_pages.append(i + 1)
            if "image_page" in reasons:
                image_pages.append(i + 1)
    pc = len(doc)
    doc.close()
    return pc, needs_ocr, total_chars, garbage_pages, image_pages, per_page


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
    import pymupdf
    import pytesseract
    from PIL import Image

    def osd_of(page):
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            return pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractError:
            return None  # no text detected (blank/logo-only page) -- nothing to orient against

    doc = pymupdf.open(pdf_path)
    fixed, unresolved = [], []
    for i in range(len(doc)):
        page = doc[i]
        base_rotation = page.rotation
        cov, bg = _page_image_coverage(page)
        if cov - bg <= 0.5 and _text_reads_upright(page):
            # the page's own text says it's upright; don't ask OSD. Not on a
            # page mostly covered by a (non-background) image: a pasted scan
            # printed sideways under an upright header still needs OSD.
            continue
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
    if fixed:
        doc.save(output_path)
        doc.close()
    else:
        # nothing corrected: pass the original bytes on, not a re-save. A
        # re-save of an unchanged document made the text engine emit nothing
        # for three pages of a real report (notes pages with ~3,000 chars of
        # text each) that it converts perfectly from the original file.
        doc.close()
        import shutil
        shutil.copyfile(pdf_path, output_path)
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


# Only images at least this share of the page are hidden (see
# hide_background_images): backgrounds are big (each strip of the test flyer's
# photo is 11% of the page); a signature with the signatory's name printed
# over it is 1-2%, and hiding one lost that name line on a real declaration.
HIDE_MIN_PAGE_SHARE = 0.05


def hide_background_images(doc):
    """Remove, in memory only, every image covering at least
    HIDE_MIN_PAGE_SHARE of the page with at least BACKGROUND_MIN_CHARS of
    visible text drawn over it; return how many. The text engine's
    layout model treats text over an image as "picture text" and flattens it
    line by line across the whole image -- confirmed on a grid flyer over a
    background photo, where five columns of day boxes came out interleaved
    and a line was lost. With the backgrounds gone the same model finds the
    grid and emits it as a table. Only the text engine's in-memory copy is
    touched: nothing is saved, and OCR engines still see the real page.
    Images without text over them (logos, icons, photos) stay."""
    import pymupdf
    hidden = 0
    for page in doc:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        try:
            images, spans, _ = _drawing_facts(page)
            page_area = page.rect.get_area()
            doomed = [r for r, sq in images
                      if (r & page.rect).get_area() >= HIDE_MIN_PAGE_SHARE * page_area
                      and _visible_chars_over(spans, r & page.rect, sq) >= BACKGROUND_MIN_CHARS]
            for r in doomed:
                page.add_redact_annot(r, fill=False)   # no box painted over the text
            if doomed:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                      text=pymupdf.PDF_REDACT_TEXT_NONE)
                hidden += len(doomed)
        except Exception:
            continue
        finally:
            if rot:
                page.set_rotation(rot)
    return hidden


# Icon-sized images: at most this much of the page, each side between these
# bounds (points), and no visible text drawn over them (that's a background).
ICON_MAX_PAGE_SHARE = 0.01
ICON_MIN_SIDE, ICON_MAX_SIDE = 8, 72
ICON_MAX_PER_DOC = 100


def find_icons(doc):
    """[(page_number, Rect)] for icon-sized images, in reading order."""
    import pymupdf
    icons = []
    for page in doc:
        rot = page.rotation
        if rot:
            page.set_rotation(0)
        try:
            images, spans, _ = _drawing_facts(page)
            area = page.rect.get_area()
            for r, sq in images:
                r = pymupdf.Rect(r & page.rect)
                if (not r.is_empty and r.get_area() <= ICON_MAX_PAGE_SHARE * area
                        and ICON_MIN_SIDE <= min(r.width, r.height)
                        and max(r.width, r.height) <= ICON_MAX_SIDE
                        and _visible_chars_over(spans, r, sq) == 0):
                    icons.append((page.number + 1, r))
        except Exception:
            continue
        finally:
            if rot:
                page.set_rotation(rot)
    icons.sort(key=lambda t: (t[0], round(t[1].y0), t[1].x0))
    return icons[:ICON_MAX_PER_DOC]


def extract_icons(pdf_path, out_dir):
    """Write each icon as a PNG (2x, for the vision model) into out_dir and
    return [{"page", "rect", "file"}]; see find_icons. The router sends them
    to a local vision model (this container has no network) and hands the
    labels back through --icon-labels."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    rows = []
    for i, (pno, r) in enumerate(find_icons(doc)):
        name = f"icon-{i:03d}.png"
        doc[pno - 1].get_pixmap(clip=r, dpi=144).save(os.path.join(out_dir, name))
        rows.append({"page": pno, "rect": [round(v, 2) for v in r], "file": name})
    doc.close()
    return rows


def insert_icon_labels(doc, labels):
    """Write each icon's label as INVISIBLE text (render mode 3) on the icon,
    in the text engine's in-memory copy, so the layout model files it with
    the text around it (a flyer's bus icon lands in its day's table cell).
    labels: [{"page", "rect", "label"}]; an empty label gives "[icon]".
    The icon image itself is dropped from that copy: the layout model files
    an image as a picture box and skips text inside one, so the label would
    vanish (confirmed on the flyer). The label stands in for the icon."""
    import pymupdf
    by_page = {}
    for row in labels:
        by_page.setdefault(row["page"], []).append(row)
    for pno, rows in by_page.items():
        page = doc[pno - 1]
        for row in rows:
            page.add_redact_annot(pymupdf.Rect(row["rect"]), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
        for row in rows:
            r = pymupdf.Rect(row["rect"])
            text = f"[icon: {row['label']}]" if row.get("label") else "[icon]"
            # sized to fit inside the icon: a label wider than its icon spills
            # into the next table cell and gets split there
            size = min(8, r.height * 0.8, r.width / max(pymupdf.get_text_length(text, fontsize=1), 1))
            page.insert_text((r.x0, r.y0 + r.height / 2 + size / 3), text,
                             fontsize=size, render_mode=3)


def to_markdown_text(pdf_path, hide_background=True, icon_labels=None):
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
    import pymupdf
    import pymupdf4llm
    doc = pymupdf.open(pdf_path)
    if hide_background:
        hide_background_images(doc)
    if icon_labels:
        insert_icon_labels(doc, icon_labels)
    chunks = pymupdf4llm.to_markdown(doc, use_ocr=False, page_chunks=True)
    plain_doc = pymupdf.open(pdf_path)  # second, independent tokenisation -- see repair_merged_spacing()

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
                block_text = repair_ligature_letters(
                    repair_merged_spacing(block_text, page_words), page_words)
            page_boxes.append({**box, "text": block_text,
                                "page_number": page_number,
                                "doc_pos": (offset + box["pos"][0], offset + box["pos"][1]) if box.get("pos") else None})
        repaired_text = split_merged_rows(repair_table_numbers(mark_undecodable(
            repair_ligature_letters(repair_merged_spacing(text, page_words), page_words)),
            page_words))
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
                    help="a page more than this fraction covered by embedded images counts "
                         "as needing OCR too, regardless of its text layer (background images, "
                         "with enough visible text drawn over them, excluded) -- confirmed a "
                         "real case (a pasted screenshot of a financial table, rotated, with an "
                         "auto-generated column-major OCR text layer that passed every other "
                         "check while being structurally unusable) (default 0.5)")
    ap.add_argument("--garbage-char-ratio", type=float, default=0.05,
                    help="a page whose extracted text is more than this fraction control "
                         "characters counts as needing OCR too -- a font subsetted without a "
                         "proper ToUnicode CMap looks like plenty of 'text' by character count but "
                         "is undecodable garbage; confirmed on a real corpus that clean pages "
                         "measure exactly 0.0 here, affected ones 5%%-76%%, so this default has a "
                         "wide safety margin (default 0.05)")
    ap.add_argument("--classify-only", action="store_true",
                    help="print 'digital' or 'scan' to stdout and exit (no model load); "
                         "used by pdf2md-auto.sh to route between the text/mineru engines")
    ap.add_argument("--classify-pages", action="store_true",
                    help="print a per-page JSON classification report and exit (no model "
                         "load). One fact-row per page: class 'text'|'ocr', reasons "
                         "(low_text/garbage_text/image_page), text_chars, image_coverage. "
                         "Used by pdf2md_route.py to route each page to the right engine "
                         "instead of forcing a whole-document choice -- the mixed-document "
                         "failure mode (a digital filing with a few scanned/pasted pages, "
                         "or an image-dense DTP document with a perfect text layer) is "
                         "exactly what whole-document routing cannot express.")
    ap.add_argument("--slice", metavar="A-B",
                    help="write pages A..B (1-indexed, inclusive) to the path given by -o "
                         "and exit. A pure page copy (no re-rendering, annotations and "
                         "resources preserved); used by pdf2md_route.py to hand each "
                         "same-class page run to its engine.")
    ap.add_argument("--derotate", metavar="OUTPUT.pdf",
                    help="detect per-page rotation via Tesseract OSD and write a corrected copy "
                         "to OUTPUT.pdf, then exit. Only the /Rotate flag is changed -- no pixel "
                         "or content is altered. Used by pdf2md-auto.sh ahead of every conversion.")
    ap.add_argument("--prepare", metavar="OUTPUT.pdf",
                    help="write the copy the engines convert -- text hidden under opaque "
                         "rectangles removed (cosmetic redactions, text under a panel), "
                         "ligature glyphs given their text back; see prepare_pdf -- print a "
                         "JSON report of what changed to stdout, and exit. Run by "
                         "pdf2md_route.py and pdf2md-auto.sh after --derotate.")
    ap.add_argument("--extract-icons", metavar="DIR",
                    help="write icon-sized images as PNGs into DIR, print a JSON list "
                         "(page, rect, file) to stdout, and exit; see extract_icons")
    ap.add_argument("--icon-labels", metavar="LABELS.json",
                    help="conversion: write these icon labels ([{page, rect, label}]) "
                         "into the text layer at each icon, as [icon: label]")
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

    if args.prepare:
        import json
        try:
            report = prepare_pdf(args.input, args.prepare)
        except Exception as e:
            err(f"[pdf2md] ERROR during --prepare: {e}")
            sys.exit(6)
        for r in report["pages"]:
            log(f"[pdf2md] --prepare: page {r['page']}: removed {r['covered_chars']} chars of "
                f"text hidden under opaque shapes ({r['redaction_markers']} redaction marker(s))")
        for f in report["ligature_fixes"]:
            log(f"[pdf2md] --prepare: font {f['font']}: gave {len(f['glyphs'])} ligature "
                f"glyph(s) their text back ({', '.join(sorted(set(f['glyphs'].values())))})")
        print(json.dumps(report))
        return

    if args.extract_icons:
        import json
        try:
            print(json.dumps(extract_icons(args.input, args.extract_icons)))
        except Exception as e:
            err(f"[pdf2md] ERROR during --extract-icons: {e}")
            sys.exit(6)
        return

    if args.slice:
        if not args.output:
            err("[pdf2md] ERROR: --slice requires -o OUTPUT.pdf")
            sys.exit(2)
        try:
            a, b = (int(x) for x in args.slice.split("-", 1))
            import pymupdf
            src = pymupdf.open(args.input)
            if not (1 <= a <= b <= len(src)):
                err(f"[pdf2md] ERROR: --slice {args.slice} out of range (1-{len(src)})")
                sys.exit(2)
            dst = pymupdf.open()
            dst.insert_pdf(src, from_page=a - 1, to_page=b - 1)
            dst.save(args.output)
            log(f"[pdf2md] --slice: wrote pages {a}-{b} -> {args.output}")
        except SystemExit:
            raise
        except Exception as e:
            err(f"[pdf2md] ERROR during --slice: {e}")
            sys.exit(6)
        return

    try:
        pc, needs_ocr, total_chars, garbage_pages, image_pages, per_page = classify(
            args.input, args.min_page_chars, args.garbage_char_ratio, args.image_coverage_threshold)
    except Exception as e:
        err(f"[pdf2md] ERROR opening/classifying PDF: {e}")
        sys.exit(2)

    if args.classify_pages:
        import json
        print(json.dumps({"pages": pc, "total_chars": total_chars,
                          "per_page": per_page}, indent=1))
        return

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
            icon_labels = None
            if args.icon_labels:
                import json
                with open(args.icon_labels, encoding="utf-8") as f:
                    icon_labels = json.load(f)
            out, page_boxes = to_markdown_text(args.input, icon_labels=icon_labels)
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
