#!/usr/bin/env python3
"""
Simple script to generate API keys for Llama Guardian.
Does not require full app context.
"""

import argparse
import sys
import json
import secrets
import time
from pathlib import Path

# Path to the shared api_keys.json
CONFIG_DIR = Path(__file__).parent.parent / "config"
API_KEYS_FILE = CONFIG_DIR / "api_keys.json"
DEFAULT_API_KEY_PREFIX = "flip"

def load_keys():
    if not API_KEYS_FILE.exists():
        return {}
    with open(API_KEYS_FILE, 'r') as f:
        return json.load(f)

def save_keys(keys):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, 'w') as f:
        json.dump(keys, f, indent=2)


def normalize_prefix(prefix):
    normalized = (prefix or DEFAULT_API_KEY_PREFIX).strip().strip("_")
    if not normalized:
        normalized = DEFAULT_API_KEY_PREFIX
    return f"{normalized}_"


def generate_key(name, metadata=None, prefix=None):
    if not name:
        print("Error: Name required")
        sys.exit(1)

    prefix = normalize_prefix(prefix)
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
    print(f"Prefix: {prefix}")
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
    parser = argparse.ArgumentParser(description="Generate or list Guardian API keys.")
    parser.add_argument("name", nargs="?", help="Key name to persist")
    parser.add_argument("metadata_json", nargs="?", help="Optional metadata JSON")
    parser.add_argument("--prefix", default=DEFAULT_API_KEY_PREFIX, help="Key prefix without trailing underscore")
    parser.add_argument("--list", action="store_true", help="List existing keys")
    args = parser.parse_args()

    if args.list:
        list_keys()
    else:
        if not args.name:
            parser.error("name is required unless --list is used")

        name = args.name
        meta = {}
        if args.metadata_json:
            try:
                meta = json.loads(args.metadata_json)
            except json.JSONDecodeError:
                print("Warning: Metadata is not valid JSON, ignoring")

        generate_key(name, meta, prefix=args.prefix)
