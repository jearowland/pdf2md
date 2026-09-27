#!/usr/bin/env python3
"""
uat.py -- regression run of pdf2md against a PRIVATE list of verified cases.

Every fixed conversion defect should stay fixed. The cases are real
documents, so they never live in this repo: the list is a JSONL file kept
wherever the documents are, and this runner only needs its path. One case
per line:

  {"id": "...", "pdf": "/abs/path.pdf", "pages": [12],
   "defect": "one line: what went wrong before the fix",
   "expect": {"engine_per_page": {"12": "mineru"},     # optional
              "must_contain": ["..."],                  # optional
              "must_not_contain": ["..."]},             # optional
   "router_args": ["--alt-text-ollama", "http://localhost:11434"]}  # optional

Quote marks are compared loosely (curly and straight are the same), since
engines differ there legitimately: the text engine keeps the PDF's own
character, MinerU straightens it. Everything else is exact -- dashes above
all, since a nil printed as U+2010 is exactly the kind of thing a case pins.

Checks are page-scoped: strings are looked for in the output of the case's
pages only (page N starts at "<!-- page N -->"; text before the first marker
is page 1), so a value that turns up on another page doesn't mask a loss.
Engines come from the router's manifest. The manifest's warnings are
reported with each case but don't decide pass or fail.

Two tiers:
  --classify-only   CPU only, seconds per document: classify each page and
                    plan the router's runs, then check engine_per_page. Run
                    on core after any classifier or routing change.
  (default)         full conversion through pdf2md_route.py, then every
                    check. Needs MinerU for OCR pages, so on core run it
                    against a GPU worker:
                      gpu run --kind convert --needs docker -- \\
                        tools/uat.py CASES.jsonl --remote '$GPU_HOST'
                    The worker's ~/pdf2md must be at this checkout's commit.

Source PDFs are copied into the run folder (default tmp/uat/<commit>/) and
never written beside; results.json there records every check. Exit status
is 1 when any case fails.

Usage:
  tools/uat.py CASES.jsonl --classify-only [--dev-bind]
  tools/uat.py CASES.jsonl [--remote HOST] [--ids a,b] [--out DIR]
"""
from __future__ import annotations
import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import pdf2md_route as route  # noqa: E402  (plan_runs, docker_text, markers)


QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def git(*a: str, cwd: Path = REPO) -> str:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True,
                          text=True).stdout.strip()


def page_segments(md: str) -> dict[int, str]:
    segs: dict[int, list[str]] = {1: []}
    page = 1
    for line in md.splitlines():
        m = route.PAGE_MARKER_RE.match(line.strip())
        if m:
            page = int(m.group(1))
            segs.setdefault(page, [])
            continue
        segs.setdefault(page, []).append(line)
    return {p: "\n".join(v) for p, v in segs.items()}


def planned_engines(pdf: Path, dev_bind: bool) -> dict[int, str]:
    """Engine per page as the router would choose it, without converting."""
    r = route.docker_text(pdf.parent, [f"/work/{pdf.name}", "--classify-pages", "--quiet"],
                          dev_bind, capture=True)
    per_page = route.parse_json_report(r.stdout)["per_page"]
    engines = {}
    for a, b, eng in route.plan_runs(per_page, route.WHOLE_DOC_OCR_RATIO, route.MAX_RUNS):
        for p in range(a, b + 1):
            engines[p] = eng
    return engines


REMOTE_MINERU_SERVER = ""   # set by main: one MinerU server for the whole remote run


def convert(pdf: Path, out_md: Path, remote: str | None, dev_bind: bool,
            extra: list[str]) -> None:
    """Run the router locally, or on `remote` with the output copied back."""
    if not remote:
        cmd = [sys.executable, str(REPO / "pdf2md_route.py"), str(pdf), "-o", str(out_md), *extra]
        if dev_bind:
            cmd.append("--dev-bind")
        subprocess.run(cmd, check=True)
        return
    rdir = f"pdf2md/tmp/uat-run/{pdf.stem}"
    q = shlex.quote
    subprocess.run(["ssh", remote, f"rm -rf {q(rdir)} && mkdir -p {q(rdir)}"], check=True)
    subprocess.run(["scp", "-q", str(pdf), f"{remote}:{rdir}/"], check=True)
    subprocess.run(["ssh", remote,
                    f"cd ~/pdf2md && "
                    + (f"PDF2MD_MINERU_SERVER={q(REMOTE_MINERU_SERVER)} " if REMOTE_MINERU_SERVER else "")
                    + f"python3 pdf2md_route.py "
                    f"tmp/uat-run/{q(pdf.stem)}/{q(pdf.name)} "
                    f"-o tmp/uat-run/{q(pdf.stem)}/{q(out_md.name)} "
                    + " ".join(q(a) for a in extra)], check=True)
    man = out_md.with_suffix(".manifest.json").name
    subprocess.run(["scp", "-q", f"{remote}:{rdir}/{out_md.name}",
                    f"{remote}:{rdir}/{man}", str(out_md.parent)], check=True)
    subprocess.run(["ssh", remote, f"rm -rf {q(rdir)}"], check=False)


