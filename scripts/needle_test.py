#!/usr/bin/env python3
"""Needle-in-haystack test against a llama-server or Guardian endpoint.

Builds a long prompt from llama.cpp source files, inserts a needle near the
end, streams the completion, and reports token counts + whether the needle
was found. Usage:

    ./venv/bin/python scripts/needle_test.py --port 11896 [--guardian] \
        [--model qwen3.6-35b-turbo4] [--max-chars 935000]
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error

import yaml

from app.paths import guardian_apikeys_file

SOURCES = [
    "/home/flip/llama_cpp_official/src/llama-model.cpp",
    "/home/flip/llama_cpp_official/src/llama-context.cpp",
    "/home/flip/llama_cpp_official/src/llama-graph.cpp",
    "/home/flip/llama_cpp_official/src/llama-kv-cache.cpp",
    "/home/flip/llama_cpp_official/tests/test-backend-ops.cpp",
]
NEEDLE = "PURPLE-GIRAFFE-742"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11896)
    ap.add_argument("--guardian", action="store_true",
                    help="go through Guardian on 127.0.0.1:11434 with the goose key")
    ap.add_argument("--model", default="x")
    ap.add_argument("--max-chars", type=int, default=935000)
    ap.add_argument("--max-tokens", type=int, default=600)
    args = ap.parse_args()

    headers = {"Content-Type": "application/json"}
    if args.guardian:
        keys = yaml.safe_load(guardian_apikeys_file().read_text()) or {}
        token = next(k for k, v in keys.items()
                     if isinstance(v, dict) and v.get("name") == "goose")
        headers["Authorization"] = "Bearer " + token
        url = "http://127.0.0.1:11434/v1/chat/completions"
    else:
        url = f"http://127.0.0.1:{args.port}/v1/chat/completions"

    parts = []
    for p in SOURCES:
        try:
            parts.append(open(p, errors="replace").read())
        except FileNotFoundError:
            pass
    code = "\n\n".join(parts)[: args.max_chars]
    prompt = (
        code
        + f"\n\n// NOTE: The internal codename is {NEEDLE}.\n\n"
        + "Question: what is the internal codename from the note at the end? "
        + "Answer with just the codename."
    )
    print(f"chars: {len(prompt)}", flush=True)

    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "temperature": 0.0,
        "stream": True,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            usage = None
            content = ""
            reasoning = ""
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    d = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if d.get("usage"):
                    usage = d["usage"]
                delta = (d.get("choices") or [{}])[0].get("delta", {})
                content += delta.get("content") or ""
                reasoning += delta.get("reasoning_content") or ""
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:400])
        return 1

    dt = time.time() - t0
    pt = (usage or {}).get("prompt_tokens", 0)
    ct = (usage or {}).get("completion_tokens", 0)
    print(f"total={dt:.0f}s prompt_tokens={pt} completion={ct} "
          f"| wall prompt ~{pt / max(dt, 1):.0f} tok/s")
    print("NEEDLE FOUND:", NEEDLE in (content + reasoning))
    print("CONTENT:", content[:200])
    print("REASONING tail:", reasoning[-200:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
