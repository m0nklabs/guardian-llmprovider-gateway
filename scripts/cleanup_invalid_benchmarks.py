import json
import os

RESULTS_FILE = '/home/flip/llama_cpp_guardian/docs/benchmark_results.json'
STATE_FILE = '/home/flip/llama_cpp_guardian/data/benchmark_state.json'
INVALID_TPS = 1000000.0

def cleanup():
    # 1. Process Results File
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, 'r') as f:
            data = json.load(f)
        
        filtered_data = [d for d in data if d.get('tokens_per_second') != INVALID_TPS]
        removed_count = len(data) - len(filtered_data)
        
        if removed_count > 0:
            print(f"Removing {removed_count} invalid entries from {RESULTS_FILE}...")
            # Backup first? Nah, user asked for fix.
            with open(RESULTS_FILE, 'w') as f:
                json.dump(filtered_data, f, indent=2)
        else:
            print(f"No invalid entries found in {RESULTS_FILE}.")
    else:
        print(f"Results file {RESULTS_FILE} not found.")

    # 2. Process State File
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        
        completed = state.get('completed', [])
        # Also need to check if 'metrics.tps' is 1000000.0
        # Wait, the structure in state file has tps in "metrics": {"tps": ...}
        
        valid_completed = []
        removed_state_count = 0
        
        for item in completed:
            metrics = item.get('metrics', {})
            tps = metrics.get('tps')
            if tps == INVALID_TPS:
                removed_state_count += 1
                continue
            valid_completed.append(item)
            
        if removed_state_count > 0:
            print(f"Removing {removed_state_count} invalid entries from {STATE_FILE} state tracking...")
            state['completed'] = valid_completed
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
        else:
            print(f"No invalid entries found in {STATE_FILE}.")
            
    else:
        print(f"State file {STATE_FILE} not found.")

if __name__ == "__main__":
    cleanup()
