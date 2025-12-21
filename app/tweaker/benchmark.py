import json
import os
import time
import requests
import logging
import subprocess
from datetime import datetime

logger = logging.getLogger("Tweaker")

class BenchmarkSuite:
    def __init__(self, data_dir="data", config_path="config/settings.yaml"):
        self.data_dir = data_dir
        self.results_file = os.path.join(data_dir, "benchmark_results.json")
        self.state_file = os.path.join(data_dir, "benchmark_state.json")
        self.ollama_url = "http://localhost:11436/api/generate" 
        # We benchmark directly against the internal Ollama instance (11436)
        # to avoid proxy overhead and auth requirements during automated testing.
        self.target_url = "http://localhost:11436/api/generate" 
        
        self.models_to_test = ["deepseek-r1:32b", "gemma2:27b", "gpt-oss:20b"] # TODO: Load from Ollama API
        self.ctx_options = [2048, 4096, 8192, 16384, 24576, 28672, 32768]
        self.batch_options = [128, 256, 512, 1024]
        
        self.current_test = None
        self.is_running = False
        self.best_tps_cache = {} # model -> max_tps

    def check_for_record(self, model, tps, config):
        """Checks if this result is a new record for the model."""
        if model not in self.best_tps_cache:
            # Initialize cache from existing state
            state = self.load_state()
            max_tps = 0
            for r in state.get("completed", []):
                if r.get("success") and r.get("config", {}).get("model") == model:
                    r_tps = r.get("metrics", {}).get("tps", 0)
                    if r_tps > max_tps:
                        max_tps = r_tps
            self.best_tps_cache[model] = max_tps

        if tps > self.best_tps_cache[model]:
            improvement = tps - self.best_tps_cache[model]
            percent = (improvement / self.best_tps_cache[model] * 100) if self.best_tps_cache[model] > 0 else 100
            
            msg = f"🏆 NEW RECORD for {model}! {tps:.2f} t/s (+{percent:.1f}%) [ctx={config['ctx']}, batch={config['batch']}]"
            logger.info(msg)
            print(f"\n{msg}\n")
            
            # Here we could add a webhook or email alert
            self.best_tps_cache[model] = tps

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {"completed": [], "queue": []}

    def save_state(self, state):
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def generate_test_queue(self):
        queue = []
        for model in self.models_to_test:
            for ctx in self.ctx_options:
                for batch in self.batch_options:
                    test_id = f"{model}|{ctx}|{batch}"
                    queue.append({
                        "id": test_id,
                        "model": model,
                        "ctx": ctx,
                        "batch": batch
                    })
        return queue

    def get_vram_usage(self):
        try:
            result = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                encoding="utf-8"
            )
            return sum(int(x) for x in result.strip().split('\n'))
        except:
            return 0

    async def run_suite(self):
        if self.is_running:
            logger.warning("Benchmark suite already running.")
            return

        self.is_running = True
        logger.info("Starting Benchmark Suite...")
        
        state = self.load_state()
        
        # If queue is empty, generate it (fresh start)
        if not state["queue"] and not state["completed"]:
            state["queue"] = self.generate_test_queue()
            self.save_state(state)
        
        # Filter out already completed tests from queue if any overlap (sanity check)
        completed_ids = {x["id"] for x in state["completed"]}
        queue = [t for t in state["queue"] if t["id"] not in completed_ids]
        
        logger.info(f"Found {len(queue)} tests remaining in queue.")

        for test_case in queue:
            if not self.is_running: # Check for stop signal
                break
                
            # User-friendly output
            print(f"Testing {test_case['model']} | ctx={test_case['ctx']} | batch={test_case['batch']} ... ", end="", flush=True)
            
            result = self.run_single_test(test_case)
            
            # Print result
            status = "SUCCESS" if result["success"] else "FAILED"
            tps = result.get("metrics", {}).get("tps", 0)
            vram = result.get("metrics", {}).get("peak_vram", 0)
            
            # Check for New Record
            if result["success"]:
                self.check_for_record(test_case["model"], tps, test_case)

            print(f"{status} | {tps:.2f} t/s | VRAM: {vram}MB")
            
            # Update State
            state["completed"].append(result)
            # Remove from queue in state (or just rely on completed list)
            # Let's keep queue static and just append to completed for history
            self.save_state(state)
            
            # Cool down
            time.sleep(2)

        self.is_running = False
        logger.info("Benchmark Suite Finished or Stopped.")

    def run_single_test(self, test_case):
        model = test_case["model"]
        ctx = test_case["ctx"]
        batch = test_case["batch"]
        
        payload = {
            "model": model,
            "prompt": "Write a short story about a llama who learns to code in Python. The story should be about 200 words long.",
            "stream": False,
            "options": {
                "num_ctx": ctx,
                "num_batch": batch,
                "num_gpu": 99,
                "temperature": 0.7
            }
        }
        
        start_vram = self.get_vram_usage()
        start_time = time.time()
        
        try:
            response = requests.post(self.target_url, json=payload, timeout=600)
            response.raise_for_status()
            data = response.json()
            
            end_vram = self.get_vram_usage()
            
            total_duration = data.get("total_duration", 0) / 1e9
            eval_count = data.get("eval_count", 0)
            eval_duration = data.get("eval_duration", 0) / 1e9
            tps = eval_count / eval_duration if eval_duration > 0 else 0
            
            return {
                "id": test_case["id"],
                "timestamp": datetime.now().isoformat(),
                "success": True,
                "metrics": {
                    "tps": tps,
                    "total_duration": total_duration,
                    "vram_delta": end_vram - start_vram,
                    "peak_vram": end_vram
                },
                "config": test_case
            }
            
        except Exception as e:
            logger.error(f"Test failed {test_case['id']}: {e}")
            return {
                "id": test_case["id"],
                "timestamp": datetime.now().isoformat(),
                "success": False,
                "error": str(e),
                "config": test_case
            }

    def stop(self):
        self.is_running = False
