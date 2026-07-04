#!/usr/bin/env python3
"""
handler.py — RunPod Serverless adapter for pdf2md. Receives one document
per job, runs the SAME routing pipeline as pdf2md-auto.sh (derotate ->
classify -> text-or-MinerU), returns the Markdown. Contains no knowledge of
any particular caller's domain -- bytes in, Markdown out, same contract as
the CLI.

Job input (either transport, decided per-job):
  {"filename": "report.pdf", "content_base64": "..."}          # inline
  {"filename": "report.pdf", "url": "https://..."}             # presigned/public URL

Job output:
  {"markdown": "...",                       # the converted document
   "engine": "text" | "mineru",             # which engine actually ran
   "classify": "digital" | "scan" | "forced" | "n/a",
   "images_base64": {"name.jpg": "..."},    # any images MinerU preserved (empty for text engine)
   "content_list_json": "..." | null,       # MinerU's provenance sidecar, when produced
   "log": "..."}                            # stage-by-stage stderr, for diagnosis

Optional job inputs: {"engine": "text"|"mineru"} to force routing,
{"no_derotate": true} to skip the rotation check, {"no_reconcile_spelling":
true} to skip MinerU's reference pass (halves GPU time per scanned doc at
the cost of the rare-proper-noun protection).

Local smoke test (no RunPod SDK/account needed):
  docker run --rm --gpus all --shm-size 32g --ipc=host \
    -v /path/to/docs:/work --entrypoint python3 pdf2md-serverless \
    /usr/local/bin/handler.py --test /work/report.pdf
"""
from __future__ import annotations
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

PDF2MD = ["python3", "/usr/local/bin/pdf2md.py"]
DOCX2MD = ["python3", "/usr/local/bin/docx2md.py"]
XLSX2MD = ["python3", "/usr/local/bin/xlsx2md.py"]
MINERU2MD = ["python3", "/usr/local/bin/mineru2md.py"]


def _run(cmd: list[str], log: list[str]) -> subprocess.CompletedProcess:
    log.append(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stderr:
        log.append(proc.stderr.strip())
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[-1]} exited {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout or '').strip()[-800:]}")
    return proc


def convert(doc_path: str, engine: str | None, no_derotate: bool,
            no_reconcile_spelling: bool, log: list[str]) -> dict:
    """The pdf2md-auto.sh pipeline, as direct subprocess calls (no docker --
    everything this needs is baked into this one image). Returns the job
    output dict, minus the log (caller attaches it)."""
    workdir = os.path.dirname(doc_path)
    stem, ext = os.path.splitext(os.path.basename(doc_path))
    ext = ext.lower().lstrip(".")
    out_md = os.path.join(workdir, f"{stem}.md")

    # docx/xlsx: structured text-native formats -- no classify/derotate
    # concepts apply (same reasoning as pdf2md-auto.sh).
    if ext == "docx":
        _run(DOCX2MD + [doc_path, "-o", out_md], log)
        return {"markdown": open(out_md, encoding="utf-8").read(),
                "engine": "text", "classify": "n/a", "images_base64": {},
                "content_list_json": None}
    if ext == "xlsx":
        _run(XLSX2MD + [doc_path, "-o", out_md], log)
        return {"markdown": open(out_md, encoding="utf-8").read(),
                "engine": "text", "classify": "n/a", "images_base64": {},
                "content_list_json": None}
    if ext != "pdf":
        raise RuntimeError(f"unsupported extension '.{ext}' on the serverless path "
                           "(pdf/docx/xlsx only; route legacy .doc/.xls locally)")

    # Derotate (Tesseract OSD, self-verifying) -- same visible-sibling
    # convention as the CLI pipeline.
    src = doc_path
    if not no_derotate:
        derotated = os.path.join(workdir, f"{stem}.derotated.pdf")
        _run(PDF2MD + [src, "--derotate", derotated], log)
        src = derotated

    # Classify (digital vs scan), unless the job forces an engine.
    classify = "forced"
    if engine not in ("text", "mineru"):
        proc = _run(PDF2MD + [src, "--classify-only"], log)
        classify = proc.stdout.strip()
        engine = {"digital": "text", "scan": "mineru"}.get(classify)
        if engine is None:
            raise RuntimeError(f"unexpected classify output: {classify!r}")
        log.append(f"[handler] auto-routed: {classify} -> engine={engine}")

    if engine == "text":
        _run(PDF2MD + [src, "-o", out_md], log)
    else:
        cmd = MINERU2MD + [src, "-o", out_md]
        if no_reconcile_spelling:
            cmd.append("--no-reconcile-spelling")
        _run(cmd, log)

    result = {"markdown": open(out_md, encoding="utf-8").read(),
              "engine": engine, "classify": classify, "images_base64": {},
              "content_list_json": None}

    # MinerU sidecars: per-document image folder + provenance json, when produced.
    images_dir = os.path.join(workdir, "images", stem)
    if os.path.isdir(images_dir):
        for name in sorted(os.listdir(images_dir)):
            with open(os.path.join(images_dir, name), "rb") as f:
                result["images_base64"][name] = base64.b64encode(f.read()).decode("ascii")
    content_list = os.path.join(workdir, f"{stem}.content_list.json")
    if os.path.isfile(content_list):
        result["content_list_json"] = open(content_list, encoding="utf-8").read()

    return result


def handler(job: dict) -> dict:
    """RunPod job signature: job["input"] is the payload dict."""
    inp = job.get("input") or {}
    filename = inp.get("filename")
    if not filename:
        return {"error": "job input needs a 'filename'"}

    log: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pdf2md-job-") as workdir:
        doc_path = os.path.join(workdir, os.path.basename(filename))
        try:
            if inp.get("content_base64"):
                with open(doc_path, "wb") as f:
                    f.write(base64.b64decode(inp["content_base64"]))
            elif inp.get("url"):
                urllib.request.urlretrieve(inp["url"], doc_path)
            else:
                return {"error": "job input needs 'content_base64' or 'url'"}

            result = convert(
                doc_path,
                engine=inp.get("engine"),
                no_derotate=bool(inp.get("no_derotate")),
                no_reconcile_spelling=bool(inp.get("no_reconcile_spelling")),
                log=log,
            )
            result["log"] = "\n".join(log)
            return result
        except Exception as e:
            return {"error": str(e), "log": "\n".join(log)}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        # Local smoke test: wrap a real file into a fake job, print a summary.
        # Exercises the exact same code path RunPod invokes, minus the SDK.
        path = sys.argv[2]
        with open(path, "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")
        job = {"input": {"filename": os.path.basename(path),
                          "content_base64": payload,
                          **({"no_derotate": True} if "--no-derotate" in sys.argv else {})}}
        out = handler(job)
        if "error" in out:
            print("ERROR:", out["error"], file=sys.stderr)
            print(out.get("log", ""), file=sys.stderr)
            sys.exit(1)
        print(f"engine={out['engine']} classify={out['classify']} "
              f"md_chars={len(out['markdown'])} images={len(out['images_base64'])} "
              f"content_list={'yes' if out['content_list_json'] else 'no'}", file=sys.stderr)
        print(out["markdown"])
    else:
        import runpod
        runpod.serverless.start({"handler": handler})
