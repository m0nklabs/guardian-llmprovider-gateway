#!/usr/bin/env python3
"""
Verify that all priority models can load and generate a prompt.
Uses the Guardian proxy API for model switching — no direct llama-server management.
This prevents the 'double llama' VRAM conflict.
"""
import time
import requests
import json
import os
import sys
import yaml

# Configuration
GUARDIAN_URL = "http://127.0.0.1:11434"
API_KEY = "flip_bdb55f05935da66a9cec280a69464392"
MODELS_CONFIG = "/home/flip/llama_cpp_guardian/config/models.yaml"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Test prompt
PROMPT = "Explain briefly what kind of AI model you are."

# Priority models to verify
PRIORITY_MODELS = [
    "Qwen3-VL-30B-A3B-Thinking",
    "Qwen3-30B-A3B-Thinking-2507",
    "gemma-3-27b-it",
]


def check_guardian_health() -> bool:
    """Check if Guardian proxy is running by querying its /api/tags endpoint."""
    try:
        resp = requests.get(
            f"{GUARDIAN_URL}/api/tags",
            headers=HEADERS,
            timeout=5
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


def wait_for_backend(timeout: int = 120) -> bool:
    """Wait for the Guardian backend to become healthy after model switch."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{GUARDIAN_URL}/api/tags",
                headers=HEADERS,
                timeout=5
            )
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def test_model(model_name: str) -> dict:
    """
    Test a model by sending a chat completion through the Guardian.
    The Guardian auto-switches the backend llama-server to the requested model.
    """
    result = {
        "model": model_name,
        "status": "unknown",
        "response": None,
        "total_time": 0,
        "error": None
    }

    print(f"\nTesting {model_name}...")

    # Send chat request — Guardian handles model switch automatically
    payload = {
        "model": model_name,
        "messages": [
            {"role": "user", "content": PROMPT}
        ],
        "stream": False,
        "temperature": 0.7,
        "max_tokens": 300  # Thinking models need extra tokens for <think> phase
    }

    t_start = time.time()

    try:
        # Use OpenAI-compatible endpoint — Guardian auto-switches on /v1/chat/completions
        resp = requests.post(
            f"{GUARDIAN_URL}/v1/chat/completions",
            headers=HEADERS,
            json=payload,
            timeout=180  # 3 min for model switch + inference
        )
        total_time = time.time() - t_start
        result["total_time"] = total_time

        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices", [])
            # Debug: show raw response structure
            print(f"    [DEBUG] Response keys: {list(data.keys())}")
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content", "")
                reasoning = msg.get("reasoning_content", "")
                print(f"    [DEBUG] content len={len(content)}, reasoning len={len(reasoning)}")
                print(f"    [DEBUG] content repr: {repr(content[:200])}")
                # For thinking models, content might be empty but reasoning_content has the thinking
                actual_content = content.strip()
                if not actual_content and reasoning:
                    actual_content = f"[thinking model] {reasoning.strip()[:100]}"
                if actual_content:
                    result["status"] = "success"
                    result["response"] = actual_content[:150]
                    print(f"  ✅ {model_name}: OK ({total_time:.1f}s)")
                    print(f"     \"{actual_content[:100]}...\"")
                else:
                    result["status"] = "empty_response"
                    result["error"] = "Model returned empty content"
                    print(f"  ❌ {model_name}: Empty response")
            else:
                result["status"] = "no_choices"
                result["error"] = f"No choices in response: {json.dumps(data)[:200]}"
                print(f"  ❌ {model_name}: No choices in response")
        else:
            result["status"] = "api_error"
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            print(f"  ❌ {model_name}: API Error {resp.status_code}")
            print(f"     {resp.text[:200]}")

    except requests.Timeout:
        result["status"] = "timeout"
        result["error"] = "Request timed out (180s)"
        print(f"  ❌ {model_name}: Timeout (model switch + inference > 180s)")
    except requests.ConnectionError:
        result["status"] = "connection_error"
        result["error"] = "Guardian not reachable"
        print(f"  ❌ {model_name}: Guardian connection failed")
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        print(f"  ❌ {model_name}: {e}")

    return result


def load_available_models() -> dict:
    """Load models from Guardian config."""
    if not os.path.exists(MODELS_CONFIG):
        print(f"Config not found: {MODELS_CONFIG}")
        return {}
    with open(MODELS_CONFIG) as f:
        data = yaml.safe_load(f)
        return data.get("models", {})


def main():
    print("=" * 60)
    print("VERIFY PROMPTS — via Guardian Proxy (no double-llama)")
    print("=" * 60)

    # Check Guardian is running
    if not check_guardian_health():
        print("❌ Guardian proxy not running on port 11434!")
        print("   Start with: sudo systemctl start llama-guardian llama-server")
        sys.exit(1)

    # Load model configs to verify they exist
    models = load_available_models()
    print(f"Models in config: {len(models)}")

    # Check which priority models are configured
    available = []
    for name in PRIORITY_MODELS:
        if name in models:
            path = models[name].get("path", "")
            kv = models[name].get("kv_type", "q4_0")
            ctx = models[name].get("context", 32768)
            if os.path.exists(path):
                available.append(name)
                print(f"  ✓ {name} (ctx={ctx}, kv={kv})")
            else:
                print(f"  ✗ {name} → FILE NOT FOUND: {path}")
        else:
            print(f"  ✗ {name} → NOT IN CONFIG")

    if not available:
        print("\n❌ No priority models available!")
        sys.exit(1)

    print(f"\nTesting {len(available)} models via Guardian on port 11434...")
    print("-" * 60)

    results = []
    for model_name in available:
        result = test_model(model_name)
        results.append(result)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = sum(1 for r in results if r["status"] == "success")

    for r in results:
        icon = "✅" if r["status"] == "success" else "❌"
        extra = f" ({r['total_time']:.1f}s)" if r["status"] == "success" else f" [{r['error']}]"
        print(f"  {icon} {r['model']}{extra}")

    print(f"\nResult: {passed}/{len(results)} passed")

    if passed < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
