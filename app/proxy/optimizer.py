import json
import os
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger("Optimizer")

class RequestOptimizer:
    def __init__(self, state_file: str = "data/benchmark_state.json"):
        self.state_file = state_file
        self.best_configs: Dict[str, Dict] = {} # model_name -> {ctx, batch, tps}
        self.last_load = 0
        self.load_benchmarks()

    def load_benchmarks(self):
        """Loads benchmark results and finds the best config for each model."""
        if not os.path.exists(self.state_file):
            logger.warning(f"Benchmark state file not found at {self.state_file}. Optimization disabled.")
            return

        try:
            # Reload if file changed
            mtime = os.path.getmtime(self.state_file)
            if mtime <= self.last_load:
                return
            
            self.last_load = mtime
            
            with open(self.state_file, 'r') as f:
                data = json.load(f)
            
            completed = data.get("completed", [])
            
            # Reset cache
            self.best_configs = {}
            
            for result in completed:
                if not result.get("success"):
                    continue
                
                config = result.get("config", {})
                metrics = result.get("metrics", {})
                
                model = config.get("model")
                tps = metrics.get("tps", 0)
                
                if not model:
                    continue
                
                # If we haven't seen this model, or this result is faster
                if model not in self.best_configs or tps > self.best_configs[model]["tps"]:
                    self.best_configs[model] = {
                        "num_ctx": config.get("ctx"),
                        "num_batch": config.get("batch"),
                        "tps": tps
                    }
            
            logger.info(f"Loaded optimizations for {len(self.best_configs)} models: {list(self.best_configs.keys())}")
            
        except Exception as e:
            logger.error(f"Failed to load benchmark state: {e}")

    def optimize_options(self, model_name: str, current_options: Dict) -> Dict:
        """Injects optimized settings if they are not explicitly set by the user."""
        self.load_benchmarks() # Check for updates
        
        best = self.best_configs.get(model_name)
        if not best:
            return current_options
        
        # Create a copy to avoid mutating original
        optimized = current_options.copy()
        
        # Only inject if NOT present in request (respect user overrides)
        if "num_ctx" not in optimized:
            optimized["num_ctx"] = best["num_ctx"]
            logger.info(f"⚡ Optimized {model_name}: Injected num_ctx={best['num_ctx']} (Best TPS: {best['tps']:.2f})")
            
        if "num_batch" not in optimized:
            optimized["num_batch"] = best["num_batch"]
            logger.info(f"⚡ Optimized {model_name}: Injected num_batch={best['num_batch']}")
            
        return optimized
