import json
import re
import os

BENCHMARK_FILE = '/home/flip/llama_cpp_guardian/docs/benchmark_results.json'
CONFIG_FILE = '/home/flip/llama_cpp_guardian/config/models.yaml'

def get_max_contexts():
    if not os.path.exists(BENCHMARK_FILE):
        return {}
    
    with open(BENCHMARK_FILE, 'r') as f:
        data = json.load(f)
        
    path_to_ctx = {}
    
    # Organize by filename
    for entry in data:
        p = entry.get('model_path')
        if p:
            base = os.path.basename(p)
            if base not in path_to_ctx:
                path_to_ctx[base] = []
            path_to_ctx[base].append(entry)
            
    # Find max context
    final_ctx = {}
    for base, entries in path_to_ctx.items():
        successes = [e for e in entries if e.get('status') == 'success']
        if successes:
            successes.sort(key=lambda x: x['context_size'])
            final_ctx[base] = successes[-1]['context_size']
            
    print("Optimization Plan:")
    for k, v in final_ctx.items():
        print(f"  {k}: {v}")
    
    return final_ctx

def update_config():
    max_contexts = get_max_contexts()
    if not max_contexts:
        print("No benchmark data found.")
        return

    with open(CONFIG_FILE, 'r') as f:
        lines = f.readlines()
        
    new_lines = []
    current_target_ctx = None
    
    for line in lines:
        stripped = line.strip()
        
        # Check for path definition
        if 'path:' in stripped and not stripped.startswith('#'):
            parts = stripped.split('path:', 1)
            if len(parts) > 1:
                path_val = parts[1].strip().split('#')[0].strip()
                base_name = os.path.basename(path_val)
                
                if base_name in max_contexts:
                    current_target_ctx = max_contexts[base_name]
                    # Print update plan for verify
                    # print(f"  Matched {base_name} -> {current_target_ctx}")
                else:
                    current_target_ctx = None
            
            new_lines.append(line)
            continue
            
        # Check for context definition
        if 'context:' in stripped and current_target_ctx is not None and not stripped.startswith('#'):
            indent = line.split('context:', 1)[0]
            new_lines.append(f"{indent}context: {current_target_ctx} # Auto-tuned\n")
            current_target_ctx = None # Applied
        else:
            # If we see another key start, invalidate target context
            if verify_key_start(line) and 'path:' not in line:
                current_target_ctx = None
            new_lines.append(line)

    with open(CONFIG_FILE, 'w') as f:
        # f.writelines(new_lines)
        pass
    
    # Actually write
    with open(CONFIG_FILE, 'w') as f:
        f.writelines(new_lines)
        
    print(f"Updated {CONFIG_FILE}")

def verify_key_start(line):
    # Heuristic: line starts with non-whitespace or just indent level change?
    # YAML is indentation based.
    # If line has less indentation than previous 'path:', it's a new block?
    # Simple check: does it contain a colon and start with a char?
    # Or start with '-' for list items?
    s = line.strip()
    if not s: return False
    if s.startswith('#'): return False
    # If it is a key: value pair
    if ':' in s:
        key = s.split(':', 1)[0].strip()
        if not key.startswith('&') and not key.startswith('*'):
            return True
    return False

if __name__ == '__main__':
    update_config()
