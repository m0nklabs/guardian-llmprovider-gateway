#!/usr/bin/env python3
import json
import os
import sys
from collections import defaultdict

# Configuration
JSON_RESULTS_FILE = "/home/flip/llama_cpp_guardian/docs/benchmark_results.json"
SUMMARY_OUTPUT_FILE = "/home/flip/llama_cpp_guardian/docs/BENCHMARK_SUMMARY.md"

def load_results():
    if not os.path.exists(JSON_RESULTS_FILE):
        print(f"❌ Results file not found: {JSON_RESULTS_FILE}")
        sys.exit(1)
    
    try:
        with open(JSON_RESULTS_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Error decoding JSON: {e}")
        sys.exit(1)

def analyze_model_performance(results):
    # Group by model + quantization
    models = defaultdict(lambda: {
        "kv_types": defaultdict(lambda: {"max_ctx": 0, "tps_avg": [], "load_times": []})
    })

    for entry in results:
        model_name = entry.get("model_name", "Unknown")
        # Extract model quantization if part of name or filename, otherwise assume from registry
        # For this summary, we'll try to group by the full model identifier used in the test
        
        # We also want to know the model quantization (e.g. Q4_K_M vs Q8_0)
        # Often this is in the model_name or filename
        model_id = model_name
        
        kv_type = entry.get("kv_type", "unknown")
        ctx = entry.get("context_size", 0)
        tps = entry.get("tokens_per_second", 0)
        load_time = entry.get("load_time_seconds", 0)
        status = entry.get("status", "failed")

        if status == "success":
            # Update Max Context
            if ctx > models[model_id]["kv_types"][kv_type]["max_ctx"]:
                 models[model_id]["kv_types"][kv_type]["max_ctx"] = ctx
            
            # Collect TPS (only for non-baseline context sizes to avoid skewing? Or all?)
            # Let's collect all for now, maybe filter low context later
            if tps > 0:
                models[model_id]["kv_types"][kv_type]["tps_avg"].append(tps)
            
            models[model_id]["kv_types"][kv_type]["load_times"].append(load_time)

    return models

def generate_markdown(models):
    lines = []
    lines.append("# 📊 Benchmark Analysis Summary")
    lines.append(f"**Generated:** {os.popen('date').read().strip()}")
    lines.append("")
    lines.append("This document summarizes the raw JSON benchmark data into actionable insights.")
    lines.append("")

    lines.append("## 🏆 Global Rankings")
    
    # 1. Max Stable Context (Overall)
    max_ctx_list = []
    for model_id, data in models.items():
        # Try finding max context across all KV types
        max_ctx = 0
        best_kv = ""
        for kv, metrics in data["kv_types"].items():
            if metrics["max_ctx"] > max_ctx:
                max_ctx = metrics["max_ctx"]
                best_kv = kv
        if max_ctx > 0:
            max_ctx_list.append((max_ctx, model_id, best_kv))
    
    max_ctx_list.sort(key=lambda x: x[0], reverse=True)
    
    lines.append("### 🧠 Top 5 Models by Max Context")
    lines.append("| Rank | Model | Max Context | Best KV Type |")
    lines.append("|------|-------|-------------|--------------|")
    for i, (ctx, model, kv) in enumerate(max_ctx_list[:10], 1):
        def fmt_ctx(val):
            if val >= 1024*1024: return f"**{val//(1024*1024)}M**"
            if val >= 1024: return f"{val//1024}k"
            return str(val)
        lines.append(f"| #{i} | {model} | {fmt_ctx(ctx)} | {kv} |")
    
    lines.append("")

    # 2. Speed (TPS) Analysis
    tps_list = []
    for model_id, data in models.items():
        # Average TPS across all KV types (filtering out > 1000 which looks like placeholder/error)
        all_tps = []
        for kv, metrics in data["kv_types"].items():
            valid_tps = [t for t in metrics["tps_avg"] if 0 < t < 500] 
            all_tps.extend(valid_tps)
        
        if all_tps:
           avg_tps = sum(all_tps) / len(all_tps)
           tps_list.append((avg_tps, model_id))
           
    tps_list.sort(key=lambda x: x[0], reverse=True)

    lines.append("### ⚡ Top 5 Fastest Models (Avg TPS)")
    lines.append("| Rank | Model | Avg TPS |")
    lines.append("|------|-------|---------|")
    for i, (tps, model) in enumerate(tps_list[:10], 1):
        lines.append(f"| #{i} | {model} | {tps:.2f} |")

    lines.append("")

    lines.append("## 🔬 Detailed Breakdown")
    lines.append("| Rank | Model | Quant | F16 Context | Q8 Context | Q4 Context | Avg TPS (F16) | Avg TPS (Q4) | Impact (Q4 vs F16) |")
    lines.append("|------|-------|-------|-------------|------------|------------|---------------|--------------|--------------------|")

    # Sort models by name
    sorted_models = sorted(models.keys())

    for i, model_id in enumerate(sorted_models, 1):
        data = models[model_id]
        
        # Try to infer model quant from name
        quant_label = "Unknown"
        if "Q8_0" in model_id: quant_label = "Q8_0"
        elif "Q4_K_M" in model_id: quant_label = "Q4_K_M"
        elif "f16" in model_id.lower(): quant_label = "F16"
        
        # Get max contexts
        f16_ctx = data["kv_types"].get("f16", {}).get("max_ctx", "-")
        q8_ctx = data["kv_types"].get("q8_0", {}).get("max_ctx", "-")
        q4_ctx = data["kv_types"].get("q4_0", {}).get("max_ctx", "-")

        # Get Avg TPS
        def get_avg_tps(kv):
            tps_list = [t for t in data["kv_types"].get(kv, {}).get("tps_avg", []) if 0 < t < 500]
            if not tps_list: return 0
            return sum(tps_list)/len(tps_list)

        tps_f16 = get_avg_tps('f16')
        tps_q4 = get_avg_tps('q4_0')
        
        # Calculate impact
        impact = "-"
        if tps_f16 > 0 and tps_q4 > 0:
            diff = ((tps_q4 - tps_f16) / tps_f16) * 100
            impact = f"{diff:+.1f}%"

        def fmt_ctx(val):
            if val == "-": return "-"
            if val >= 1024*1024: return f"**{val//(1024*1024)}M**"
            if val >= 1024: return f"{val//1024}k"
            return str(val)
            
        def fmt_tps(val):
            return f"{val:.2f}" if val > 0 else "-"

        lines.append(f"| {i} | {model_id} | {quant_label} | {fmt_ctx(f16_ctx)} | {fmt_ctx(q8_ctx)} | {fmt_ctx(q4_ctx)} | {fmt_tps(tps_f16)} | {fmt_tps(tps_q4)} | {impact} |")

    return "\n".join(lines)

def main():
    print("🔍 Reading benchmark results...")
    results = load_results()
    print(f"   Found {len(results)} data points.")

    print("🧠 Analyzing performance metrics...")
    stats = analyze_model_performance(results)

    print("📝 Generating summary document...")
    markdown = generate_markdown(stats)

    with open(SUMMARY_OUTPUT_FILE, "w") as f:
        f.write(markdown)
    
    print(f"✅ Summary saved to: {SUMMARY_OUTPUT_FILE}")

if __name__ == "__main__":
    main()
