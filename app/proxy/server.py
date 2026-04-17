import os
import json
import asyncio
import logging
import subprocess
import time
import sys
import errno
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import yaml
import httpx
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import StreamingResponse
from starlette.status import HTTP_401_UNAUTHORIZED

from collections import defaultdict
from app.proxy.optimizer import RequestOptimizer
from app.engine.manager import ModelManager, ModelLoadError
from app.proxy.auth import verify_api_key
from app.proxy.queue import InferenceQueue
from app.proxy.metrics import (
    track_request,
    update_queue_metrics,
    update_gpu_metrics,
    update_system_metrics,
    get_metrics_output,
    MODEL_SWITCHES,
    MODEL_CRASHES,
    QUEUE_TOTAL_QUEUED,
    QUEUE_TOTAL_COMPLETED,
    QUEUE_TOTAL_TIMEOUTS,
    AUTH_FAILURES,
)

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
# Read from settings.yaml proxy.vram_limit_mb, fallback to hardcoded value
def _load_vram_limit() -> int:
    try:
        config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        if config_path.exists():
            with open(config_path, 'r') as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("proxy", {}).get("vram_limit_mb", 27000)
    except Exception:
        pass
    return 27000

SAFE_VRAM_LIMIT_MB = _load_vram_limit()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Guardian")

PID_FILE = "guardian.pid"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Check and write PID file
    pid_path = Path(__file__).parent.parent.parent / PID_FILE
    if pid_path.exists():
        try:
            with open(pid_path, 'r') as f:
                content = f.read().strip()
                if content:
                    old_pid = int(content)
                    # Check if process exists
                    if old_pid != os.getpid():
                        try:
                            os.kill(old_pid, 0)
                            logger.error(f"FATAL: Guardian is already running (PID {old_pid}). Exiting immediately to prevent conflict.")
                            sys.exit(1)
                        except OSError as e:
                            if e.errno == errno.ESRCH:
                                logger.warning(f"Found stale PID file for PID {old_pid}. Overwriting.")
                            else:
                                raise e
        except ValueError:
             logger.warning("Invalid PID file found. Overwriting.")
        except FileNotFoundError:
            pass

    try:
        with open(pid_path, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"Guardian started with PID {os.getpid()}")
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")

    # SECURITY: Run startup model verification (checks actual backend matches config)
    try:
        await model_manager.startup_check()
    except Exception as e:
        logger.error(f"⚠️ Startup check error (non-fatal): {e}")

    # Start idle-unload background watcher
    idle_task = asyncio.create_task(_idle_unload_watcher())

    yield

    idle_task.cancel()
    
    # Shutdown: Remove PID file
    if pid_path.exists():
        try:
            with open(pid_path, 'r') as f:
                content = f.read().strip()
                if content and int(content) == os.getpid():
                     pid_path.unlink()
                     logger.info("PID file removed.")
        except Exception as e:
            logger.warning(f"Failed to clean up PID file: {e}")

app = FastAPI(lifespan=lifespan)
model_manager = ModelManager()


