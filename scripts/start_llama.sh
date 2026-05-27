#!/bin/bash
# Wrapper script to load dynamic model arguments for the official llama.cpp binary

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${LLAMA_CPP_GUARDIAN_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG_DIR="$ROOT_DIR/config"
OFFICIAL_ROOT="${LLAMA_CPP_OFFICIAL_ROOT:-$ROOT_DIR/../llama_cpp_official}"
MODELS_DIR="${MODELS_DIR:-$ROOT_DIR/../models}"
SLOTS_DIR="${LLAMA_CPP_GUARDIAN_SLOTS_DIR:-$HOME/llama_slots}"

CONFIG_FILE="$CONFIG_DIR/current_model.args"
ENV_FILE="$CONFIG_DIR/current_model.env"

# Source optional per-model environment (e.g. CUDA_VISIBLE_DEVICES)
if [ -f "$ENV_FILE" ]; then
    echo "Sourcing model environment: $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a
fi

# Keep llama CUDA ordinals stable across reboots; v2 telemetry maps nvidia-smi
# readings into this same PCI-bus order.
export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"
echo "CUDA_DEVICE_ORDER=$CUDA_DEVICE_ORDER"

# Default binary: official llama.cpp
DEFAULT_BINARY="$OFFICIAL_ROOT/build/bin/llama-server"
BINARY="${LLAMA_SERVER_BINARY:-$DEFAULT_BINARY}"

# Default fallback if config missing — MUST match manager.py default_model
# SECURITY: This fallback should match the pinned/default model in Guardian config
DEFAULT_MODEL="$MODELS_DIR/glm-4.7-flash-claude-4.5-opus.q4_k_m.gguf"
ARGS="-m $DEFAULT_MODEL -c 262144 -ngl 99 -ctk q4_0 -ctv q4_0 --host 127.0.0.1 --port 11440 --slot-save-path $SLOTS_DIR --no-mmap --tensor-split 0.57,0.43 -nkvo --parallel 4"

if [ -f "$CONFIG_FILE" ]; then
    # Read args from file (expecting single line)
    IFS= read -r ARGS < "$CONFIG_FILE"
    echo "Starting Llama Server with dynamic args: $ARGS"
else
    echo "Config file not found, using default: $ARGS"
fi

echo "Using official llama.cpp binary: $BINARY"

# Verify binary exists
if [ ! -x "$BINARY" ]; then
    echo "ERROR: Binary not found or not executable: $BINARY"
    echo "Falling back to default: $DEFAULT_BINARY"
    BINARY="$DEFAULT_BINARY"
fi

# Need to run llama-server explicitly
$BINARY $ARGS
