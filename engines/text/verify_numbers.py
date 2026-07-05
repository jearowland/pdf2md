#!/usr/bin/env python3
"""
verify_numbers.py — post-conversion completeness check: did the markdown
keep the numbers the PDF's text layer contains?

Motivation: audited against a large real-world corpus with external
ground truth, roughly one in ten "value absent from the markdown" cases
turned out to have the value present in the PDF's text layer — the
conversion dropped it. Losses concentrate in small, oddly-formatted
tables late in documents (appendix-style notes), where layout models
drop blocks they can't place; primary statement tables are rarely
affected. This check makes that class of loss visible at conversion
time instead of later by forensic audit.

Method: extract the set of MONEY-LIKE numbers (comma-grouped, >= 4 digits
— deliberately excludes page numbers, note references, years) from each
PDF page's text layer and from the markdown; report coverage and the
per-page location of anything missing. Only meaningful when the PDF has
a text layer at all (scanned documents have no reference to check
against — reported as 'no text layer', not as success). A garbled text
layer (e.g. column-major OCR relics) still works as a RECALL reference:
the digits are digits regardless of reading order.

Output: one machine-parsable summary line on stdout; per-page misses on
stderr. Exit 0 always — this is a reporter, not a gate; pdf2md-auto.sh
decides what to do with the result (see --min-coverage there for the
recovery hook).

Usage:
  python3 verify_numbers.py document.pdf output.md
  python3 verify_numbers.py document.pdf output.md --json   # full detail
"""
from __future__ import annotations
import argparse
import json
import re
import sys

import fitz  # PyMuPDF

# money-like: comma-grouped with >= 4 significant digits ("1,234",
# "12,345,678") — the formatting financial statements actually use.
# Plain runs of digits are deliberately NOT matched: they're note refs,
# years, ABNs, page numbers.
MONEY_RE = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])")


def money_numbers(text: str) -> set[str]:
    # normalise away decimals for comparison ("1,234.56" vs "1,234")
    return {m.group(0).split(".")[0] for m in MONEY_RE.finditer(text)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="source PDF")
    ap.add_argument("md", help="converted markdown to verify")
    ap.add_argument("--json", action="store_true",
                    help="emit full JSON detail (per-page misses) on stdout")
    args = ap.parse_args()

    with open(args.md, encoding="utf-8", errors="replace") as f:
        md_numbers = money_numbers(f.read())

    doc = fitz.open(args.pdf)
    page_numbers: dict[int, set[str]] = {}
    for i, page in enumerate(doc, start=1):
        nums = money_numbers(page.get_text())
        if nums:
            page_numbers[i] = nums
    doc.close()

    pdf_numbers = set().union(*page_numbers.values()) if page_numbers else set()

    if not pdf_numbers:
        print("[verify] no text layer numbers to check against (scanned or image-only PDF)")
        return

    missing = pdf_numbers - md_numbers
    covered = len(pdf_numbers) - len(missing)
    pct = 100 * covered / len(pdf_numbers)
    missing_pages = sorted({pg for pg, nums in page_numbers.items()
                            if nums & missing})

    print(f"[verify] text-layer number coverage: {pct:.0f}% "
          f"({covered}/{len(pdf_numbers)})"
          + (f" -- {len(missing)} missing, on page(s) "
             f"{','.join(map(str, missing_pages))}" if missing else ""))

    if missing:
        for pg in missing_pages:
            lost = sorted(page_numbers[pg] & missing)
            print(f"[verify]   page {pg}: {', '.join(lost[:8])}"
                  + (" ..." if len(lost) > 8 else ""), file=sys.stderr)

    if args.json:
        print(json.dumps({
            "coverage_pct": round(pct, 1),
            "pdf_numbers": len(pdf_numbers),
            "covered": covered,
            "missing": sorted(missing),
            "missing_pages": missing_pages,
        }))


if __name__ == "__main__":
    main()
