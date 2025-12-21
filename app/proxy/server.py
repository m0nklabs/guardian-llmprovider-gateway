import os
import json
import asyncio
import logging
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

import yaml

import httpx
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.background import BackgroundTask
from starlette.status import HTTP_401_UNAUTHORIZED

from collections import defaultdict
from app.proxy.optimizer import RequestOptimizer

# Load configuration from settings.yaml
def load_config() -> dict:
    """Load configuration from settings.yaml with sensible defaults."""
    config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
    default_config = {
        "timeouts": {
            "tiers": {
                "tier_70b": {"min_size_mb": 40000, "timeout_seconds": 900},
                "tier_32b": {"min_size_mb": 20000, "timeout_seconds": 600},
                "tier_13b": {"min_size_mb": 10000, "timeout_seconds": 300},
                "tier_8b": {"min_size_mb": 5000, "timeout_seconds": 180},
                "tier_small": {"min_size_mb": 0, "timeout_seconds": 120},
            },
            "default_timeout": 300
        }
    }
    
    try:
        if config_path.exists():
            with open(config_path, 'r') as f:
                file_config = yaml.safe_load(f) or {}
            # Merge with defaults (file config takes precedence)
            if "timeouts" in file_config:
                default_config["timeouts"].update(file_config["timeouts"])
            return default_config
    except Exception as e:
        logging.warning(f"Failed to load config from {config_path}: {e}. Using defaults.")
    
    return default_config

# Load config at module level
CONFIG = load_config()

# Configuration
OLLAMA_URL = "http://127.0.0.1:11436"
# Total VRAM available (approx 28GB: 12GB + 16GB)
# We set a safe limit to avoid OOM (Adjusted to 26GB to provide safety buffer for CPU offloading prevention)
SAFE_VRAM_LIMIT_MB = 26000
# Global default context size (28k)
DEFAULT_CONTEXT_SIZE = 28672
# Concurrency Limit (Increased to allow parallel small models)
MAX_CONCURRENT_REQUESTS = 8
# Max Request Duration (5 minutes) - Kills stuck requests
MAX_REQUEST_TIMEOUT = 300
STATS_FILE = "data/model_stats.json"
CLIENTS_FILE = "config/clients.json"

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Proxy")
security = HTTPBasic()
app = FastAPI()

# ...existing code...

def get_gpu_metrics():
    try:
        result = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=memory.used,memory.free,memory.total', '--format=csv,nounits,noheader'],
            encoding='utf-8'
        )
        lines = result.strip().split('\n')
        total_used = 0
        total_free = 0
        total_cap = 0
        for line in lines:
            u, f, t = map(int, line.split(','))
            total_used += u
            total_free += f
            total_cap += t
        return {'used': total_used, 'free': total_free, 'total': total_cap}
    except Exception as e:
        logger.error(f"Failed to get GPU metrics: {e}")
        return {'used': 0, 'free': SAFE_VRAM_LIMIT_MB, 'total': SAFE_VRAM_LIMIT_MB}

def get_model_size(model_name: str) -> int:
    if "70b" in model_name: return 40000
    if "32b" in model_name: return 20000
    if "13b" in model_name: return 10000
    if "8b" in model_name: return 6000
    if "7b" in model_name: return 5000
    if "1.5b" in model_name: return 1500
    if "0.5b" in model_name: return 600
    if "embed" in model_name: return 500
    return 4000

def get_model_timeout(model_name: str) -> int:
    """Calculate timeout based on model size using config tiers.
    
    Tiers are configurable in config/settings.yaml under 'timeouts.tiers'.
    Each tier has min_size_mb and timeout_seconds.
    """
    size = get_model_size(model_name)
    timeout_config = CONFIG.get("timeouts", {})
    tiers = timeout_config.get("tiers", {})
    default_timeout = timeout_config.get("default_timeout", 300)
    
    # Sort tiers by min_size_mb descending to match largest first
    sorted_tiers = sorted(
        tiers.items(),
        key=lambda x: x[1].get("min_size_mb", 0),
        reverse=True
    )
    
    for tier_name, tier_config in sorted_tiers:
        min_size = tier_config.get("min_size_mb", 0)
        timeout = tier_config.get("timeout_seconds", default_timeout)
        
        if size >= min_size:
            logger.debug(f"Model {model_name} ({size}MB) matched tier '{tier_name}' -> {timeout}s timeout")
            return timeout
    
    # Fallback to default
    logger.debug(f"Model {model_name} ({size}MB) using default timeout -> {default_timeout}s")
    return default_timeout


