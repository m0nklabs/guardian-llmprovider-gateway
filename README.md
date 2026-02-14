# Llama Server Tweaker Guardian

A comprehensive tool to manage, monitor, and optimize Llama Server LLM usage.

## Features

1.  **Guardian Proxy (Port 11435)**:
    *   Intercepts requests to Llama Server (Port 11440).
    *   **Active Unload**: Automatically unloads idle models to free VRAM for new requests, preventing OOM crashes on multi-GPU setups.
    *   **Stats Learning**: Learns actual VRAM usage of models over time and saves it to `data/model_stats.json`.
    *   **Safety Limits**: Enforces a configurable VRAM limit (default 27GB).

2.  **Benchmark Suite**:
    *   Automated testing of models with various configurations (Context Size, Batch Size).
    *   **Resumable**: Saves state to `data/benchmark_state.json`, allowing tests to be interrupted and resumed.
    *   **Metrics**: Records Tokens Per Second (TPS), VRAM usage, and Latency.

3.  **Scheduler**:
    *   Runs the Benchmark Suite automatically during idle hours (04:00 - 11:00 Weekdays).
    *   Can manage (stop/start) other services to ensure clean test environments.

## Installation

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

2.  Run the Guardian:
    ```bash
    python3 app/main.py
    ```

## Configuration

Edit `config/settings.yaml` to configure:
*   Idle window hours.
*   Services to stop/start.
*   Models to benchmark.
*   VRAM limits.

## Directory Structure

*   `app/`: Source code.
*   `config/`: Configuration files.
*   `data/`: Persistent data (stats, results).
*   `scripts/`: Helper scripts.
