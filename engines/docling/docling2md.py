#!/usr/bin/env python3
"""
docling2md.py -- Docling engine for pdf2md (added 2026-07-10). Bytes-in,
Markdown-out, same calling convention as engines/text/pdf2md.py and
engines/mineru/mineru2md.py: no domain-specific logic here, that belongs
in the pipeline calling this tool (see the project README's own rule).

Status: direct-call only, NOT YET containerized like the other two
engines. Docling is a pure-Python package (no baked model weights the
way MinerU's ~43GB image has), so it runs fine directly in a venv --
added this way first to validate whether it's worth keeping around
before spending the effort on a Dockerfile + GPU passthrough. Evaluated
2026-07-10 against a 100-doc real-fixture sample from a downstream
pipeline's corpus: comparable table structure to the existing engines,
genuine Markdown pipe tables (not HTML), no fabrication pattern found --
but 3 silent whole-statement-omission cases in 100 (no error raised),
so treat it the same way as the other two: not to be trusted unattended
without a completeness check downstream.

  python3 engines/docling/docling2md.py INPUT.pdf -o OUT.md
  python3 engines/docling/docling2md.py INPUT.pdf          # markdown to stdout
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def convert(input_path: Path) -> str:
    from docling.document_converter import DocumentConverter
    conv = DocumentConverter()
    result = conv.convert(str(input_path))
    return result.document.export_to_markdown()


def main():
    ap = argparse.ArgumentParser(prog="docling2md", description="PDF to Markdown via Docling.")
    ap.add_argument("input", help="input PDF path")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--quiet", action="store_true", help="suppress stderr routing logs")
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"[docling2md] input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        print(f"[docling2md] converting {input_path}", file=sys.stderr)

    try:
        md = convert(input_path)
    except Exception as e:
        print(f"[docling2md] conversion failed: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        if not args.quiet:
            print(f"[docling2md] wrote {len(md)} chars -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
