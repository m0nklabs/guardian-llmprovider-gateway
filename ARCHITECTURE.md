# Ollama Guardian - Architecture & Documentation

## Project Overview
**Ollama Guardian** is a middleware solution designed to sit between client applications and the Ollama LLM server. It acts as a traffic controller, security guard, and performance optimizer.

## Core Objectives

1.  **Queue Proxy & Traffic Control**:
    *   **Flood Protection**: Prevents the Ollama server from being overwhelmed by too many simultaneous requests.
    *   **Queue Management**: Queues incoming requests when the system is at capacity.
    *   **Active Unload**: Proactively unloads idle models from VRAM to make space for new high-priority requests, preventing Out-Of-Memory (OOM) crashes.

2.  **Identity & Security (Guard)**:
    *   **Basic Authentication**: Enforces authentication to identify *which* application is making a request (e.g., "Caramba-Backend", "Agent-Forge").
    *   **Resource Tracking**: Logs usage per client application to understand resource consumption patterns.

3.  **Automated Tweaking & Benchmarking**:
    *   **Scheduled Optimization**: Runs a comprehensive benchmark suite during configured "off-hours" (e.g., 04:00 - 11:00).
    *   **Parameter Tuning**: Tests various combinations of `num_ctx` (context size) and `num_batch` to find the "sweet spot" for each model on the specific hardware.
    *   **Resumable Testing**: Can be interrupted and resumed without losing progress.

4.  **User Interface (Dashboard)**:
    *   **Configuration**: Web interface to adjust settings (VRAM limits, schedule times, allowed models).
    *   **Statistics**: Visual graphs showing VRAM usage, Tokens Per Second (TPS), and request latency.
    *   **Optimization Reports**: Displays the optimal settings found by the benchmark suite.

## Technical Architecture

### Architecture Diagram
*   **Port 11434 (Public)**: **Ollama Guardian**. The entry point for all clients. Handles Basic Auth, VRAM scheduling, Combo Caching, and Request Optimization.
*   **Port 11436 (Internal)**: **Real Ollama Server**. The execution engine. Only accessible by the Guardian.
*   **Port 11437 (UI)**: **Guardian Dashboard**.

### Authentication & Load Balancing
*   **Identity**: Clients identify themselves using HTTP Basic Auth.
*   **Password Policy**: The system uses Basic Auth for *identification* (load balancing/fairness), not strict security. The password check is permissive to allow easy integration.
*   **Load Balancing**: The Guardian uses the client identity to manage fair usage and VRAM allocation.
*   **Smart Combo Caching**: The system automatically detects and keeps frequently used sets of models ("combos") loaded in VRAM to minimize loading times.
*   **Feedback Loop**: The Guardian injects optimized parameters (`num_ctx`, `num_batch`) into requests based on nightly benchmark results.

### Directory Structure
```text
/home/flip/llama_cpp_guardian/
├── app/
│   ├── main.py            # Application Entry Point (FastAPI + Scheduler)
│   ├── proxy/             # Proxy Server Logic
│   │   ├── server.py      # Request handling & Auth Middleware
│   │   └── vram.py        # VRAM management logic
│   ├── tweaker/           # Benchmarking Engine
│   │   └── benchmark.py   # Test suite implementation
│   ├── scheduler/         # Task Scheduler
│   │   └── manager.py     # Idle window & service management
│   └── ui/                # Frontend Dashboard
│       ├── index.html     # Main Dashboard UI
│       └── static/        # JS/CSS assets
├── config/
│   └── settings.yaml      # Global Configuration
├── data/
│   ├── model_stats.json   # Learned VRAM usage data
│   ├── benchmark.json     # Benchmark results
│   └── usage_logs.db      # SQLite DB for usage tracking
└── requirements.txt       # Python Dependencies
```

### Authentication Flow
1.  Client sends request with `Authorization: Basic <base64_credentials>`.
2.  Proxy decodes credentials and verifies against `config/clients.yaml`.
3.  If valid, request is tagged with `client_id`.
4.  If invalid, returns `401 Unauthorized`.

### Benchmarking Strategy
*   **Time Window**: Configurable (Default: 04:00 - 11:00).
*   **Isolation**: Scheduler stops conflicting services (e.g., `caramba-backend`) before starting tests.
*   **Metrics**:
    *   **TPS**: Speed of generation.
    *   **VRAM**: Peak memory usage.
    *   **Latency**: Time to first token.
*   **Output**: Updates `data/benchmark.json` which feeds the UI graphs.

## Future Roadmap
*   **Auto-Apply**: Automatically apply the best found parameters (`num_batch`, `num_ctx`) to incoming requests based on the benchmark results.
*   **Alerting**: Notify admin via webhook if VRAM usage is consistently hitting limits.
