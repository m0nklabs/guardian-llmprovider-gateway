import asyncio
import json
import logging
import os
import pathlib
import signal
import sys
import time
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

# Load .env file early — before any module that reads environment variables
from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

from app.proxy.server import app as proxy_app, state as proxy_state, get_gpu_metrics, get_model_size, inference_queue
from app.proxy.auth import load_api_keys, generate_api_key, _token_fingerprint
from app.proxy.cloud_keys import CloudCredentialStore, parse_guardian_route, mask_api_key
from app.proxy.providers import ProviderRegistry
from app.proxy.server import provider_registry, cloud_cred_store
from app.scheduler.manager import SchedulerManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("guardian.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Guardian")


def _configure_static_mount(application: FastAPI, static_dir: pathlib.Path) -> None:
    if static_dir.is_dir():
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        return

    logger.warning("Static UI directory %s is missing; skipping /static mount", static_dir)


# Main App (UI + API)
app = FastAPI()

# Serve UI
_configure_static_mount(app, pathlib.Path("app/ui/static"))

@app.get("/")
async def read_index():
    return FileResponse("app/ui/index.html")

@app.get("/favicon.ico")
async def favicon():
    # Return empty 1x1 GIF to suppress 404
    from fastapi.responses import Response
    return Response(content=b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b', media_type="image/gif")

@app.get("/api/stats")
async def get_stats():
    # VRAM
    vram = get_gpu_metrics()
    queue_status = inference_queue.get_status()
    
    # Active Models
    active_models = list(proxy_state.scheduler.active_counts.keys())
    
    # Cached Models
    cached_models = []
    for model, timestamp in proxy_state.last_used.items():
        cached_models.append({
            "name": model,
            "size_mb": get_model_size(model),
            "last_used": timestamp
        })
    cached_models.sort(key=lambda x: x["last_used"], reverse=True)

    api_usage = proxy_state.api_usage.snapshot() if hasattr(proxy_state, "api_usage") else {
        "summary": {
            "started_at": None,
            "uptime_seconds": 0,
            "total_requests": 0,
            "total_errors": 0,
            "error_rate_pct": 0.0,
            "unauthenticated_requests": 0,
            "streaming_requests": 0,
            "unique_clients": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "total_request_bytes": 0,
            "total_response_bytes": 0,
            "average_duration_ms": 0.0,
            "requests_last_5m": 0,
            "requests_last_hour": 0,
            "active_requests_count": 0,
            "active_streaming_requests": 0,
            "requests_per_minute": 0.0,
        },
        "active_requests": [],
        "top_clients": [],
        "top_endpoints": [],
        "recent_requests": [],
    }
    
    return {
        "vram": vram,
        "active_models": active_models,
        "queue_size": queue_status.get("queue_length", 0),
        "queue_status": queue_status,
        "optimized_count": 0,
        "cached_models": cached_models,
        "records": [],
        "api_usage": api_usage,
    }


def _read_benchmark_state(data_dir: str = "data") -> dict:
    state_path = pathlib.Path(data_dir) / "benchmark_state.json"
    if not state_path.exists():
        return {"completed": [], "queue": [], "state_file": str(state_path), "state_mtime": None}

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"completed": [], "queue": [], "state_file": str(state_path), "state_mtime": None}

    completed = raw.get("completed", []) if isinstance(raw, dict) else []
    queue = raw.get("queue", []) if isinstance(raw, dict) else []
    try:
        mtime = state_path.stat().st_mtime
    except Exception:
        mtime = None

    return {
        "completed": completed,
        "queue": queue,
        "state_file": str(state_path),
        "state_mtime": mtime,
    }


@app.get("/api/benchmark")
async def get_benchmark_summary():
    state = _read_benchmark_state("data")
    completed = state.get("completed", [])
    queue = state.get("queue", [])

    def _safe_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _get_timestamp(row: dict) -> str:
        ts = row.get("timestamp")
        return ts if isinstance(ts, str) else ""

    last_completed = None
    if completed:
        last_completed = max(completed, key=_get_timestamp)

    # Best TPS per model (computed from completed list)
    best_by_model: dict[str, dict] = {}
    for row in completed:
        if not isinstance(row, dict) or not row.get("success"):
            continue
        config = row.get("config") or {}
        model = config.get("model")
        if not isinstance(model, str) or not model:
            continue
        tps = _safe_float(((row.get("metrics") or {}).get("tps")), 0.0)

        current_best = best_by_model.get(model)
        if current_best is None or tps > _safe_float(current_best.get("best_tps"), 0.0):
            best_by_model[model] = {
                "model": model,
                "best_tps": tps,
                "ctx": config.get("ctx"),
                "batch": config.get("batch"),
                "timestamp": row.get("timestamp"),
            }

    best_list = sorted(best_by_model.values(), key=lambda x: _safe_float(x.get("best_tps"), 0.0), reverse=True)

    return {
        "is_running": False,
        "state_file": state.get("state_file"),
        "state_mtime": state.get("state_mtime"),
        "state_mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state["state_mtime"])) if state.get("state_mtime") else None,
        "completed_count": len(completed),
        "queue_count": len(queue) if isinstance(queue, list) else 0,
        "last_completed": {
            "id": last_completed.get("id"),
            "timestamp": last_completed.get("timestamp"),
            "success": last_completed.get("success"),
            "model": (last_completed.get("config") or {}).get("model"),
            "ctx": (last_completed.get("config") or {}).get("ctx"),
            "batch": (last_completed.get("config") or {}).get("batch"),
            "tps": _safe_float(((last_completed.get("metrics") or {}).get("tps")), 0.0),
            "peak_vram": (last_completed.get("metrics") or {}).get("peak_vram"),
        } if isinstance(last_completed, dict) else None,
        "best_by_model": best_list,
    }


