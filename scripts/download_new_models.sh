#!/bin/bash
set -e

MODELS_DIR="/home/flip/models"
mkdir -p "$MODELS_DIR"

download_model() {
    url="$1"
    filename="$2"
    filepath="$MODELS_DIR/$filename"
    
    if [ -f "$filepath" ]; then
        echo "✅ $filename already exists, skipping."
    else
        echo "⬇️ Downloading $filename..."
        curl -L -o "$filepath" "$url"
        echo "✅ Download complete: $filename"
    fi
}

# GLM-5 40B (32B Active)
download_model "https://huggingface.co/unsloth/GLM-5-GGUF/resolve/main/GLM-5-Q4_K_M.gguf" "GLM-5-Q4_K_M.gguf"

# Qwen3 Coder Next - 32B Coding Specialist
download_model "https://huggingface.co/unsloth/Qwen3-Coder-Next-GGUF/resolve/main/Qwen3-Coder-Next-Q4_K_M.gguf" "Qwen3-Coder-Next-Q4_K_M.gguf"

# GLM 4.7 Flash Heretic - Uncensored Creative Writing (Q6_K)
download_model "https://huggingface.co/DavidAU/GLM-4.7-Flash-Uncensored-Heretic-NEO-CODE-Imatrix-MAX-GGUF/resolve/main/GLM-4.7-Flash-Uncensored-Heretic-NEO-CODE-Imatrix-MAX-Q6_K.gguf" "DavidAU-GLM-4.7-Flash-Heretic-Uncensored.gguf"

# DeepSeek R1 + Llama 3.1 16.5B Brainstorm - Storytelling (Q6_K)
download_model "https://huggingface.co/DavidAU/DeepSeek-R1-Distill-Llama-3.1-16.5B-Brainstorm-gguf/resolve/main/DeepSeek-R1-Distill-Llama-3.1-16.5B-Brainstorm-Q6_K.gguf" "DavidAU-DeepSeek-R1-Distill-Llama-3.1-16.5B-Brainstorm.gguf"

# MiniMax M2.5 (Q4_K_M)
download_model "https://huggingface.co/unsloth/MiniMax-M2.5-GGUF/resolve/main/MiniMax-M2.5-Q4_K_M.gguf" "MiniMax-M2.5-Q4_K_M.gguf"

echo "🎉 All downloads complete!"