async def unload_model(model_name: str):
    async with httpx.AsyncClient() as client:
        try:
            await client.post(f"{OLLAMA_URL}/api/generate", json={"model": model_name, "keep_alive": 0})
        except Exception as e:
            logger.error(f"Failed to unload {model_name}: {e}")

async def update_model_stats(model_name: str):
    pass

async def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    # Permissive Mode: We use Basic Auth for identification (Client ID), not security.
    # We accept any password (or none), as long as a username is provided.
    if credentials.username:
        return credentials.username
        
    raise HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Username required for identification",
        headers={"WWW-Authenticate": "Basic"},
    )

# VramScheduler
class VramScheduler:

    def __init__(self, limit_mb):
        self.limit_mb = limit_mb
        self.active_counts = defaultdict(int) # model -> count
        self.condition = asyncio.Condition()

    async def acquire(self, model_name, model_size_mb):
        async with self.condition:
            while True:
                # Calculate what VRAM would be if we proceed
                current_active_models = [m for m, c in self.active_counts.items() if c > 0]
                
                needed_vram = 0
                for m in current_active_models:
                    needed_vram += get_model_size(m)
                
                # If this model is NOT already active, we need to add its size
                if model_name not in current_active_models:
                    needed_vram += model_size_mb
                
                if needed_vram <= self.limit_mb:
                    self.active_counts[model_name] += 1
                    logger.info(f"VRAM Acquired for {model_name}. Active: {current_active_models + [model_name] if model_name not in current_active_models else current_active_models}")
                    return # Success
                
                # Wait
                logger.info(f"Wait: {model_name} ({model_size_mb}MB) needs space. Active: {current_active_models} (Total: {needed_vram}MB > {self.limit_mb}MB)")
                await self.condition.wait()

    async def release(self, model_name):
        async with self.condition:
            self.active_counts[model_name] -= 1
            if self.active_counts[model_name] <= 0:
                del self.active_counts[model_name]
            self.condition.notify_all()
            logger.info(f"VRAM Released for {model_name}.")

# State
class State:
    def __init__(self):
        self.active_generations: Dict[str, int] = {} # request_id -> vram_usage
        self.model_stats: Dict[str, int] = {}
        self.last_used: Dict[str, float] = defaultdict(float)
        # VRAM Scheduler
        self.scheduler = VramScheduler(SAFE_VRAM_LIMIT_MB)
        # Optimizer
        self.optimizer = RequestOptimizer()

state = State()

# ...existing code...

async def check_and_free_vram(needed_mb: int, target_model: str):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{OLLAMA_URL}/api/ps")
            if r.status_code != 200: return

            data = r.json()
        models = data.get("models", [])
        
        # Get currently active models from scheduler to protect them
        active_models = list(state.scheduler.active_counts.keys())
        
        # Get REAL GPU metrics
        gpu = get_gpu_metrics()
        real_free = gpu['free']
        
        # Smart Combo Caching:
        # If we have enough space (with 500MB buffer), DON'T unload anything.
        # This keeps "combos" (sets of models) loaded in VRAM.
        if real_free >= (needed_mb + 500):
            current_combo = [m.get("name") or m.get("model") for m in models]
            if target_model not in current_combo:
                current_combo.append(target_model)
            logger.info(f"🎰 COMBO HIT! Keeping {len(current_combo)} models loaded: {current_combo} (Free: {real_free}MB)")
            return

        logger.info(f"VRAM Check: Need {needed_mb}MB, Have {real_free}MB. Deficit: {needed_mb - real_free}MB")

        # Identify idle models
        idle_models = []
        for m in models:
            name = m.get("name") or m.get("model")
            # Skip target and active
            if name == target_model or name in active_models: 
                continue
            
            size_vram = m.get("size_vram", 0)
            size_mb = int(size_vram / 1024 / 1024)
            
            # Use our own last_used tracker for LRU policy
            last_used_ts = state.last_used.get(name, 0)
            
            idle_models.append({
                "name": name,
                "size_mb": size_mb,
                "last_used": last_used_ts
            })

        # Sort by last_used (Oldest first) -> Least Recently Used
        idle_models.sort(key=lambda x: x["last_used"])

        freed_mb = 0
        deficit = (needed_mb + 500) - real_free
        
        # Partial Unload: Only unload enough to fit the new model
        for m in idle_models:
            if deficit <= 0:
                break
                
            logger.info(f"Smart Unload: Dropping {m['name']} (Last used: {m['last_used']}) to free {m['size_mb']}MB")
            await unload_model(m['name'])
            freed_mb += m['size_mb']
            deficit -= m['size_mb']
            
        if freed_mb > 0:
            await asyncio.sleep(0.5)
            
    except Exception as e:
        logger.error(f"Error checking VRAM state: {e}")

