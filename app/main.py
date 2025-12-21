import asyncio
import logging
import os
import signal
import sys
from fastapi import FastAPI
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
