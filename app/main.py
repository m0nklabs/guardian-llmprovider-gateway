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

from app.proxy.server import app as proxy_app, state as proxy_state, get_gpu_metrics, get_model_size
from app.scheduler.manager import SchedulerManager
from app.tweaker.benchmark import BenchmarkSuite

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

# Main App (UI + API)
app = FastAPI()

# Serve UI
app.mount("/static", StaticFiles(directory="app/ui/static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("app/ui/index.html")

@app.get("/api/stats")
async def get_stats():
    # VRAM
    vram = get_gpu_metrics()
    
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
    
    # Records
    benchmark = getattr(app.state, "benchmark", None)
    records = []
    if benchmark:
        for model, tps in benchmark.best_tps_cache.items():
             records.append({
                 "model": model,
                 "config": "Best Config", 
                 "tps": tps,
                 "improvement": 0 
             })
             
    return {
        "vram": vram,
        "active_models": active_models,
        "queue_size": 0,
        "optimized_count": 0,
        "cached_models": cached_models,
        "records": records
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
    benchmark = getattr(app.state, "benchmark", None)
    state = _read_benchmark_state(benchmark.data_dir if benchmark else "data")
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
        "is_running": bool(getattr(benchmark, "is_running", False)) if benchmark else False,
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
    benchmark = getattr(app.state, "benchmark", None)
    if not benchmark:
        raise HTTPException(status_code=503, detail="Benchmark suite not initialized")

    if getattr(benchmark, "is_running", False):
        return {"started": False, "reason": "already_running"}

    # Run in the background so the API stays responsive
    asyncio.create_task(benchmark.run_suite())
    return {"started": True}


@app.post("/api/benchmark/stop")
async def stop_benchmark():
    benchmark = getattr(app.state, "benchmark", None)
    if not benchmark:
        raise HTTPException(status_code=503, detail="Benchmark suite not initialized")

    benchmark.stop()
    return {"stopped": True}

class GuardianService:
    def __init__(self):
        self.scheduler = SchedulerManager()
        self.benchmark = BenchmarkSuite()
        # Proxy listens on 11434 (Direct Entry, Nginx disabled)
        self.proxy_config = uvicorn.Config(proxy_app, host="0.0.0.0", port=11434, log_level="info")
        self.proxy_server = uvicorn.Server(self.proxy_config)
        self.scheduler_task = None
        self.proxy_task = None

    async def start(self):
        logger.info("Starting Ollama Guardian...")
        
        # Expose internals to UI app
        app.state.scheduler = self.scheduler
        app.state.benchmark = self.benchmark
        
        # Start Proxy Server
        self.proxy_task = asyncio.create_task(self.proxy_server.serve())
        
        # Start Scheduler
        self.scheduler_task = asyncio.create_task(self.scheduler.run_loop(self.benchmark))
        
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
