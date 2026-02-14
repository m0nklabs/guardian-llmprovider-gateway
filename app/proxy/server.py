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

import base64

import httpx
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.background import BackgroundTask
from starlette.status import HTTP_401_UNAUTHORIZED

from collections import defaultdict
from app.proxy.optimizer import RequestOptimizer
from app.engine.manager import ModelManager

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
LLAMA_SERVER_URL = "http://127.0.0.1:11440"

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
model_manager = ModelManager()

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
    model_lower = model_name.lower()
    # Specific overrides for new models
    if "glm-4" in model_lower: return 26000  # ~24GB
    if "qwen3" in model_lower and "30b" in model_lower: return 20000 # ~18GB
    if "deepseek-r1" in model_lower and "32b" in model_lower: return 22000 # ~19GB
    
    # Generic heuristics
    if "70b" in model_lower: return 40000
    if "32b" in model_lower: return 20000
    if "30b" in model_lower: return 20000
    if "27b" in model_lower: return 18000
    if "13b" in model_lower: return 10000
    if "14b" in model_lower: return 11000
    if "8b" in model_lower: return 6000
    if "7b" in model_lower: return 5000
    if "1.5b" in model_lower: return 1500
    if "0.5b" in model_lower: return 600
    if "embed" in model_lower: return 500
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
            await client.post(f"{LLAMA_SERVER_URL}/api/generate", json={"model": model_name, "keep_alive": 0})
        except Exception as e:
            logger.error(f"Failed to unload {model_name}: {e}")

async def update_model_stats(model_name: str):
    pass

async def verify_credentials(request: Request):
    # Permissive Mode: We use auth for identification (Client ID), not security.
    # Accepts both Basic Auth (username as client_id) and Bearer tokens (token as client_id).
    auth_header = request.headers.get("Authorization", "")
    
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            logger.debug(f"🔑 Bearer auth from client: {token}")
            return token
    elif auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username = decoded.split(":", 1)[0]
            if username:
                logger.debug(f"🔑 Basic auth from client: {username}")
                return username
        except Exception:
            pass
    
    raise HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Username (Basic) or token (Bearer) required for identification",
        headers={"WWW-Authenticate": 'Basic realm="llama-guardian", Bearer'},
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
    # Llama-server manages its own memory via single-model loading.
    # Manager.py handles switching. This logic is legacy Ollama-specific and is disabled.
    return

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
            f"{LLAMA_SERVER_URL}/api/generate",
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
            f"{LLAMA_SERVER_URL}/api/chat",
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
        resp = await client.get(f"{LLAMA_SERVER_URL}/api/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/api/{path:path}")
async def proxy_post(path: str, request: Request, client_id: str = Depends(verify_credentials)):
    body = await request.body()
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{LLAMA_SERVER_URL}/api/{path}", content=body)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

# Model listing endpoint (Before catch-all)
@app.get("/v1/models")
async def list_models():
    """List available models from config."""
    models_list = []
    try:
        current = await model_manager.get_current_model()
        for name, cfg in model_manager.models.items():
            models_list.append({
                "id": name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "organization-owner",
                "permission": []
            })
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        
    return {"object": "list", "data": models_list}

# OpenAI-compatible /v1/ routes (used by OpenClaw and other OpenAI-compatible clients)
@app.get("/v1/{path:path}")
async def proxy_v1_get(path: str, request: Request):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{LLAMA_SERVER_URL}/v1/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/v1/{path:path}")
async def proxy_v1_post(path: str, request: Request, client_id: str = Depends(verify_credentials)):
    body = await request.body()
    
    # Auto-switch logic for chat completions
    if path == "chat/completions":
        try:
            json_body = json.loads(body)
            requested_model = json_body.get("model")
            current_model = await model_manager.get_current_model()
            
            if requested_model and requested_model != current_model:
                if requested_model in model_manager.models:
                    logger.info(f"🔄 Auto-switching backend from {current_model} to {requested_model}")
                    try:
                        await model_manager.switch_model(requested_model)
                    except Exception as e:
                        logger.error(f"❌ Switch failed: {e}")
                        raise HTTPException(status_code=500, detail="Model switch failed")
                else:
                    logger.warning(f"⚠️ Requested model {requested_model} not managed by Guardian. Forwarding anyway.")
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.error(f"Error checking model switch: {e}")

    timeout = httpx.Timeout(600.0, connect=10.0)
    logger.info(f"OpenAI-compat request from client '{client_id}': POST /v1/{path}")
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{LLAMA_SERVER_URL}/v1/{path}",
            content=body,
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
        )
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

async def start_proxy():
    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=11434, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@app.post("/api/session/save")
async def save_session(request: Request):
    try:
        data = await request.json()
        filename = data.get("filename")
        if not filename:
            raise HTTPException(status_code=400, detail="Filename required")
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{LLAMA_SERVER_URL}/slots/0?action=save",
                json={"filename": filename},
                timeout=60.0
            )  
            if resp.status_code != 200:
                logger.error(f"Llama save failed: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Llama save failed: {resp.text}")
                
            return resp.json()
    except Exception as e:
        logger.error(f"Save session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/session/load")
async def load_session(request: Request):
    try:
        data = await request.json()
        filename = data.get("filename")
        if not filename:
            raise HTTPException(status_code=400, detail="Filename required")
            
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{LLAMA_SERVER_URL}/slots/0?action=restore",
                json={"filename": filename},
                timeout=60.0 # Loading takes time
            )
            if resp.status_code != 200:
                logger.error(f"Llama load failed: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Llama load failed: {resp.text}")
                
            return resp.json()
    except Exception as e:
        logger.error(f"Load session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/session/list")
async def list_sessions():
    try:
        save_path = Path("/home/flip/llama_slots") 
        if not save_path.exists():
            return {"sessions": []}
            
        files = [f.stem for f in save_path.glob("*.bin")]
        return {"sessions": sorted(files)}
    except Exception as e:
        logger.error(f"List sessions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
