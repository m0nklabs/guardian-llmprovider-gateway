#!/usr/bin/env python3
import subprocess
import time
import requests
import json
import os
import sys
import glob
from datetime import datetime

# Model Configuration
# Use environment variables for flexibility
MODELS_CONFIG_FILE = os.getenv("MODELS_CONFIG_FILE", "/home/flip/llama_cpp_guardian/docs/model_registry.json")
RESULTS_FILE = os.getenv("RESULTS_FILE", "/home/flip/llama_cpp_guardian/docs/REAL_BENCHMARK_RESULTS.md")
JSON_RESULTS_FILE = os.getenv("JSON_RESULTS_FILE", "/home/flip/llama_cpp_guardian/docs/benchmark_results.json")
MODELS_DIR = os.getenv("MODELS_DIR", "/home/flip/models")

# Context sizes to test (incremental)
CTX_SIZES = [8192, 16384, 32768, 65536, 98304, 131072, 262144, 524288, 1048576]

# KV Cache Quantization Types to test
KV_CACHE_TYPES = ["f16", "q8_0", "q4_0"]

# Server binary path
LLAMA_SERVER_BIN = os.getenv("LLAMA_SERVER_BIN", "/home/flip/ik_llama_cpp_build/build/bin/llama-server")
HOST = "127.0.0.1"
PORT = int(os.getenv("LLAMA_PORT", "11442"))
URL = f"http://{HOST}:{PORT}/health"

def load_json_results():
    """Loads all previous benchmark results from JSON file."""
    if not os.path.exists(JSON_RESULTS_FILE):
        return []
    try:
        with open(JSON_RESULTS_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"[WARN] Could not decode {JSON_RESULTS_FILE}. Starting fresh.")
        return []

def save_json_result(entry):
    """Appends a single result entry to the JSON file immediately."""
    # Optimization: Just read, append, write. No need to re-scan if we trust our local state,
    # but for safety against manual edits, we read-modify-write.
    results = load_json_results()
    results.append(entry)
    with open(JSON_RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

def is_test_already_done(results, model_path, ctx, kv_type):
    """Checks if a successful test for this configuration already exists."""
    for r in results:
        if (r.get("model_path") == model_path and 
            r.get("context_size") == ctx and 
            r.get("kv_type") == kv_type and
            r.get("status") == "success"):
            return True, r
    return False, None

def get_models_from_config():
    """Reads the JSON config to know exactly what models to test and where to find them."""
    if not os.path.exists(MODELS_CONFIG_FILE):
        print(f"Config file not found: {MODELS_CONFIG_FILE}")
        return {}
    
    with open(MODELS_CONFIG_FILE, "r") as f:
        data = json.load(f)
        config = data.get("models", [])
        
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
        if "embed" in p:
            continue
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
        f.write("| Model Name | Source Repo | Model Quant | KV Cache | Max Stable Context | Load Time (s) | KV Allocation Delta (s) | Original Filename | Notes |\n")
        f.write("|------------|-------------|-------------|----------|-------------------|---------------|-------------------------|-------------------|-------|\n")

def append_result(model_info, max_ctx, kv_type, load_time, base_load_time=0):
    name = os.path.basename(model_info["path"]).replace(".gguf", "")
    repo = model_info.get("repo", "UNKNOWN")
    filename = model_info.get("filename", "UNKNOWN")
    quant = model_info.get("quant_level", "UNKNOWN")
    
    kv_delta = max(0, load_time - base_load_time) if base_load_time > 0 else 0.0
    
    with open(RESULTS_FILE, "a") as f:
        f.write(f"| {name} | {repo} | {quant} | {kv_type} | **{max_ctx}** | {load_time:.2f}s | +{kv_delta:.2f}s | {filename} | Verified |\n")


def kill_existing_server():
    subprocess.run(["pkill", "-9", "-f", "llama-server"], capture_output=True)
    time.sleep(1)

def run_server(model_path, n_ctx, kv_type="f16"):
    """
    Start the llama-server with the given context size and KV cache type.
    Measures load time and runs a simple inference test.
    
    Returns:
        tuple: (success, load_time, inference_time, output_chars, tps)
    """
    kill_existing_server()
    
    cmd = [
        LLAMA_SERVER_BIN,
        "-m", model_path,
        "-c", str(n_ctx),
        "--port", str(PORT),
        "--host", HOST,
        "--n-gpu-layers", "99", # Focus on VRAM for KV cache
        "--cache-type-k", kv_type,
        "--cache-type-v", kv_type,
        "--batch-size", "256", 
        "--threads", "8",
        "--no-mmap"      
    ]
    
    start_time = time.time()
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace"
        )
    except Exception as e:
        print(f"[ERROR] Failed to start server: {e}")
        return False, 0, 0, 0, 0
    
    # Wait for server to be healthy
    max_wait = 95 # 90s + buffer
    server_ready = False
    
    while time.time() - start_time < max_wait:
        if proc.poll() is not None:
            # Encoutered error
            stderr_out = proc.stderr.read() if proc.stderr else "Unknown error"
            print(f"\n[ERROR] Server exited early! code={proc.returncode}")
            print(f"STDERR:\n{stderr_out}\n")
            return False, 0, 0, 0, 0

        try:
            resp = requests.get(URL, timeout=1)
            if resp.status_code == 200:
                if resp.json().get("status") == "ok":
                    server_ready = True
                    break
        except requests.RequestException:
            pass
        time.sleep(0.5)
        
    load_duration = time.time() - start_time
    
    if not server_ready:
        print(f"    [FAIL] Server failed to become ready in {max_wait}s")
        kill_process_tree(proc)
        return False, load_duration, 0, 0, 0

    # ---------------------------
    # Inference Benchmark
    # ---------------------------
    inference_duration = 0.0
    output_chars = 0
    tps = 0.0

    # Ensure context fits within predict limit if context is huge
    # For inference speed test, we just want a standard output length
    
    prompt = "Explain in detail how a bicycle works, focusing on the physics of stability and the mechanism of the gears."
    
    inf_start = time.time()
    try:
        payload = {
            "prompt": prompt,
            "n_predict": 256, 
            "temperature": 0.7
        }
        comp_url = f"http://{HOST}:{PORT}/completion"
        
        resp = requests.post(comp_url, json=payload, timeout=60)
        
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", "")
            output_chars = len(content)
            
            # Check for metrics if available (timings)
            timings = data.get("timings", {})
            predicted_ms = timings.get("predicted_ms", 0)
            predicted_n = timings.get("predicted_n", 0)
            
            if predicted_n > 0 and predicted_ms > 0:
                 tps = predicted_n / (predicted_ms / 1000.0)
            
            inference_duration = time.time() - inf_start
        else:
            print(f"    [WARN] Inference failed: {resp.status_code}")
            
    except Exception as e:
        print(f"    [WARN] Inference exception: {e}")
        inference_duration = 0.0

    # Cleanup
    kill_process_tree(proc)
        
    return True, load_duration, inference_duration, output_chars, tps


