import json
import os
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger("Optimizer")

class RequestOptimizer:
    def __init__(self, 
                 legacy_state_file: str = "data/benchmark_state.json",
                 context_results_file: str = "docs/benchmark_results.json"):
        
        # We read from BOTH the integrated benchmark suite (standard optimizations)
        # AND the specialized context benchmark (max limits).
        self.legacy_state_file = legacy_state_file
        self.context_results_file = context_results_file
        
        self.best_configs: Dict[str, Dict] = {} # model_name -> {num_ctx, tps}
        self.last_load = 0
        self.load_benchmarks()

    def load_benchmarks(self):
        """Loads benchmark results and finds the best config for each model."""
        # Check mtimes to see if reload is needed
        mtime1 = os.path.getmtime(self.legacy_state_file) if os.path.exists(self.legacy_state_file) else 0
        mtime2 = os.path.getmtime(self.context_results_file) if os.path.exists(self.context_results_file) else 0
        
        current_max_mtime = max(mtime1, mtime2)
        if current_max_mtime <= self.last_load and self.best_configs:
            return
            
        self.last_load = current_max_mtime
        self.best_configs = {} # Reset
        
        # 1. Load Integrated Suite Results (Standard Optimization)
        if os.path.exists(self.legacy_state_file):
            try:
                with open(self.legacy_state_file, 'r') as f:
                    data = json.load(f)
                
                completed = data.get("completed", [])
                for result in completed:
                    if not result.get("success"): continue
                    
                    config = result.get("config", {})
                    metrics = result.get("metrics", {})
                    model = config.get("model")
                    tps = metrics.get("tps", 0)
                    ctx = config.get("ctx", 0)
                    
                    if not model: continue
                    
                    # Logic: Maximize TPS
                    if model not in self.best_configs or tps > self.best_configs[model]["tps"]:
                        self.best_configs[model] = {"num_ctx": ctx, "tps": tps}
            except Exception as e:
                logger.error(f"Failed to load legacy/integrated state: {e}")

        # 2. Load Context Benchmark Results (Max Limits)
        # These might override if they prove high TPS at high context
        if os.path.exists(self.context_results_file):
            try:
                with open(self.context_results_file, 'r') as f:
                    data = json.load(f)
                
                # New format is list of objects
                completed = data if isinstance(data, list) else []
                
                for result in completed:
                    if result.get("status") != "success": continue
                    
                    model = result.get("model_name")
                    if not model: continue
                    
                    tps = result.get("tokens_per_second", 0)
                    ctx = result.get("context_size", 0)
                    
                    # Logic: Maximize TPS, but give weight to context? 
                    # If this context is larger AND TPS is comparable (within 10%), pick larger?
                    # For now just strict TPS maximization across both sources.
                    
                    if model not in self.best_configs or tps > self.best_configs[model]["tps"]:
                        self.best_configs[model] = {"num_ctx": ctx, "tps": tps}
                        
            except Exception as e:
                logger.error(f"Failed to load context results: {e}")
            
        logger.info(f"Loaded optimizations for {len(self.best_configs)} models")

    def optimize_options(self, model_name: str, current_options: Dict) -> Dict:
        """Injects optimized settings if they are not explicitly set by the user."""
        self.load_benchmarks() # Check for updates
        
        # Clean up model name (remove .gguf etc if needed)
        # The benchmark saves "DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M"
        # The request might ask for "DeepSeek-R1-Distill-Qwen-32B"
        # We need flexible matching? For now exact match.
        
        best = self.best_configs.get(model_name)
        if not best:
             # Try partial match?
             for saved_name in self.best_configs:
                 if model_name in saved_name or saved_name in model_name:
                     best = self.best_configs[saved_name]
                     break
        
        if not best:
            return current_options
        
        # Create a copy to avoid mutating original
        optimized = current_options.copy()
        
        # Only inject if NOT present in request (respect user overrides)
        if "num_ctx" not in optimized:
            optimized["num_ctx"] = best["num_ctx"]
            logger.info(f"⚡ Optimized {model_name}: Injected num_ctx={best['num_ctx']} (Verified Context)")
            
        # We don't optimize batch size in new benchmark yet
        
        return optimized
