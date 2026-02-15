#!/usr/bin/env python3
import subprocess
import time
import requests
import json
import os
import signal
import sys
import glob
import re

# Model Configuration
MODELS_CONFIG_FILE = "/home/flip/llama_cpp_guardian/docs/model_registry.json"
RESULTS_FILE = "/home/flip/llama_cpp_guardian/docs/REAL_BENCHMARK_RESULTS.md"
MODELS_DIR = "/home/flip/models"

# Context sizes to test (incremental) - Up to 256k
# We skip the very small ones (4k) as we are looking for MAX context.
CTX_SIZES = [8192, 12288, 16384, 20480, 24576, 28672, 32768, 40960, 49152, 65536, 81920, 98304, 131072, 163840, 262144]

# KV Cache Quantization Types to test
# Order matters: Test f16 (standard) first. If it fails early, try q8/q4 to see if we can go higher.
KV_CACHE_TYPES = ["f16", "q8_0", "q4_0"]

LLAMA_SERVER_BIN = "/home/flip/llama.cpp/build/bin/llama-server"
HOST = "127.0.0.1"
PORT = 8080
URL = f"http://{HOST}:{PORT}/health"

def get_models_from_config():
    """Reads the JSON config to know exactly what models to test and where to find them."""
    if not os.path.exists(MODELS_CONFIG_FILE):
        print(f"Config file not found: {MODELS_CONFIG_FILE}")
        return {}
    
    with open(MODELS_CONFIG_FILE, "r") as f:
        config = json.load(f)
        
    models = {}
    for entry in config:
        local_name = entry.get("local_name")
        if not local_name:
             local_name = entry.get("filename")
             
        if local_name and local_name.startswith("/"):
            path = local_name
        else:
            path = os.path.join(MODELS_DIR, local_name)
            
        key = os.path.basename(path).replace(".gguf", "")
        models[key] = {
            "path": path,
            "repo": entry.get("repo", "UNKNOWN"),
            "filename": entry.get("filename", "UNKNOWN"),
            "quant_level": entry.get("quant_level", "UNKNOWN"),
            "hf_url": entry.get("hf_url", "")
        }
    return models

def get_models_fallback():
    models = {}
    files = glob.glob(f"{MODELS_DIR}/*.gguf")
    for p in files:
        if "embed" in p: continue
        name = os.path.basename(p).replace(".gguf", "")
        models[name] = {
            "path": p, 
            "repo": "UNKNOWN", 
            "filename": "UNKNOWN", 
            "quant_level": "UNKNOWN", 
            "hf_url": ""
        }
    return models

def write_results_header():
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        f.write("# Real-World Context Benchmark Results (Empirical)\n")
        f.write("**Hardware**: Dual GPU (RTX 5060 Ti 16GB + RTX 3060 12GB) | Total VRAM: ~28GB\n")
        f.write("**Method**: `llama-server` startup test. KV Cache: f16/q8/q4. Stop if f16 maxes out range. Timeout: 90s.\n\n")
        f.write("| Model Name | Source Repo | Model Quant | KV Cache | Max Stable Context | Load Time (s) | Original Filename | Notes |\n")
        f.write("|------------|-------------|-------------|----------|-------------------|---------------|-------------------|-------|\n")

def append_result(model_info, max_ctx, kv_type, load_time):
    name = os.path.basename(model_info["path"]).replace(".gguf", "")
    repo = model_info.get("repo", "UNKNOWN")
    filename = model_info.get("filename", "UNKNOWN")
    quant = model_info.get("quant_level", "UNKNOWN")
    
    with open(RESULTS_FILE, "a") as f:
        f.write(f"| {name} | {repo} | {quant} | {kv_type} | **{max_ctx}** | {load_time:.2f}s | {filename} | Verified |\n")


def kill_existing_server():
    subprocess.run(["pkill", "-9", "-f", "llama-server"], capture_output=True)
    time.sleep(1)

