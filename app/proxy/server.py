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
from app.proxy.auth import verify_api_key

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
# security = HTTPBasic() # Removed in favor of API Key Auth
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
    if not model_name: return 0
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
    
    # Default fallback
    return 8000

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

# Auth replaced by verify_api_key imported from app.proxy.auth

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

@app.post("/api/chat")
async def proxy_chat_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Bridge Ollama-style chat requests to OpenAI-style Llama Server"""
    try:
        body = await request.json()
    except:
        body = {}
        
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    logger.info(f"bridge: Ollama chat request for '{model}' -> Translating to OpenAI format")
    
    # Check if model switch needed
    current_model = await model_manager.get_current_model()
    if model != current_model and model in model_manager.models:
         await model_manager.switch_model(model)

    # Translate Ollama request to OpenAI format
    messages = body.get("messages", [])
    stream = body.get("stream", True)
    
    # Basic options mapping
    options = body.get("options", {})
    temperature = options.get("temperature", 0.7)
    
    openai_body = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature
    }

    # Forward to Llama Server (OpenAI Endpoint)
    timeout_sec = get_model_timeout(model)
    client = httpx.AsyncClient(timeout=timeout_sec)
    
    req = client.build_request(
        "POST",
        f"{LLAMA_SERVER_URL}/v1/chat/completions",
        json=openai_body,
        timeout=timeout_sec
    )
    
    try:
        r = await client.send(req, stream=stream)
    except Exception as e:
        await client.aclose()
        raise e

    if stream:
        async def stream_adapter():
            try:
                async for chunk in r.aiter_lines():
                    if not chunk or chunk.strip() == "data: [DONE]": 
                        continue
                    if chunk.startswith("data: "):
                        try:
                            data = json.loads(chunk[6:])
                            # Translate OpenAI chunk back to Ollama chunk
                            if "choices" in data and len(data["choices"]) > 0:
                                delta = data["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    ollama_chunk = {
                                        "model": model,
                                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                        "message": {"role": "assistant", "content": content},
                                        "done": False
                                    }
                                    yield json.dumps(ollama_chunk) + "\n"
                        except:
                            pass
                # Final done message
                yield json.dumps({
                    "model": model, 
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), 
                    "done": True,
                    "total_duration": 0,
                    "load_duration": 0,
                    "prompt_eval_count": 0,
                    "eval_count": 0
                }) + "\n"
            finally:
                await r.aclose()
                await client.aclose()

        return StreamingResponse(stream_adapter(), media_type="application/x-ndjson")
    else:
        # Handle non-streaming response
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            ollama_resp = {
                "model": model,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "message": {"role": "assistant", "content": content},
                "done": True,
                "total_duration": 0,
                "load_duration": 0,
                "prompt_eval_count": data.get("usage", {}).get("prompt_tokens", 0),
                "eval_count": data.get("usage", {}).get("completion_tokens", 0)
            }
            await r.aclose()
            await client.aclose()
            return ollama_resp
        except Exception as e:
            await r.aclose()
            await client.aclose()
            raise e

# Legacy endpoint for Ollama generate
@app.post("/api/generate")
async def proxy_generate_ollama(request: Request, client_id: str = Depends(verify_api_key)):
    """Bridge Ollama /api/generate (prompt-based) to /api/chat logic"""
    try:
        body = await request.json()
    except:
        body = {}
        
    prompt = body.get("prompt", "")
    if prompt and "messages" not in body:
        # Convert prompt to messages format for the chat bridge
        body["messages"] = [{"role": "user", "content": prompt}]
    
    # Create a new request with the modified body
    # We can't easily replace the request body in the request object, so we'll call the logic directly
    # But proxy_chat_ollama expects a Request. Let's refactor slightly or just patch the body retrieval if possible.
    # actually, verifying dependency injection might be tricky if we call the function directly.
    # Instead, let's implement the specific Generate bridge here to be safe and clean.
    
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")
        
    # Translate to OpenAI
    messages = body.get("messages", [{"role": "user", "content": prompt}])
    stream = body.get("stream", True)
    options = body.get("options", {})
    temperature = options.get("temperature", 0.7)
    
    openai_body = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature
    }

    # reuse the same client/streaming logic
    timeout_sec = get_model_timeout(model)
    client = httpx.AsyncClient(timeout=timeout_sec)
    
    req = client.build_request(
        "POST",
        f"{LLAMA_SERVER_URL}/v1/chat/completions",
        json=openai_body,
        timeout=timeout_sec
    )

    try:
        r = await client.send(req, stream=stream)
    except Exception as e:
        await client.aclose()
        raise e

    if stream:
        async def stream_adapter_generate():
            try:
                async for chunk in r.aiter_lines():
                    if not chunk or chunk.strip() == "data: [DONE]": 
                        continue
                    if chunk.startswith("data: "):
                        try:
                            data = json.loads(chunk[6:])
                            if "choices" in data and len(data["choices"]) > 0:
                                delta = data["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    # /api/generate response format: { "response": "..." }
                                    ollama_chunk = {
                                        "model": model,
                                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                        "response": content,
                                        "done": False
                                    }
                                    yield json.dumps(ollama_chunk) + "\n"
                        except:
                            pass
                yield json.dumps({
                    "model": model, 
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), 
                    "done": True,
                    "response": "",
                    "total_duration": 0,
                    "load_duration": 0,
                    "prompt_eval_count": 0,
                    "eval_count": 0
                }) + "\n"
            finally:
                await r.aclose()
                await client.aclose()

        return StreamingResponse(stream_adapter_generate(), media_type="application/x-ndjson")
    else:
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            ollama_resp = {
                "model": model,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "response": content,
                "done": True,
                "context": [], # context usually returned by generate, handled by client
                "total_duration": 0,
                "load_duration": 0,
                "prompt_eval_count": data.get("usage", {}).get("prompt_tokens", 0),
                "eval_count": data.get("usage", {}).get("completion_tokens", 0)
            }
            await r.aclose()
            await client.aclose()
            return ollama_resp
        except Exception as e:
            await r.aclose()
            await client.aclose()
            raise e


@app.get("/api/version")
async def get_version():
    """Mimic Ollama version endpoint"""
    return {"version": "0.1.27"}

@app.get("/api/tags")
async def proxy_tags_ollama(client_id: str = Depends(verify_api_key)):
    """Simulate Ollama /api/tags endpoint"""
    import traceback
    models = []
    try:
        # Get models from our manager config
        if not hasattr(model_manager, 'models') or model_manager.models is None:
            logger.error("model_manager.models is missing or None")
            return {"models": []}
            
        for name in model_manager.models.keys():
            models.append({
                "name": name,
                "model": name,
                "modified_at": "2024-01-01T00:00:00.0000000+00:00",
                "size": get_model_size(name) * 1024 * 1024,
                "digest": "000000000000",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "llama",
                    "families": ["llama"],
                    "parameter_size": "7B",
                    "quantization_level": "Q4_0"
                }
            })
    except Exception as e:
        logger.error(f"Error in proxy_tags_ollama: {e}")
        traceback.print_exc()
        # Return empty list instead of crashing
        pass
    return {"models": models}



# ...existing code...


# Model listing endpoint (Before catch-all)
@app.get("/v1/models")
async def list_models(client_id: str = Depends(verify_api_key)):
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
async def proxy_v1_get(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{LLAMA_SERVER_URL}/v1/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/v1/{path:path}")
async def proxy_v1_post(path: str, request: Request, client_id: str = Depends(verify_api_key)):
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
async def save_session(request: Request, client_id: str = Depends(verify_api_key)):
    logger.info(f"💾 Session SAVE request from {client_id}")
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
async def load_session(request: Request, client_id: str = Depends(verify_api_key)):
    logger.info(f"📂 Session LOAD request from {client_id}")
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
async def list_sessions(client_id: str = Depends(verify_api_key)):
    logger.debug(f"📜 Session LIST request from {client_id}")
    try:
        save_path = Path("/home/flip/llama_slots") 
        if not save_path.exists():
            return {"sessions": []}
            
        files = [f.stem for f in save_path.glob("*.bin")]
        return {"sessions": sorted(files)}
    except Exception as e:
        logger.error(f"List sessions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import asyncio
    asyncio.run(start_proxy())
