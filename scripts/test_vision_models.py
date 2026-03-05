#!/usr/bin/env python3
"""Test vision capabilities of newly added VL models."""

import base64
import json
import urllib.request
import urllib.error
import sys

API_KEY = "flip_bdb55f05935da66a9cec280a69464392"
BASE_URL = "http://localhost:11434"

MODELS = [
    "Gemma3-27B-it-vl-GLM-4.7-Uncensored-Heretic",
    "Qwen3-VL-32B-Gemini-Heretic-Uncensored-Thinking",
]

def test_model(model_name: str, image_b64: str) -> None:
    print(f"\n{'='*60}")
    print(f"Testing: {model_name}")
    print('='*60)

    payload = {
        "model": model_name,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": "What do you see in this image? Describe it briefly in 2-3 sentences."}
            ]
        }],
        "max_tokens": 300,
        "force_switch": True
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            tokens = result.get("usage", {})
            print(f"✅ Response: {content}")
            print(f"📊 Tokens: {tokens}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"❌ HTTP {e.code}: {body}")
    except Exception as e:
        print(f"❌ Error: {e}")


def main():
    image_path = "/tmp/test_image.jpg"
    print(f"Loading image: {image_path}")
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")
    print(f"Image size: {len(image_b64)} bytes (b64)")

    for model in MODELS:
        test_model(model, image_b64)

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    main()
