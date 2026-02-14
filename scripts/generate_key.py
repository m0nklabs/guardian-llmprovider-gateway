#!/usr/bin/env python3
"""
Simple script to generate API keys for Llama Guardian.
Does not require full app context.
"""

import sys
import json
import secrets
import time
from pathlib import Path

# Path to the shared api_keys.json
CONFIG_DIR = Path(__file__).parent.parent / "config"
API_KEYS_FILE = CONFIG_DIR / "api_keys.json"

def load_keys():
    if not API_KEYS_FILE.exists():
        return {}
    with open(API_KEYS_FILE, 'r') as f:
        return json.load(f)

def save_keys(keys):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, 'w') as f:
        json.dump(keys, f, indent=2)

def generate_key(name, metadata=None):
    if not name:
        print("Error: Name required")
        sys.exit(1)
        
    prefix = "flip_"
    token = secrets.token_hex(16)
    api_key = f"{prefix}{token}"
    
    keys = load_keys()
    
    # Check duplicate names (optional but nice)
    for k, v in keys.items():
        if v.get('name') == name:
            print(f"Warning: Name '{name}' already exists for key {k[:10]}...")
    
    keys[api_key] = {
        "name": name,
        "created_at": time.time(),
        "metadata": metadata or {}
    }
    
    save_keys(keys)
    print(f"\n✅ Generated successfully!")
    print(f"Name: {name}")
    print(f"Key:  {api_key}")
    print(f"File: {API_KEYS_FILE}")
    return api_key

def list_keys():
    keys = load_keys()
    if not keys:
        print("No keys found.")
        return
        
    print(f"{'NAME':<20} {'KEY (PREFIX)':<40} {'CREATED'}")
    print("-" * 80)
    for k, v in keys.items():
        name = v.get('name', 'Unknown')
        created = time.ctime(v.get('created_at', 0))
        print(f"{name:<20} {k:<40} {created}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 generate_key.py <name> [metadata_json]")
        print("       python3 generate_key.py --list")
        sys.exit(1)
        
    cmd = sys.argv[1]
    
    if cmd == "--list":
        list_keys()
    else:
        name = cmd
        meta = {}
        if len(sys.argv) > 2:
            try:
                meta = json.loads(sys.argv[2])
            except:
                print("Warning: Metadata is not valid JSON, ignoring")
        
        generate_key(name, meta)
