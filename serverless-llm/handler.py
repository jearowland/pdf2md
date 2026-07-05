#!/usr/bin/env python3
"""RunPod serverless handler: generic ollama inference.

Input:  {"model": "...", "prompt": "...", "format": {...}|null,
         "options": {...}, "timeout": 240}
Output: {"response": "<model output string>"} or {"error": "..."}

The model is served from the network volume (OLLAMA_MODELS); first
invocation on a fresh volume pulls it (one-time cost), every worker after
mounts it warm.
"""
import subprocess
import time

import requests
import runpod

OLLAMA = "http://127.0.0.1:11434"
_started = False


def ensure_server():
    global _started
    if _started:
        return
    subprocess.Popen(["/usr/local/bin/ollama", "serve"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            requests.get(OLLAMA, timeout=2)
            _started = True
            return
        except requests.RequestException:
            time.sleep(1)
    raise RuntimeError("ollama serve did not come up")


def handler(job):
    inp = job["input"]
    model = inp["model"]
    ensure_server()
    # ensure model present (no-op when the volume already has it)
    r = requests.post(f"{OLLAMA}/api/pull",
                      json={"model": model, "stream": False}, timeout=1800)
    if r.status_code != 200:
        return {"error": f"pull failed: {r.text[:200]}"}
    body = {"model": model, "prompt": inp["prompt"], "stream": False}
    if inp.get("format"):
        body["format"] = inp["format"]
    if inp.get("options"):
        body["options"] = inp["options"]
    try:
        r = requests.post(f"{OLLAMA}/api/generate", json=body,
                          timeout=inp.get("timeout", 240))
        r.raise_for_status()
        return {"response": r.json()["response"]}
    except requests.RequestException as e:
        return {"error": str(e)[:300]}


runpod.serverless.start({"handler": handler})