async def _idle_unload_watcher():
    """Background task: auto-unload llama-server after N minutes of inactivity."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        idle_minutes = model_manager.idle_unload_minutes
        if idle_minutes is None:
            continue  # Feature disabled
        if model_manager.is_unloaded:
            continue  # Already free
        if model_manager.active_requests > 0:
            continue  # Don't unload while requests are in-flight
        if inference_queue.active_count > 0 or inference_queue.waiting_count > 0:
            continue  # Don't unload while queue has pending work
        idle_secs = time.time() - model_manager.last_request_time
        if idle_secs >= idle_minutes * 60:
            logger.info(f"💤 Idle for {idle_secs/60:.1f}m (limit {idle_minutes}m) — auto-unloading to free VRAM")
            try:
                await model_manager.unload()
            except Exception as e:
                logger.error(f"❌ Auto-unload failed: {e}")


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
    
    # Small models
    if "0.5b" in model_lower: return 600
    if "embed" in model_lower: return 500
    
    # Default fallback
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


# Model switch concurrency lock - prevents race conditions when
# multiple requests try to switch models simultaneously
_model_switch_lock = asyncio.Lock()

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


# --- Inference queue: serializes access to single-slot backend ---
def _load_queue_config() -> dict:
    try:
        config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        if config_path.exists():
            with open(config_path, 'r') as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("queue", {})
    except Exception:
        pass
    return {}

_queue_cfg = _load_queue_config()
inference_queue = InferenceQueue(
    max_concurrent=_queue_cfg.get("max_concurrent", 1),
    queue_timeout=_queue_cfg.get("queue_timeout_seconds", 300),
)


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

    # Resolve aliases and case-insensitive names
    try:
        model = model_manager.resolve_model(model)
    except ValueError:
        pass  # Let it fall through — will be handled by switch logic

    logger.info(f"bridge: Ollama chat request for '{model}' -> Translating to OpenAI format")

    # Acquire inference slot (blocks if another request is active)
    try:
        request_id = await inference_queue.acquire(client_id, model)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "queue_timeout", "message": f"Waited {inference_queue.queue_timeout}s in queue"},
        )

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if model_manager.is_unloaded:
            logger.info(f"🔄 Auto-reloading '{model_manager.current_model}'...")
            async with _model_switch_lock:
                if model_manager.is_unloaded:
                    await model_manager.load()

        # Check if model switch needed (safe — we hold the queue slot)
        current_model = await model_manager.get_current_model()
        if model != current_model and model in model_manager.models:
            # SECURITY: Check client permission and pin
            if not model_manager.is_switch_allowed(client_id):
                logger.warning(f"🔒 Client '{client_id}' not in switch_allowlist, blocked Ollama switch to '{model}'")
            else:
                async with _model_switch_lock:
                    # Re-check after acquiring lock (another request may have switched already)
                    current_model = await model_manager.get_current_model()
                    if model != current_model:
                        try:
                            await model_manager.switch_model(model, client_id=client_id)
                        except ModelLoadError as e:
                            crash = e.crash_record
                            detail = {
                                "error": f"Model '{model}' failed to load",
                                "message": str(e),
                                "crash_details": crash.to_dict() if crash else None,
                            }
                            logger.error(f"💥 Model load crash: {detail}")
                            raise HTTPException(status_code=503, detail=detail)
                        except ValueError as e:
                            logger.warning(f"🔒 Switch denied: {e}")
                        except Exception as e:
                            logger.error(f"❌ Switch failed: {e}")
                            raise HTTPException(status_code=500, detail=f"Model switch failed: {e}")

        model_manager.last_request_time = time.time()
        model_manager.active_requests += 1

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
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.release(request_id)

            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            response = StreamingResponse(
                stream_adapter(),
                media_type="application/x-ndjson",
                headers={"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
            )
            _release_in_finally = False
            return response
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
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                return ollama_resp
            except Exception as e:
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
    finally:
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.release(request_id)

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
        body["messages"] = [{"role": "user", "content": prompt}]
    
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Model not specified")

    # Resolve aliases and case-insensitive names
    try:
        model = model_manager.resolve_model(model)
    except ValueError:
        pass  # Let it fall through — will be handled by switch logic

    # Acquire inference slot
    try:
        request_id = await inference_queue.acquire(client_id, model)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "queue_timeout", "message": f"Waited {inference_queue.queue_timeout}s in queue"},
        )

    _release_in_finally = True
    try:
        # Auto-reload if unloaded
        if model_manager.is_unloaded:
            logger.info(f"🔄 Auto-reloading '{model_manager.current_model}'...")
            async with _model_switch_lock:
                if model_manager.is_unloaded:
                    await model_manager.load()

        # Model switch (safe — we hold the queue slot)
        current_model = await model_manager.get_current_model()
        if model != current_model and model in model_manager.models:
            if not model_manager.is_switch_allowed(client_id):
                logger.warning(f"🔒 Client '{client_id}' not in switch_allowlist, blocked switch to '{model}'")
            else:
                async with _model_switch_lock:
                    current_model = await model_manager.get_current_model()
                    if model != current_model:
                        try:
                            await model_manager.switch_model(model, client_id=client_id)
                        except ModelLoadError as e:
                            crash = e.crash_record
                            raise HTTPException(status_code=503, detail={
                                "error": f"Model '{model}' failed to load",
                                "message": str(e),
                                "crash_details": crash.to_dict() if crash else None,
                            })
                        except ValueError as e:
                            logger.warning(f"🔒 Switch denied: {e}")
                        except Exception as e:
                            raise HTTPException(status_code=500, detail=f"Model switch failed: {e}")

        model_manager.last_request_time = time.time()
        model_manager.active_requests += 1

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
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.release(request_id)

            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            response = StreamingResponse(
                stream_adapter_generate(),
                media_type="application/x-ndjson",
                headers={"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
            )
            _release_in_finally = False
            return response
        else:
            try:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                ollama_resp = {
                    "model": model,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "response": content,
                    "done": True,
                    "context": [],
                    "total_duration": 0,
                    "load_duration": 0,
                    "prompt_eval_count": data.get("usage", {}).get("prompt_tokens", 0),
                    "eval_count": data.get("usage", {}).get("completion_tokens", 0)
                }
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                return ollama_resp
            except Exception as e:
                await r.aclose()
                await client.aclose()
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                raise e
    finally:
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.release(request_id)


@app.get("/api/version")
async def get_version(client_id: str = Depends(verify_api_key)):
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


# Model listing endpoint (Before catch-all)
@app.get("/v1/models")
async def list_models(client_id: str = Depends(verify_api_key)):
    """List available models from config."""
    models_list = []
    try:
        current = await model_manager.get_current_model()
        for name, cfg in model_manager.models.items():
            model_entry = {
                "id": name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "organization-owner",
                "permission": [],
            }
            if "max_context" in cfg:
                model_entry["max_context"] = cfg["max_context"]
            models_list.append(model_entry)
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        
    return {"object": "list", "data": models_list}


# --- Crash history & status endpoints ---

@app.post("/admin/unload")
async def admin_unload(client_id: str = Depends(verify_api_key)):
    """Stop llama-server immediately to free all VRAM (e.g. before running ComfyUI)."""
    if model_manager.is_unloaded:
        return {"status": "already_unloaded", "message": "llama-server is already stopped"}
    await model_manager.unload()
    return {"status": "unloaded", "message": f"Model '{model_manager.current_model}' unloaded — VRAM is free"}


@app.post("/admin/load")
async def admin_load(request: Request, client_id: str = Depends(verify_api_key)):
    """Reload llama-server. Optionally pass {\"model\": \"name\"} to load a specific model."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    target = body.get("model", None)
    try:
        await model_manager.load(target)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "loaded", "model": model_manager.current_model}


