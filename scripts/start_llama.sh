#!/bin/bash
# Wrapper script to load dynamic model arguments and select correct backend binary

CONFIG_FILE="/home/flip/llama_cpp_guardian/config/current_model.args"
BINARY_FILE="/home/flip/llama_cpp_guardian/config/current_model.binary"
ENV_FILE="/home/flip/llama_cpp_guardian/config/current_model.env"

# Source optional per-model environment (e.g. CUDA_VISIBLE_DEVICES)
if [ -f "$ENV_FILE" ]; then
    echo "Sourcing model environment: $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a
fi

# Default binary: ik_llama.cpp fork (primary)
DEFAULT_BINARY="/home/flip/ik_llama_cpp_build/build/bin/llama-server"

# Default fallback if config missing — MUST match manager.py default_model
# SECURITY: This fallback should match the pinned/default model in Guardian config
DEFAULT_MODEL="/home/flip/models/glm-4.7-flash-claude-4.5-opus.q4_k_m.gguf"
ARGS="-m $DEFAULT_MODEL -c 262144 -ngl 99 -ctk q4_0 -ctv q4_0 --host 127.0.0.1 --port 11440 --slot-save-path /home/flip/llama_slots --no-mmap --tensor-split 0.57,0.43 -nkvo --parallel 4"

if [ -f "$CONFIG_FILE" ]; then
    # Read args from file (expecting single line)
    ARGS=$(cat "$CONFIG_FILE")
    echo "Starting Llama Server with dynamic args: $ARGS"
else
    echo "Config file not found, using default: $ARGS"
fi

# Select binary: read from binary file, or use default (ik_fork)
if [ -f "$BINARY_FILE" ]; then
    BINARY=$(cat "$BINARY_FILE")
    echo "Using backend binary: $BINARY"
else
    BINARY="$DEFAULT_BINARY"
    echo "No binary config, using default (ik_fork): $BINARY"
fi

# Verify binary exists
if [ ! -x "$BINARY" ]; then
    echo "ERROR: Binary not found or not executable: $BINARY"
    echo "Falling back to default: $DEFAULT_BINARY"
    BINARY="$DEFAULT_BINARY"
fi

# Need to run llama-server explicitly
$BINARY $ARGS