@app.post("/api/generate")
async def proxy_generate(request: Request, client_id: str = Depends(verify_credentials)):
    try:
        body = await request.json()
    except:
        body = {}
        
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    # Update Last Used (for LRU caching)
    state.last_used[model] = time.time()

    logger.info(f"Request from client '{client_id}' for model '{model}'")

    # Enforce default context size if not specified
    if "options" not in body:
        body["options"] = {}
    
    # Optimize Options (Inject Benchmark Results)
    body["options"] = state.optimizer.optimize_options(model, body["options"])

    if "num_ctx" not in body["options"]:
        body["options"]["num_ctx"] = DEFAULT_CONTEXT_SIZE
        logger.info(f"Enforced default context size {DEFAULT_CONTEXT_SIZE} for {model}")

    vram_needed = get_model_size(model)
    
    # Acquire VRAM slot
    await state.scheduler.acquire(model, vram_needed)
    
    try:
        # Active Unload Logic (now aware of active models)
        await check_and_free_vram(vram_needed, model)

        # Use configured timeout to prevent stuck requests
        model_timeout = get_model_timeout(model)
        logger.info(f"Using timeout {model_timeout}s for model {model}")
        client = httpx.AsyncClient(timeout=model_timeout)
        req = client.build_request(
            request.method,
            f"{OLLAMA_URL}/api/generate",
            json=body,
            timeout=model_timeout
        )
        r = await client.send(req, stream=True)
    except httpx.ReadTimeout:
        logger.error(f"Request timeout for {model} after {model_timeout}s")
        await state.scheduler.release(model)
        if 'client' in locals(): await client.aclose()
        raise HTTPException(status_code=504, detail="Request timed out (Guardian Protection)")
    except Exception as e:
        await state.scheduler.release(model)
        if 'client' in locals(): await client.aclose()
        raise e

    async def stream_generator():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()
            await state.scheduler.release(model)

    background = BackgroundTask(update_model_stats, model_name=model)
    
    return StreamingResponse(
        stream_generator(),
        status_code=r.status_code,
        headers=r.headers,
        background=background
    )

@app.post("/api/chat")
async def proxy_chat(request: Request, client_id: str = Depends(verify_credentials)):
    try:
        body = await request.json()
    except:
        body = {}
        
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    # Update Last Used (for LRU caching)
    state.last_used[model] = time.time()

    logger.info(f"Chat request from client '{client_id}' for model '{model}'")

    # Enforce default context size if not specified
    if "options" not in body:
        body["options"] = {}
    
    # Optimize Options (Inject Benchmark Results)
    body["options"] = state.optimizer.optimize_options(model, body["options"])

    if "num_ctx" not in body["options"]:
        body["options"]["num_ctx"] = DEFAULT_CONTEXT_SIZE

    vram_needed = get_model_size(model)
    
    # Acquire VRAM slot
    await state.scheduler.acquire(model, vram_needed)

    try:
        # Active Unload Logic
        await check_and_free_vram(vram_needed, model)

        # Forward request with proper streaming lifecycle and timeout
        chat_timeout = get_model_timeout(model)
        logger.info(f"Using timeout {chat_timeout}s for chat with {model}")
        client = httpx.AsyncClient(timeout=chat_timeout)
        req = client.build_request(
            request.method,
            f"{OLLAMA_URL}/api/chat",
            json=body,
            timeout=chat_timeout
        )
        r = await client.send(req, stream=True)
    except httpx.ReadTimeout:
        logger.error(f"Chat request timeout for {model} after {chat_timeout}s")
        await state.scheduler.release(model)
        if 'client' in locals(): await client.aclose()
        raise HTTPException(status_code=504, detail="Request timed out (Guardian Protection)")
    except Exception as e:
        await state.scheduler.release(model)
        if 'client' in locals(): await client.aclose()
        raise e

    async def stream_generator():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()
            await state.scheduler.release(model)

    # Background task to learn stats
    background = BackgroundTask(update_model_stats, model_name=model)
    
    return StreamingResponse(
        stream_generator(),
        status_code=r.status_code,
        headers=r.headers,
        background=background
    )

@app.get("/api/{path:path}")
async def proxy_get(path: str, request: Request):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{OLLAMA_URL}/api/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/api/{path:path}")
async def proxy_post(path: str, request: Request, client_id: str = Depends(verify_credentials)):
    body = await request.body()
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{OLLAMA_URL}/api/{path}", content=body)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

async def start_proxy():
    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=11434, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

