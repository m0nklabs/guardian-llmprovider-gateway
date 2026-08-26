import asyncio
import httpx
import time

# We will use 3 models that should fit together in 28GB VRAM
# 1. qwen2.5:0.5b (~600MB)
# 2. llama3.1:8b (~5GB)
# 3. nomic-embed-text (~300MB)
# Total: ~6GB. Fits easily.

MODELS = [
    "qwen2.5:0.5b",
    "llama3.1:8b",
    "nomic-embed-text"
]

URL = "http://localhost:11434/api/generate"
AUTH = ("caramba", "caramba_secret")

async def trigger_model(model):
    print(f"🚀 Triggering {model}...")
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            start = time.time()
            resp = await client.post(
                URL, 
                auth=AUTH, 
                json={
                    "model": model, 
                    "prompt": "Hi", 
                    "stream": False,
                    "options": {"num_ctx": 1024}
                }
            )
            dur = time.time() - start
            print(f"✅ {model} finished in {dur:.2f}s (Status: {resp.status_code})")
        except Exception as e:
            print(f"❌ {model} failed: {e}")

async def main():
    print("🎰 Testing TRIPLE COMBO HIT...")
    
    # Fire them all at once!
    # The Guardian should let them all run (or queue slightly) but KEEP them all loaded.
    tasks = [trigger_model(m) for m in MODELS]
    await asyncio.gather(*tasks)
    
    print("🏁 Combo Test Completed. Check Guardian logs for 'COMBO HIT'!")

if __name__ == "__main__":
    asyncio.run(main())
