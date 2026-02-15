#!/bin/bash
# Wrapper script to load dynamic model arguments

CONFIG_FILE="/home/flip/llama_cpp_guardian/config/current_model.args"

# Default fallback if config missing
DEFAULT_MODEL="/home/flip/models/GLM-4.7-Flash-Q4_K_M-latest.gguf"
ARGS="-m $DEFAULT_MODEL -c 32768 -ngl 99 -sm row -ctk q4_0 -ctv q4_0 --host 127.0.0.1 --port 11440 --slot-save-path /home/flip/llama_slots"

if [ -f "$CONFIG_FILE" ]; then
    # Read args from file (expecting single line)
    ARGS=$(cat "$CONFIG_FILE")
    echo "Starting Llama Server with dynamic args: $ARGS"
else
    echo "Config file not found, using default: $ARGS"
fi

# Need to run llama-server explicitly
/home/flip/ik_llama_cpp_build/build/bin/llama-server $ARGS