@app.post("/api/benchmark/start")
async def start_benchmark():
    raise HTTPException(status_code=410, detail="Legacy BenchmarkSuite is disabled")


@app.post("/api/benchmark/stop")
async def stop_benchmark():
    raise HTTPException(status_code=410, detail="Legacy BenchmarkSuite is disabled")


# ── Cloud LLM Router Admin API (no auth — LAN dashboard) ─────────────


@app.get("/api/keys")
async def list_api_keys_ui():
    """List all Guardian API keys."""
    keys = load_api_keys()
    result = []
    for token, data in keys.items():
        result.append({
            "key_fingerprint": _token_fingerprint(token),
            "name": data.get("name"),
            "created_at": data.get("created_at"),
        })
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"keys": result}


@app.post("/api/keys")
async def create_api_key_ui(request: Request):
    """Generate a new Guardian API key. Body: {"name": "my-app"}"""
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        name = f"key-{len(load_api_keys()) + 1}"
    prefix = body.get("prefix")
    api_key = generate_api_key(name, metadata={"client": name}, prefix=prefix)
    return {
        "api_key": api_key,
        "key_fingerprint": _token_fingerprint(api_key),
        "name": name,
    }


@app.get("/api/cloud/credentials")
async def list_cloud_creds_ui():
    return {"credentials": cloud_cred_store.list_credentials()}


@app.post("/api/cloud/credentials")
async def add_cloud_cred_ui(request: Request):
    body = await request.json()
    provider = str(body.get("provider", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    models = body.get("models") or []
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key required")
    if not name:
        name = f"{provider}-credential"
    return await cloud_cred_store.add_credential(provider, name, api_key, [str(m) for m in models if m])


@app.delete("/api/cloud/credentials/{cred_id}")
async def delete_cloud_cred_ui(cred_id: str):
    deleted = await cloud_cred_store.delete_credential(cred_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted"}


@app.get("/api/cloud/links")
async def list_cloud_links_ui():
    return {"links": cloud_cred_store.list_links()}


@app.post("/api/cloud/links")
async def link_cloud_cred_ui(request: Request):
    body = await request.json()
    key_fp = str(body.get("guardian_key_fingerprint", "")).strip()
    provider = str(body.get("provider", "")).strip().lower()
    cred_id = str(body.get("credential_id", "")).strip()
    if not key_fp or not provider or not cred_id:
        raise HTTPException(status_code=400, detail="all fields required")
    linked = await cloud_cred_store.link_credential(key_fp, provider, cred_id)
    if not linked:
        raise HTTPException(status_code=404, detail="credential not found")
    return {"status": "linked"}


@app.delete("/api/cloud/links")
async def unlink_cloud_cred_ui(request: Request):
    body = await request.json()
    key_fp = str(body.get("guardian_key_fingerprint", "")).strip()
    provider = str(body.get("provider", "")).strip().lower()
    unlinked = await cloud_cred_store.unlink_credential(key_fp, provider)
    if not unlinked:
        raise HTTPException(status_code=404, detail="link not found")
    return {"status": "unlinked"}


@app.get("/api/cloud/providers")
async def list_providers_ui():
    providers = []
    for p in provider_registry.get_enabled_providers():
        providers.append({"name": p.name, "configured": p.is_configured, "models": p.models})
    return {"providers": providers}


@app.get("/api/cloud/models")
async def list_cloud_models_ui():
    """All cloud models — global + all per-key routes."""
    models = []
    for model_name in provider_registry.get_all_cloud_models():
        entry = provider_registry.build_model_metadata_entry(model_name)
        if entry:
            models.append(entry)
    for c in cloud_cred_store.list_credentials():
        for m in c.get("models", []):
            models.append({
                "id": f"guardian/{c['provider']}/{m}",
                "provider": c["provider"],
                "credential_id": c["id"],
                "credential_name": c["name"],
            })
    return {"models": models}


class GuardianService:
    def __init__(self):
        self.scheduler = SchedulerManager()
        # Proxy listens on 11434 (Direct Entry, Nginx disabled)
        self.proxy_config = uvicorn.Config(proxy_app, host="0.0.0.0", port=11434, log_level="info")
        self.proxy_server = uvicorn.Server(self.proxy_config)
        self.scheduler_task = None
        self.proxy_task = None

    async def start(self):
        logger.info("Starting Llama Server Guardian...")
        
        # Expose internals to UI app
        app.state.scheduler = self.scheduler
        
        # Start Proxy Server
        self.proxy_task = asyncio.create_task(self.proxy_server.serve())
        
        # Start Scheduler
        self.scheduler_task = asyncio.create_task(self.scheduler.run_loop())
        
        # Start UI Server (on port 11437)
        ui_config = uvicorn.Config(app, host="0.0.0.0", port=11437, log_level="info")
        ui_server = uvicorn.Server(ui_config)
        
        await asyncio.gather(
            self.proxy_task,
            self.scheduler_task,
            ui_server.serve()
        )

    def stop(self):
        logger.info("Stopping Guardian...")
        if self.proxy_task: self.proxy_task.cancel()
        if self.scheduler_task: self.scheduler_task.cancel()

if __name__ == "__main__":
    service = GuardianService()
    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        service.stop()
