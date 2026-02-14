
import requests
import json
import sys
import time

API_KEY = "flip_d73d65ca4780b9f17f9c3cc10c1bb218"
BASE_URL = "http://127.0.0.1:11434"

def test_endpoint(name, method, url, data=None, headers=None, expect_status=200):
    print(f"Testing {name}...", end=" ")
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            r = requests.post(url, json=data, headers=headers, timeout=60) # Longer timeout for generation
        
        if r.status_code == expect_status:
            print(f"✅ OK ({r.status_code})")
            if "tags" in url or "models" in url:
                print(f"   > Found {len(r.json().get('models', r.json().get('data', [])))} models")
            return True
        else:
            print(f"❌ FAILED (Got {r.status_code}, expected {expect_status})")
            print(f"   Response: {r.text[:200]}...")
            return False
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False

print(f"🔍 Starting Comprehensive System Test for Llama Guardian")
print(f"📍 Target: {BASE_URL}")
print(f"🔑 Key: {API_KEY[:10]}...")
print("-" * 50)

# 1. Test Auth (No Key)
test_endpoint(
    "Auth Enforcement (No Key)", 
    "GET", 
    f"{BASE_URL}/api/tags", 
    expect_status=401
)

# 2. Test Tags (With Key)
headers = {"Authorization": f"Bearer {API_KEY}"}
models_ok = test_endpoint(
    "Ollama API Tags (/api/tags)", 
    "GET", 
    f"{BASE_URL}/api/tags", 
    headers=headers
)

# 3. Test Llama-server direct connection (optional check)
try:
    direct = requests.get("http://127.0.0.1:11440/health", timeout=2)
    print(f"Backend Llama-Server (11440): {'✅ UP' if direct.status_code == 200 else '⚠️ UP (Status ' + str(direct.status_code) + ')'}")
except:
    print("Backend Llama-Server (11440): ⚠️ NOT REACHABLE (might be protected or down)")

# 4. Test Chat (Simple)
# Use a known model or the first one from tags if available
model = "glm-4" # Default assumption
chat_data = {
    "model": model,
    "messages": [{"role": "user", "content": "Return the word 'Working' and nothing else."}],
    "stream": False
}

print(f"Testing Chat Generation with model '{model}'...")
api_ok = test_endpoint(
    "Ollama Chat API (/api/chat)", 
    "POST", 
    f"{BASE_URL}/api/chat", 
    data=chat_data,
    headers=headers
)

# 5. Test OpenAI Compat
openai_data = {
    "model": model,
    "messages": [{"role": "user", "content": "Return the word 'Working'."}],
    "stream": False
}
openai_ok = test_endpoint(
    "OpenAI Compat API (/v1/chat/completions)", 
    "POST", 
    f"{BASE_URL}/v1/chat/completions",
    data=openai_data,
    headers=headers
)

print("-" * 50)
print("🏁 Test Summary")
if models_ok and api_ok and openai_ok:
    print("✅ SYSTEM FULLY OPERATIONAL")
else:
    print("⚠️ ISSUES DETECTED")
