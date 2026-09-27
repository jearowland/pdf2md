#!/usr/bin/env python3
"""
pdf2md_batch.py -- convert many PDFs as ONE job, keeping a GPU worker busy.

Converting documents one at a time left most of a worker idle: one small
MinerU request at a time (often a single page, each paying model start-up)
and one CPU container at a time on a machine with dozens of threads. A job
here runs in stages across ALL its documents, each stage using the machine
in full, with worker counts sized from the machine itself (CPU threads,
free RAM), so a smaller worker scales down on its own:

  A. analyze (CPU, parallel): derotate, prepare, classify, plan, slice, crop
     icons -- the same pdf2md_route.analyze as a single conversion.
  B. OCR (GPU): every document's OCR runs in ONE MinerU container, one MinerU
     call per backend over them all (engines/mineru/mineru.sh --batch), so
     models load once and vLLM batches pages across documents. Meanwhile,
     text-engine runs that don't need icon labels convert in parallel on the
     CPU threads and RAM MinerU leaves free.
  C. icons (GPU, once): MinerU has exited, so the card is free; every icon of
     the job is labelled in one pass, then the vision model is unloaded.
  D. the rest (CPU, parallel): text runs with icon labels, then each
     document's merge, manifest and checks (pdf2md_route.finalize).

Each document's output is exactly what pdf2md_route.py would write for it
alone: the same stages, the same engines, the same finishing code.

Usage:
  pdf2md_batch.py a.pdf b.pdf ...            # each writes <stem>.md beside itself
  pdf2md_batch.py DIR                        # every *.pdf in DIR
  pdf2md_batch.py DIR --alt-text-ollama http://localhost:11434
Exit 1 if any document failed (the others are still written).
"""
from __future__ import annotations
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import pdf2md_route as route  # noqa: E402

TEXT_WORKER_MB = 1500      # one text-engine container's working set, with headroom
# RAM set aside for the MinerU container while CPU workers share the machine
# with it: its measured peak was 5.8 GB (its hard cap, 20 GB, is a limit, not
# its use -- reserving the cap left most CPU threads idle during OCR)
MINERU_MB = 8 * 1024
RESERVE_MB = 2048          # left for the host itself


def mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 4096


def cpu_workers(reserve_mb: int = 0) -> int:
    """As many parallel text-engine containers as the machine has CPU threads,
    capped by the RAM available after reserve_mb."""
    by_ram = (mem_available_mb() - RESERVE_MB - reserve_mb) // TEXT_WORKER_MB
    return max(1, min(os.cpu_count() or 1, by_ram))


def run_pool(fn, items, workers: int, label: str) -> list:
    """fn over items on `workers` threads (each drives docker containers,
    each container given its share of the CPU threads); returns the items
    that failed, after logging why."""
    route.CONTAINER_THREADS = max(1, (os.cpu_count() or 1) // max(workers, 1))
    failed = []

    def one(item):
        try:
            fn(item)
        except BaseException as e:   # SystemExit from a stage counts too
            failed.append(item)
            route.err(f"[batch] {label} failed: {e!r}")
            traceback.print_exc()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, items))
    return failed


