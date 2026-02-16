import os
import json
from huggingface_hub import hf_hub_download

# Define paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = "/home/flip/models"
REGISTRY_PATH = os.path.join(BASE_DIR, "docs", "model_registry.json")

def download_registry_models():
    print(f"📖 Reading registry from {REGISTRY_PATH}...")
    
    if not os.path.exists(REGISTRY_PATH):
        print(f"❌ Registry file not found: {REGISTRY_PATH}")
        return

    try:
        with open(REGISTRY_PATH, 'r') as f:
            data = json.load(f)
            models = data.get("models", [])
    except Exception as e:
        print(f"❌ Error reading registry: {e}")
        return

    if not os.path.exists(MODELS_DIR):
        print(f"📂 Creating models directory: {MODELS_DIR}")
        os.makedirs(MODELS_DIR, exist_ok=True)

    print(f"📋 Found {len(models)} models to check/download.")

    for model in models:
        name = model.get("name")
        repo_id = model.get("repo_id")
        filename = model.get("filename")
        
        if not (name and repo_id and filename):
            print(f"⚠️ Skipping invalid entry: {model}")
            continue

        local_path = os.path.join(MODELS_DIR, filename)
        
        print(f"\n🔹 Processing: {name}")
        print(f"   Repo: {repo_id}")
        print(f"   File: {filename}")
        
        if os.path.exists(local_path):
            print(f"   ✅ File already exists at {local_path}")
            # Optional: Check size or hash validation here if needed
            continue

        print(f"   ⬇️ Downloading to {MODELS_DIR}...")
        try:
            # Download directly to the models directory
            downloaded_path = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=MODELS_DIR,
                local_dir_use_symlinks=False  # We want the actual file, not a symlink to cache
            )
            print(f"   ✅ Download successful: {downloaded_path}")
        except Exception as e:
            print(f"   ❌ Download failed: {e}")

if __name__ == "__main__":
    download_registry_models()
