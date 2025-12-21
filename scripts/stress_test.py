import asyncio
import httpx
import time
import sys

URL = "http://localhost:11434/api/generate"
AUTH = ("caramba", "caramba_secret")
MODEL = "deepseek-r1:32b" # Larger model

async def send_request(i, start_time):
    print(f"[{time.time() - start_time:.2f}s] 🚀 Request {i} queued...")
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            req_start = time.time()
            # Use stream=True to detect Time To First Token (Queue wait time)
            async with client.stream("POST", URL, auth=AUTH, json={
                "model": MODEL, 
                "prompt": "Explain quantum entanglement in one sentence.", 
                "stream": False, # We want the full response time for this test
                "options": {"num_ctx": 2048}
            }) as resp:
                # Wait for body
                body = await resp.aread()
                dur = time.time() - req_start
                
                if resp.status_code == 200:
                    print(f"[{time.time() - start_time:.2f}s] ✅ Request {i} finished in {dur:.2f}s")
                else:
                    print(f"[{time.time() - start_time:.2f}s] ⚠️ Request {i} error {resp.status_code}")
        except Exception as e:
            print(f"❌ Request {i} failed: {e}")

async def main():
    print(f"🔥 Starting Stress Test: 5 concurrent requests to {MODEL}...")
    start_time = time.time()
    tasks = [send_request(i, start_time) for i in range(5)]
    await asyncio.gather(*tasks)
    print(f"🏁 Stress Test Completed in {time.time() - start_time:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())
