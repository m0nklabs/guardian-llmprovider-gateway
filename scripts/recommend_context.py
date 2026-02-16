import json
import os
import yaml

BENCHMARK_FILE = '/home/flip/llama_cpp_guardian/docs/benchmark_results.json'
CONFIG_FILE = '/home/flip/llama_cpp_guardian/config/models.yaml'

def get_optimal_context():
    if not os.path.exists(BENCHMARK_FILE):
        print("Benchmark file not found.")
        return {}

    with open(BENCHMARK_FILE, 'r') as f:
        data = json.load(f)

    model_limits = {}

    # Group by model
    models = {}
    for entry in data:
        name = entry['model_name']
        if name not in models:
            models[name] = []
        models[name].append(entry)

    print("Analyzing benchmark results for optimal context...")
    
    for name, entries in models.items():
        # Filter for successes only
        successes = [e for e in entries if e.get('status') == 'success']
        if not successes:
            print(f"⚠️  {name}: No successful runs found.")
            continue
            
        # Sort by context size
        successes.sort(key=lambda x: x['context_size'])
        
        # Find the max successful context
        max_context = successes[-1]['context_size']
        
        # Determine strict limit (if performance drops drastically, maybe clamp it? 
        # But user asked for max context "now that we can tune". 
        # Usually implies setting it to the max working value we found.)
        
        # Let's just use the max successful context for now.
        model_limits[name] = max_context
        print(f"✅ {name}: Max successful context = {max_context}")

    return model_limits

if __name__ == "__main__":
    limits = get_optimal_context()
    if not limits:
        exit()
        
    print("\nProposed updates for config/models.yaml:")
    for model, ctx in limits.items():
        print(f"{model}: {ctx}")