@app.get("/api/crashes")
async def get_crash_history(client_id: str = Depends(verify_api_key)):
    """Return the crash history of llama-server load failures."""
    return {
        "total_crashes": len(model_manager.crash_history),
        "last_crash": model_manager.last_crash.to_dict() if model_manager.last_crash else None,
        "history": model_manager.get_crash_history(),
    }


@app.get("/api/status")
async def get_server_status(client_id: str = Depends(verify_api_key)):
    """Return current model status and backend health."""
    healthy = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LLAMA_SERVER_URL}/health")
            healthy = resp.status_code == 200
    except Exception:
        pass

    vram = get_gpu_metrics()
    idle_minutes = model_manager.idle_unload_minutes
    idle_secs = time.time() - model_manager.last_request_time
    return {
        "current_model": await model_manager.get_current_model(),
        "backend_healthy": healthy,
        "is_unloaded": model_manager.is_unloaded,
        "idle_seconds": round(idle_secs),
        "idle_unload_minutes": idle_minutes,
        "backend_url": LLAMA_SERVER_URL,
        "total_crashes": len(model_manager.crash_history),
        "last_crash": model_manager.last_crash.to_dict() if model_manager.last_crash else None,
        "vram": vram,
        "vram_model_mb": get_model_size(await model_manager.get_current_model()),
        "security": {
            "pinned_model": model_manager.pinned_model,
            "switch_allowlist": list(model_manager._switch_allowlist) if model_manager._switch_allowlist else None,
            "backend_verified": model_manager._model_verified,
        },
    }


# --- Prometheus metrics endpoint (no auth — standard for scraping) ---

@app.get("/metrics")
async def prometheus_metrics():
    """Expose Prometheus-compatible metrics for Grafana/alerting.

    No auth required — standard Prometheus convention for scrape targets.
    """
    update_queue_metrics(inference_queue)
    update_gpu_metrics()
    update_system_metrics(model_manager)
    body, content_type = get_metrics_output()
    return Response(content=body, media_type=content_type)


# --- Queue status endpoint (non-queued, always immediately available) ---

@app.get("/v1/queue/status")
async def queue_status(client_id: str = Depends(verify_api_key)):
    """Return current queue status.  Clients should poll this while waiting."""
    return inference_queue.get_status(client_id=client_id)