def run_server(model_path, n_ctx, kv_type="f16"):
    kill_existing_server()
    
    cmd = [
        LLAMA_SERVER_BIN,
        "-m", model_path,
        "-c", str(n_ctx),
        "--port", str(PORT),
        "--host", HOST,
        "--n-gpu-layers", "80", # Use limited layers first to simulate partial offload if needed, but user said 'limited cpu offloading' - usually means heavy GPU use. Actually stick with auto for max benchmark? Or force layers?
        # User said "test met beperkte cpu offloading" -> "test with limited CPU offloading" -> meaning mostly GPU? 
        # Or "test scenarios involving limited cpu offloading" -> meaning scenarios where CPU is used?
        # Let's stick to "auto" but enforce timeout.
        "--n-gpu-layers", "auto",
        "--cache-type-k", kv_type,
        "--cache-type-v", kv_type,
        "--batch-size", "256", 
        "--threads", "8",
        "--no-mmap"      
    ]
    
    start_time = time.time()
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for server to be healthy
    max_wait = 95 # 90s + buffer
    server_ready = False
    
    while time.time() - start_time < max_wait:
        if proc.poll() is not None:
            # Encoutered error
            return False, 0

        try:
            resp = requests.get(URL, timeout=1)
            if resp.status_code == 200:
                if resp.json().get("status") == "ok":
                    server_ready = True
                    break
        except:
            pass
        time.sleep(0.5)
        
    load_duration = time.time() - start_time
    
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except:
        proc.kill()
        
    if server_ready and load_duration <= 90:
        return True, load_duration
    else:
        return False, load_duration

    try:
        proc.wait(timeout=2)
    except:
        proc.kill()
    return False

def load_existing_results():
    results = {}
    if not os.path.exists(RESULTS_FILE):
        return results
    
    with open(RESULTS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|") or "Model" in line or "---" in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            # New format (7 columns with quant inserted at index 2)
            # | Name | Repo | Quant | KV | Context | File | Notes |
            try:
                if len(parts) >= 8: # 7 columns + empty ends = 9 split parts usually, but split by | might be different
                     # "" | Name | Repo | Quant | KV | Context | File | Notes | ""
                    model_name = parts[1].strip()
                    kv_type = parts[4].lower().strip()
                    val_str = parts[5].replace("**", "").replace(",", "").strip()
                    max_ctx = int(val_str)
                    results[(model_name, kv_type)] = max_ctx
            except (ValueError, IndexError):
                pass
    return results

def main():
    write_results_header() # Force overwrite header for new columns
    
    existing_tests_map = load_existing_results()
    
    models_config = get_models_from_config()
    print(f"Loaded {len(models_config)} models from config.")
    if not models_config:
         print("Config file empty or missing, falling back to directory scan.")
         models_config = get_models_fallback()
         
    sorted_names = sorted(models_config.keys())
    
    for name in sorted_names:
        info = models_config[name]
        path = info["path"]
        
        print(f"\n=== Benchmarking {name} ===")
        if not os.path.exists(path):
            print(f"    [WARN] Model file not found at: {path}")
            continue
            
        previous_max_ctx = 0
        
        for kv_type in KV_CACHE_TYPES:
            if (name, kv_type) in existing_tests_map:
                previous_max_ctx = existing_tests_map[(name, kv_type)]
                print(f"    Skipping {kv_type} (Already tested, max={previous_max_ctx}).")
                continue
            
            print(f"    -> Testing KV Cache: {kv_type}")
            
            start_index = 0
            if previous_max_ctx > 0:
                 for idx, val in enumerate(CTX_SIZES):
                     if val >= previous_max_ctx:
                         start_index = idx
                         break
                 print(f"       [Optimized] Starting at ctx={CTX_SIZES[start_index]} (skipped {start_index} steps based on previous max {previous_max_ctx})")
            
            result_max_stable = 0
            best_load_time = 0
            
            for ctx in CTX_SIZES[start_index:]:
                print(f"       Testing ctx={ctx} ({kv_type})...", end="\r")
                sys.stdout.flush()
                # Run Server & Measure Time
                success, elapsed = run_server(path, ctx, kv_type)
                
                if success:
                    result_max_stable = ctx
                    best_load_time = elapsed
                    print(f"       Testing ctx={ctx} ({kv_type})... OK ({elapsed:.2f}s)   ")
                else:
                    print(f"       Testing ctx={ctx} ({kv_type})... FAIL (Timeout or Error: {elapsed:.2f}s) ")
                    break
            
            print(f"       => Max Stable ({kv_type}): {result_max_stable}")
            append_result(info, result_max_stable, kv_type, best_load_time)
            
            if result_max_stable > 0:

                previous_max_ctx = result_max_stable

if __name__ == "__main__":
    main()