def check_case(case: dict, run_dir: Path, args) -> dict:
    cid = case["id"]
    exp = case.get("expect", {})
    work = run_dir / cid
    work.mkdir(parents=True, exist_ok=True)
    pdf = work / f"{cid}.pdf"
    shutil.copyfile(case["pdf"], pdf)
    fails: list[str] = []
    result = {"id": cid, "defect": case.get("defect", ""), "pages": case["pages"]}

    want_eng = {int(k): v for k, v in exp.get("engine_per_page", {}).items()}
    if args.classify_only:
        got_eng = planned_engines(pdf, args.dev_bind)
        warnings = []
    else:
        out_md = work / f"{cid}.md"
        try:
            convert(pdf, out_md, args.remote, args.dev_bind, case.get("router_args", []))
        except subprocess.CalledProcessError as e:
            result.update(status="FAIL", failures=[f"conversion failed: {e}"])
            return result
        man = json.loads(out_md.with_suffix(".manifest.json").read_text())
        got_eng = {p["page"]: p["engine"] for p in man["per_page"]}
        warnings = man.get("warnings", [])
        segs = page_segments(out_md.read_text(encoding="utf-8", errors="replace"))
        scoped = "\n".join(segs.get(p, "") for p in case["pages"]).translate(QUOTES)
        fails += [f"missing on pages {case['pages']}: {s!r}"
                  for s in exp.get("must_contain", []) if s.translate(QUOTES) not in scoped]
        fails += [f"present on pages {case['pages']}: {s!r}"
                  for s in exp.get("must_not_contain", []) if s.translate(QUOTES) in scoped]
    fails += [f"page {p}: engine {got_eng.get(p)}, expected {e}"
              for p, e in sorted(want_eng.items()) if got_eng.get(p) != e]
    result.update(status="FAIL" if fails else "PASS", failures=fails,
                  engines={p: got_eng.get(p) for p in case["pages"]},
                  warnings=[w for w in warnings
                            if w.get("page") in case["pages"] or "page" not in w])
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", help="JSONL case list (kept outside this repo)")
    ap.add_argument("--classify-only", action="store_true",
                    help="CPU tier: check routing only, no conversion")
    ap.add_argument("--remote", metavar="HOST",
                    help="convert on this worker (its ~/pdf2md at this commit)")
    ap.add_argument("--ids", help="comma-separated case ids to run (default: all)")
    ap.add_argument("--out", help="run folder (default tmp/uat/<commit>[-dirty])")
    ap.add_argument("--no-mineru-server", action="store_true",
                    help="remote runs: one-shot MinerU containers per run, as before "
                         "(default: one MinerU server for the whole run)")
    ap.add_argument("--dev-bind", action="store_true",
                    help="local runs only: overlay this checkout's text-engine "
                         "scripts on the baked image (see pdf2md_route.py)")
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(args.cases, encoding="utf-8") if l.strip()]
    if args.ids:
        keep = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in keep]
    head = git("rev-parse", "--short", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    if args.remote and not args.classify_only:
        if args.dev_bind:
            sys.exit("[uat] --dev-bind applies to local runs only")
        rhead = subprocess.run(["ssh", args.remote, "git -C ~/pdf2md rev-parse --short HEAD"],
                               capture_output=True, text=True).stdout.strip()
        if rhead != head or dirty:
            sys.exit(f"[uat] {args.remote} is at {rhead or '?'}, this checkout at {head}"
                     f"{' with uncommitted changes' if dirty else ''}: commit, push "
                     f"and pull first, so the run tests what you think it does")
    run_dir = Path(args.out).resolve() if args.out else \
        REPO / "tmp" / "uat" / (head + ("-dirty" if dirty else ""))
    run_dir.mkdir(parents=True, exist_ok=True)

    tier = "classify-only" if args.classify_only else f"full ({args.remote or 'local'})"
    print(f"[uat] {len(cases)} case(s), commit {head}{'-dirty' if dirty else ''}, "
          f"tier {tier} -> {run_dir}", file=sys.stderr)
    global REMOTE_MINERU_SERVER
    if args.remote and not args.classify_only and not args.no_mineru_server:
        # one MinerU for the whole run: models load once, not once per case
        REMOTE_MINERU_SERVER = subprocess.run(
            ["ssh", args.remote, "cd ~/pdf2md && tools/mineru-session start"],
            check=True, capture_output=True, text=True).stdout.strip()
        print(f"[uat] MinerU server on {args.remote}: {REMOTE_MINERU_SERVER}", file=sys.stderr)
    results = []
    try:
        for c in cases:
            results.append(run_one(c, run_dir, args))
    finally:
        if REMOTE_MINERU_SERVER:
            subprocess.run(["ssh", args.remote,
                            f"cd ~/pdf2md && tools/mineru-session stop {shlex.quote(REMOTE_MINERU_SERVER)}"],
                           check=False)
    (run_dir / "results.json").write_text(json.dumps(
        {"commit": head, "dirty": dirty, "tier": tier, "results": results}, indent=1))
    n_fail = sum(r["status"] == "FAIL" for r in results)
    print(f"[uat] {len(results) - n_fail} passed, {n_fail} failed", file=sys.stderr)
    sys.exit(1 if n_fail else 0)


def run_one(c, run_dir, args):
    """One case, reported as it finishes; its result with wall time."""
    import time
    t0 = time.time()
    r = check_case(c, run_dir, args)
    r["secs"] = round(time.time() - t0, 1)
    print(f"{r['status']}  {r['id']}  ({r['secs']}s)", flush=True)
    for f in r.get("failures", []):
        print(f"      {f}", flush=True)
    for w in r.get("warnings", []):
        print(f"      warning: {w.get('kind')} page {w.get('page', '-')}", flush=True)
    return r


if __name__ == "__main__":
    main()
