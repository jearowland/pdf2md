#!/usr/bin/env python3
"""
common.py — shared CLI/output/logging boilerplate for pdf2md's structured-
document engines (docx2md.py, xlsx2md.py). Guarantees they share an
identical CLI contract with each other: same -o/--output handling, same
--quiet convention, same exit codes, same stdout/stderr discipline
(routing/timing logs to stderr, clean markdown to stdout only) -- so
pdf2md-auto.sh and any calling pipeline invoke every engine the same way
regardless of source format.

pdf2md.py predates this module and has PDF-specific concerns (derotation,
classify) that don't apply to docx/xlsx, so it isn't refactored onto this
shared base -- but its output-writing/timing/logging conventions (exit code
4 on conversion failure, same log message shapes) are matched here
deliberately, not coincidentally.
"""
import argparse
import sys
import time


def err(*a):
    print(*a, file=sys.stderr, flush=True)


def run(prog: str, description: str, convert_fn, extra_args=None):
    """convert_fn(args, log) -> markdown string. extra_args(ap), if given,
    adds engine-specific flags to the parser before parsing."""
    ap = argparse.ArgumentParser(prog=prog, description=description)
    ap.add_argument("input", help="input file path")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--quiet", action="store_true", help="suppress stderr routing logs")
    if extra_args:
        extra_args(ap)
    args = ap.parse_args()

    def log(*a):
        if not args.quiet:
            err(*a)

    t0 = time.time()
    try:
        out = convert_fn(args, log)
    except Exception as e:
        err(f"[{prog}] ERROR during conversion: {e}")
        sys.exit(4)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        log(f"[{prog}] wrote {args.output} ({len(out)} chars) in {time.time()-t0:.1f}s")
    else:
        sys.stdout.write(out)
        log(f"[{prog}] done in {time.time()-t0:.1f}s ({len(out)} chars)")