def gpu_stage(docs: list[dict], job: Path) -> set[str]:
    """Every document's OCR runs through one MinerU container; each output
    lands where the single-document path puts it (the run's chunk .md, its
    content_list.json, images/<chunk stem>/). Returns the inputs of documents
    whose OCR failed."""
    ocr = [(d, r) for d in docs for r in d["runs"] if r[2] == "mineru"]
    if not ocr:
        return set()
    in_dir = job / "in"
    in_dir.mkdir(parents=True)
    names = {}
    for i, (d, (a, b, _)) in enumerate(ocr):
        piece_pdf, piece_md = route.piece_paths(d, a, b)
        name = f"{i:04d}"          # unique across documents that share stems
        shutil.copyfile(piece_pdf, in_dir / f"{name}.pdf")
        names[name] = (d, a, b, piece_md)
    route.err(f"[batch] GPU: {len(ocr)} OCR run(s) from "
              f"{len({id(d) for d, _ in ocr})} document(s) in one MinerU container")
    t = time.time()
    subprocess.run([str(route.MINERU_SH), "--batch", str(job)], check=False)
    failed = set()
    out = job / "out"
    for name, (d, a, b, piece_md) in names.items():
        md = out / f"{name}.md"
        if not md.exists():
            failed.add(d["input"])
            continue
        chunk_stem = piece_md.stem
        text = md.read_text(encoding="utf-8").replace(f"](images/{name}/", f"](images/{chunk_stem}/")
        piece_md.write_text(text, encoding="utf-8")
        for suffix in (".content_list.json", ".middle.json"):
            if (out / f"{name}{suffix}").exists():
                shutil.copyfile(out / f"{name}{suffix}", d["workdir"] / f"{chunk_stem}{suffix}")
        if (out / "images" / name).is_dir():
            dest = d["workdir"] / "images" / chunk_stem
            dest.mkdir(parents=True, exist_ok=True)
            for f in (out / "images" / name).iterdir():
                shutil.copyfile(f, dest / f.name)
        d["chunks"][(a, b)] = {"engine": "mineru", "secs": round(time.time() - t, 1),
                               "batched": len(ocr)}
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="PDF files, or directories of them")
    route.add_options(ap)
    opts = ap.parse_args()

    pdfs = []
    for p in map(Path, opts.inputs):
        if not p.exists():
            sys.exit(f"[batch] no such file or directory: {p}")
        pdfs += sorted(p.glob("*.pdf")) if p.is_dir() else [p]
    # a directory may hold earlier runs' artifacts; convert originals only
    pdfs = [p.resolve() for p in pdfs
            if not re.search(r"\.(derotated|prepared)\.|\.p\d{4}-\d{4}\.pdf$", p.name)]
    if not pdfs:
        sys.exit("[batch] no PDFs given")
    t0 = time.time()
    route.err(f"[batch] {len(pdfs)} document(s); {os.cpu_count()} CPU threads, "
              f"{mem_available_mb()} MB RAM available")

    # A. analyze, all documents in parallel
    docs: list[dict] = []
    lock = threading.Lock()

    def analyze(pdf: Path):
        d = route.analyze(str(pdf), str(pdf.with_suffix(".md")), opts)
        with lock:
            docs.append(d)
    failed = {str(p) for p in run_pool(analyze, pdfs, cpu_workers(), "analyze")}

    # B. OCR on the GPU, alongside the text runs that need no icon labels
    def needs_labels(d, run):
        a, b, _ = run
        return any(a <= i["page"] <= b for i in d["icons"])
    text_now = [(d, r) for d in docs for r in d["runs"] if r[2] == "text" and not needs_labels(d, r)]
    text_later = [(d, r) for d in docs for r in d["runs"] if r[2] == "text" and needs_labels(d, r)]
    with tempfile.TemporaryDirectory(prefix="pdf2md-batch-", dir=REPO / "tmp") as job:
        gpu_failed: set[str] = set()

        def gpu():
            gpu_failed.update(gpu_stage(docs, Path(job)))
        g = threading.Thread(target=gpu)
        g.start()
        workers = cpu_workers(reserve_mb=MINERU_MB if any(r[2] == "mineru" for d in docs
                                                             for r in d["runs"]) else 0)
        route.err(f"[batch] CPU: {len(text_now)} text run(s) on {workers} worker(s) during OCR")
        failed |= {d["input"] for d, _ in run_pool(lambda dr: route.convert_text_run(dr[0], dr[1], opts),
                                                   text_now, workers, "text run")}
        # documents that need nothing from the GPU finish while it works
        gpu_free = [d for d in docs if d["input"] not in failed
                    and not any(r[2] == "mineru" for r in d["runs"]) and not d["icons"]]
        failed |= {d["input"] for d in run_pool(lambda d: route.finalize(d, opts),
                                                gpu_free, workers, "finalize")}
        finished = {d["input"] for d in gpu_free}
        g.join()
        failed |= gpu_failed

    # C. every icon of the job labelled in one pass (MinerU has exited)
    if opts.alt_text_ollama:
        route.label_all_icons([d for d in docs if d["input"] not in failed], opts)

    # D. text runs that needed labels, then finish every document
    workers = cpu_workers()
    failed |= {d["input"] for d, _ in run_pool(lambda dr: route.convert_text_run(dr[0], dr[1], opts),
                                               [x for x in text_later if x[0]["input"] not in failed],
                                               workers, "text run")}
    failed |= {d["input"] for d in run_pool(lambda d: route.finalize(d, opts),
                                           [d for d in docs if d["input"] not in failed
                                            and d["input"] not in finished],
                                           workers, "finalize")}
    route.err(f"[batch] {len(pdfs) - len(failed)} of {len(pdfs)} document(s) converted "
              f"in {time.time() - t0:.0f}s" + (f"; failed: {sorted(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