# OpenAI-compatible /v1/ routes (used by OpenClaw and other OpenAI-compatible clients)
@app.get("/v1/{path:path}")
async def proxy_v1_get(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{LLAMA_SERVER_URL}/v1/{path}", params=request.query_params)
        return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

@app.post("/v1/{path:path}")
async def proxy_v1_post(path: str, request: Request, client_id: str = Depends(verify_api_key)):
    body = await request.body()

    # Only queue inference endpoints; everything else passes through directly
    is_inference = path in ("chat/completions", "completions", "embeddings")

    if not is_inference:
        timeout = httpx.Timeout(600.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{LLAMA_SERVER_URL}/v1/{path}",
                content=body,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
            )
            return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)

    # --- Inference path: acquire queue slot ---
    # Determine requested model for queue tracking
    requested_model = "_unknown"
    try:
        json_body = json.loads(body)
        requested_model = json_body.get("model", requested_model)
    except (json.JSONDecodeError, Exception):
        pass

    try:
        request_id = await inference_queue.acquire(client_id, requested_model)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail={"error": "queue_timeout", "message": f"Waited {inference_queue.queue_timeout}s in queue"},
        )

    _release_in_finally = True
    try:
        # If llama-server was unloaded, auto-reload before forwarding
        if model_manager.is_unloaded:
            logger.info(f"🔄 Incoming request while unloaded — auto-reloading '{model_manager.current_model}'...")
            try:
                async with _model_switch_lock:
                    if model_manager.is_unloaded:  # double-check under lock
                        await model_manager.load()
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Auto-reload failed: {e}")

        # Track last request time for idle-unload
        model_manager.last_request_time = time.time()
        model_manager.active_requests += 1

        # Auto-switch logic for chat completions (with concurrency lock)
        if path == "chat/completions":
            try:
                json_body = json.loads(body)
                requested_model = json_body.get("model")

                # Resolve aliases and case-insensitive names
                if requested_model:
                    try:
                        requested_model = model_manager.resolve_model(requested_model)
                    except ValueError:
                        pass  # Unknown model — forward to current

                current_model = await model_manager.get_current_model()
                
                if requested_model and requested_model != current_model:
                    if requested_model in model_manager.models:
                        # SECURITY: Check if client is allowed to switch models
                        if not model_manager.is_switch_allowed(client_id):
                            logger.warning(
                                f"🔒 Client '{client_id}' not in switch_allowlist, "
                                f"blocked switch to '{requested_model}'. Forwarding to current model."
                            )
                        else:
                            async with _model_switch_lock:
                                # Re-check after acquiring lock
                                current_model = await model_manager.get_current_model()
                                if requested_model != current_model:
                                    logger.info(f"🔄 Auto-switching backend from {current_model} to {requested_model} (client: {client_id})")
                                    try:
                                        await model_manager.switch_model(requested_model, client_id=client_id)
                                    except ModelLoadError as e:
                                        crash = e.crash_record
                                        detail = {
                                            "error": f"Model '{requested_model}' failed to load",
                                            "message": str(e),
                                            "crash_details": crash.to_dict() if crash else None,
                                        }
                                        logger.error(f"💥 Model load crash: {detail}")
                                        raise HTTPException(status_code=503, detail=detail)
                                    except ValueError as e:
                                        # Pinned model or permission error
                                        logger.warning(f"🔒 Switch denied: {e}")
                                        # Don't raise — just forward to current model
                                    except Exception as e:
                                        logger.error(f"❌ Switch failed: {e}")
                                        raise HTTPException(status_code=500, detail="Model switch failed")
                    else:
                        logger.warning(f"⚠️ Requested model {requested_model} not managed by Guardian. Forwarding to current.")
            except json.JSONDecodeError:
                pass
            except HTTPException:
                raise  # Let model-load errors propagate to the client
            except Exception as e:
                logger.error(f"Error checking model switch: {e}")

        timeout = httpx.Timeout(600.0, connect=10.0)
        logger.info(f"OpenAI-compat request from client '{client_id}': POST /v1/{path}")

        # Detect streaming requests for chat/completions — must proxy SSE in real-time
        is_stream = False
        if path == "chat/completions":
            try:
                json_body = json.loads(body)
                is_stream = json_body.get("stream", False)
            except (json.JSONDecodeError, Exception):
                pass

        if is_stream:
            # Stream SSE chunks in real-time instead of buffering entire response
            client = httpx.AsyncClient(timeout=timeout)
            req = client.build_request(
                "POST",
                f"{LLAMA_SERVER_URL}/v1/{path}",
                content=body,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            )
            try:
                resp = await client.send(req, stream=True)
            except Exception as e:
                await client.aclose()
                raise HTTPException(status_code=502, detail=f"Backend request failed: {e}")

            async def stream_passthrough():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()
                    model_manager.active_requests = max(0, model_manager.active_requests - 1)
                    model_manager.last_request_time = time.time()
                    inference_queue.release(request_id)

            queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
            response = StreamingResponse(
                stream_passthrough(),
                status_code=resp.status_code,
                media_type="text/event-stream",
                headers={
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-length")
                } | {"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
            )
            _release_in_finally = False
            return response
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{LLAMA_SERVER_URL}/v1/{path}",
                    content=body,
                    headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
                )
                model_manager.active_requests = max(0, model_manager.active_requests - 1)
                model_manager.last_request_time = time.time()
                queue_wait_ms = inference_queue.get_queue_wait_ms(request_id)
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=dict(resp.headers) | {"X-Request-Id": request_id, "X-Queue-Wait-Ms": str(int(queue_wait_ms))},
                )
    finally:
        if _release_in_finally:
            model_manager.active_requests = max(0, model_manager.active_requests - 1)
            inference_queue.release(request_id)

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
