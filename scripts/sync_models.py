#!/home/flip/llama_cpp_guardian/venv/bin/python3
import os
import yaml
from pathlib import Path
import logging
import subprocess
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MODELS_DIR = Path("/home/flip/models")
CONFIG_FILE = Path("/home/flip/llama_cpp_guardian/config/models.yaml")

# Intelligent defaults based on filename/size
DEFAULT_CONTEXT = 32768
DEFAULT_NGL = 99

def get_model_defaults(filename):
    """Return context size based on model heuristics"""
    name = filename.lower()
    if "70b" in name: return 8192  # Large model, limit context
    if "command-r" in name: return 16384
    return DEFAULT_CONTEXT

def load_config():
    if not CONFIG_FILE.exists():
        logger.warning(f"Config file {CONFIG_FILE} not found. Creating new...")
        return {"models": {}}
    
    with open(CONFIG_FILE, 'r') as f:
        try:
            return yaml.safe_load(f) or {"models": {}}
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML: {e}")
            return {"models": {}}

def save_config(config):
    # Ensure directory exists
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    logger.info(f"Saved configuration to {CONFIG_FILE}")

def sync_models():
    logger.info("Starting model synchronization...")
    
    config = load_config()
    current_models = config.get("models", {})
    if current_models is None: current_models = {}

    # 1. Identify and remove stale entries
    stale_keys = []
    known_paths = set()
    
    for model_name, details in list(current_models.items()):
        # Handle alias anchors (might be parsed as dict references by pyyaml)
        if hasattr(details, 'get'):
            path = details.get("path")
            if path:
                if not os.path.exists(path):
                    logger.warning(f"Model '{model_name}' path not found: {path} -> Marking for removal")
                    stale_keys.append(model_name)
                else:
                    known_paths.add(str(Path(path).resolve()))

    if stale_keys:
        logger.info(f"Removing {len(stale_keys)} stale models...")
        for key in stale_keys:
            del current_models[key]

    # 1.5 Deduplicate: Remove redundant entries pointing to the same file path
    # Prefer the shortest key name (usually the cleanest)
    path_to_keys = {}
    for k, v in list(current_models.items()):
        if not isinstance(v, dict): continue
        p = v.get('path')
        if not p: continue
        try:
            path_canon = str(Path(p).resolve())
            if path_canon not in path_to_keys:
                path_to_keys[path_canon] = []
            path_to_keys[path_canon].append(k)
        except Exception:
            continue
            
    dedupe_count = 0
    for path_canon, keys in path_to_keys.items():
        if len(keys) > 1:
            # Sort by length (asc) then name (asc)
            keys.sort(key=lambda x: (len(x), x))
            keep = keys[0]
            for remove in keys[1:]:
                logger.info(f"Removing redundant entry '{remove}' (keeping '{keep}')")
                del current_models[remove]
                dedupe_count += 1
    
    if dedupe_count > 0:
        logger.info(f"Removed {dedupe_count} duplicate entries")

    # 2. Scan directory for new models
    if not MODELS_DIR.exists():
        logger.error(f"Models directory not found: {MODELS_DIR}")
        return

    new_count = 0
    for file_path in MODELS_DIR.glob("*.gguf"):
        # Robust filtering: Ignore hidden files and bad downloads
        if file_path.name.startswith('.'):
            continue
            
        if not file_path.is_file():
            continue
            
        try:
            # Check file size - ignore files < 1MB (e.g. "Entry not found" text files)
            if file_path.stat().st_size < 1024 * 1024:
                logger.warning(f"Skipping tiny file (likely broken download): {file_path.name} ({file_path.stat().st_size} bytes)")
                continue
        except Exception as e:
            logger.warning(f"Could not stat file {file_path.name}: {e}")
            continue

        abs_path = str(file_path.resolve())
        if abs_path in known_paths:
            continue
            
        # Add new model
        model_name = file_path.stem
        # Clean up name: remove common suffixes for nicer display names
        clean_name = model_name
        for suffix in ["-latest", "-Q4_K_M", "-q4_k_m", "-Q8_0", "-q8_0", "-F16", "-f16", ".Q4_K_M", ".Q8_0", "-Q4_K_M-64k", "-64k"]:
            clean_name = clean_name.replace(suffix, "")
        
        # Avoid empty names
        if not clean_name:
            clean_name = model_name
            
        # Add entry ONLY if it doesn't exist (respect existing/manual entries)
        if clean_name not in current_models:
            logger.info(f"Found new model: {file_path.name} -> Adding as '{clean_name}'")
            current_models[clean_name] = {
                "path": abs_path,
                "context": get_model_defaults(file_path.name),
                "ngl": DEFAULT_NGL
            }
            new_count += 1
        
        # DISABLED: Also add original filename (minus extension) as alias if different
        # if clean_name != model_name:
        #      current_models[model_name] = {
        #         "path": abs_path,
        #         "context": get_model_defaults(file_path.name),
        #         "ngl": DEFAULT_NGL
        #     }
            
        known_paths.add(abs_path)

    # 3. Update config and restart if needed
    config["models"] = current_models
    
    if stale_keys or new_count > 0 or dedupe_count > 0:
        save_config(config)
        logger.info("Config updated. Restarting services...")
        try:
            # Try reloading first, if supported, otherwise restart
            # Llama-server needs restart to load new config? No, llama-guardian reads config?
            # Llama-guardian reads config on startup.
            subprocess.run(["sudo", "systemctl", "restart", "llama-guardian.service"], check=True)
            logger.info("Restarted llama-guardian.service")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to restart service: {e}")
            
    else:
        logger.info("Models are up to date. No changes needed.")

if __name__ == "__main__":
    sync_models()
