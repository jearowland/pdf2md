#!/usr/bin/env python3
"""
verify_text.py — post-conversion completeness check: did the markdown keep
the WORDS each PDF page's text layer contains?

Motivation: a layout engine can drop whole blocks of a page and say nothing.
Confirmed on a one-page grid flyer with a complete text layer: MinerU emitted
6 of its 10 day boxes and lost the title, with no error and nothing in the
output to show anything was missing. verify_numbers can't see that kind of
loss (there are no money-like numbers in prose); this check can. It matters
most on pages that go to OCR despite having a healthy text layer (an image
page, a designed page over a background photo), since there the engine
ignores a perfect reference that we still have.

Method: per page, the multiset of words (letters only, 3+ chars, Unicode
NFKC, case-folded) in the text layer is the reference. Recall is the share
of those word occurrences found in the markdown for that page and its two
neighbours (engines move a block across a page boundary now and then, which
is not a loss). A markdown without page markers is compared whole. Pages
with a thin text layer (< --min-chars) or a garbled one (control characters,
see pdf2md.py's CONTROL_CHAR_RE) are 'unverifiable', never failures.

Word recall, not character or string match: reading order, line breaks,
tables vs prose and markdown syntax all differ legitimately between engines;
which words survived does not. Occurrence counts, not distinct words, so a
dropped block of common words still registers. OCR misreads cost a little
recall on a correct conversion; the default --min-recall leaves room for that
(and for ligatures a PDF's own text layer encodes badly).

Output: one summary line on stdout (or --json detail); per-page warnings on
stderr. Exit 0 always -- a reporter, not a gate. pdf2md_route.py copies the
JSON warnings into <out>.manifest.json (kind 'text_layer_content_missing').

Usage:
  python3 verify_text.py document.pdf output.md
  python3 verify_text.py document.pdf output.md --json
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import unicodedata
from collections import Counter

import pymupdf

PAGE_MARKER_RE = re.compile(r"<!-- page (\d+) -->")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # as pdf2md.py
WORD_RE = re.compile(r"[^\W\d_]{3,}")
TAG_RE = re.compile(r"<[^>]*>")


def words(text: str) -> Counter:
    text = unicodedata.normalize("NFKC", TAG_RE.sub(" ", text)).casefold()
    return Counter(WORD_RE.findall(text))


def md_segments(md: str) -> dict[int, str] | None:
    """Markdown per page. Markers sit BETWEEN pages, so text before the first
    marker is page 1. None when there are no markers (compare whole)."""
    if not PAGE_MARKER_RE.search(md):
        return None
    segs: dict[int, list[str]] = {1: []}
    page = 1
    for line in md.splitlines():
        m = PAGE_MARKER_RE.match(line.strip())
        if m:
            page = int(m.group(1))
            segs.setdefault(page, [])
            continue
        segs.setdefault(page, []).append(line)
    return {p: "\n".join(v) for p, v in segs.items()}


def recall(ref: Counter, got: Counter) -> float:
    total = sum(ref.values())
    return sum(min(n, got[w]) for w, n in ref.items()) / total if total else 1.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="source PDF")
    ap.add_argument("md", help="converted markdown to verify")
    ap.add_argument("--min-recall", type=float, default=0.8,
                    help="warn when a page keeps less than this share of its "
                         "text-layer words (default 0.8)")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="pages with a thinner text layer are unverifiable (default 200)")
    ap.add_argument("--json", action="store_true",
                    help="emit full JSON detail (per-page recall, warnings) on stdout")
    args = ap.parse_args()

    with open(args.md, encoding="utf-8", errors="replace") as f:
        md = f.read()
    segs = md_segments(md)
    seg_words = {p: words(t) for p, t in segs.items()} if segs else None
    whole = words(md) if segs is None else None

    doc = pymupdf.open(args.pdf)
    per_page, warnings = [], []
    for i, page in enumerate(doc, start=1):
        txt = page.get_text("text")
        n = len(txt.strip())
        if n < args.min_chars or \
                len(CONTROL_CHAR_RE.findall(txt)) / max(len(txt), 1) > 0.05:
            per_page.append({"page": i, "recall": None})
            continue
        ref = words(txt)
        if seg_words is None:
            got = whole
        else:
            got = Counter()
            for p in (i - 1, i, i + 1):
                got |= seg_words.get(p, Counter())
        r = recall(ref, got)
        per_page.append({"page": i, "recall": round(r, 3)})
        if r < args.min_recall:
            # words gone entirely first: they point at what was dropped
            missing = sorted((w for w, c in ref.items() if got[w] < c),
                             key=lambda w: got[w] > 0)
            warnings.append({
                "kind": "text_layer_content_missing", "page": i,
                "recall": round(r, 3),
                "detail": f"output keeps {r:.0%} of the page's text-layer words "
                          f"(threshold {args.min_recall:.0%}); the engine may have "
                          f"dropped blocks",
                "missing_sample": missing[:12]})
            print(f"[verify_text] page {i}: {r:.0%} of text-layer words kept "
                  f"-- content may be missing", file=sys.stderr)
    doc.close()

    checked = [p["recall"] for p in per_page if p["recall"] is not None]
    if args.json:
        print(json.dumps({"pages": len(per_page), "pages_checked": len(checked),
                          "per_page": per_page, "warnings": warnings}, indent=1))
    elif checked:
        print(f"[verify_text] {len(checked)}/{len(per_page)} page(s) checkable; "
              f"min recall {min(checked):.0%}; {len(warnings)} page(s) below "
              f"{args.min_recall:.0%}")
    else:
        print(f"[verify_text] no page has a usable text layer -- unverifiable")


if __name__ == "__main__":
    main()