def kill_process_tree(proc):
    """Cleanly kill a process and avoid zombies."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except:
                pass

def run_server_wrapper(model_path, n_ctx, kv_type="f16"):
    # Wrapper to handle the multiple return values cleanly in loop
    # returns: success, load_time, inference_time, tokens_count, tps
    try:
        return run_server(model_path, n_ctx, kv_type)
    except Exception as e:
        print(f"Error in wrapper: {e}")
        return False, 0, 0, 0, 0

def main():
    write_results_header()
    
    # Load all historical results
    json_history = load_json_results()
    print(f"Loaded {len(json_history)} previous test results from JSON.")
    
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
            
        for kv_type in KV_CACHE_TYPES:
            print(f"    -> Testing KV Cache: {kv_type}")
            
            # 1. Baseline Check (ctx=512)
            is_done, prev_data = is_test_already_done(json_history, path, 512, kv_type)
            
            if is_done and prev_data:
                baseline_time = prev_data.get("load_time_seconds", 0.0)
                print(f"       Baseline (ctx=512): FOUND in JSON ({baseline_time:.2f}s)")
                baseline_success = True
            else:
                baseline_time = 0.0 # Default if failed
                print("       Testing BASELINE (ctx=512)...", end="\r")
                baseline_success, baseline_time, inf_time, out_len, tps_val = run_server_wrapper(path, 512, kv_type)
                
                # Save result regardless of success/fail
                kv_delta_baseline = 0.0
                  
                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "model_name": name,
                    "model_path": path,
                    "context_size": 512,
                    "kv_type": kv_type,
                    "status": "success" if baseline_success else "failed",
                    "load_time_seconds": baseline_time,
                    "kv_delta_seconds": kv_delta_baseline,
                    "inference_time_seconds": inf_time,
                    "output_length_chars": out_len,
                    "tokens_per_second": tps_val
                }
                save_json_result(entry)
                json_history.append(entry)
                
                if baseline_success:
                    print(f"       Testing BASELINE (ctx=512)... OK ({baseline_time:.2f}s) | TPS: {tps_val:.2f}   ")
                else:
                    print("       Testing BASELINE (ctx=512)... FAIL")
                    baseline_time = 0

            # 2. Iterate Context Sizes
            result_max_stable = 0
            best_load_time = 0
            
            for ctx in CTX_SIZES:
                # Check if we already have this specific test point
                is_done, prev_data = is_test_already_done(json_history, path, ctx, kv_type)
                
                if is_done and prev_data:
                    elapsed = prev_data.get("load_time_seconds", 0.0)
                    delta = prev_data.get("kv_delta_seconds", 0.0)
                    tps_val = prev_data.get("tokens_per_second", 0.0)
                    print(f"       ctx={ctx}: FOUND in JSON ({elapsed:.2f}s, delta={delta:.2f}s, tps={tps_val:.2f})")
                    result_max_stable = ctx
                    best_load_time = elapsed
                    continue
                
                print(f"       Testing ctx={ctx} ({kv_type})...", end="\r")
                sys.stdout.flush()
                
                success, elapsed, inf_time, out_len, tps_val = run_server_wrapper(path, ctx, kv_type)
                
                kv_delta = max(0, elapsed - baseline_time) if baseline_time > 0 else 0
                
                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "model_name": name,
                    "model_path": path,
                    "context_size": ctx,
                    "kv_type": kv_type,
                    "status": "success" if success else "failed",
                    "load_time_seconds": elapsed,
                    "kv_delta_seconds": kv_delta,
                    "inference_time_seconds": inf_time,
                    "output_length_chars": out_len,
                    "tokens_per_second": tps_val
                }
                save_json_result(entry)
                json_history.append(entry)
                
                if success:
                    print(f"       Testing ctx={ctx} ({kv_type})... OK ({elapsed:.2f}s) [KV: +{kv_delta:.2f}s] [TPS: {tps_val:.2f}]  ")
                    result_max_stable = ctx
                    best_load_time = elapsed
                else:
                    print(f"       Testing ctx={ctx} ({kv_type})... FAIL (Timeout or Error: {elapsed:.2f}s) ")
                    break # Stop testing larger contexts for this KV type
            
            print(f"       => Max Stable ({kv_type}): {result_max_stable}")
            # Append to markdown only if we actually did something or verified it
            if result_max_stable > 0:
                # We append to markdown summary for humans
                append_result(info, result_max_stable, kv_type, best_load_time, baseline_time)

if __name__ == "__main__":
    main()
