# Llama-CPP Guardian

> Hardware-aware queue manager and VRAM control plane for shared local LLM hosts.

Llama-CPP Guardian sits in front of `llama-server` and turns a raw inference
process into an operator-grade service. It serializes inference, owns backend
reloads, cooperates with ComfyUI to free VRAM, protects model switching with
auth and allowlists, and keeps a mixed 12 GB + 16 GB GPU host stable while
large-context text and vision runtimes share the same machine with other GPU
tenants.

## The Problem Guardian Solves

Local multi-tenant inference turns into VRAM Tetris fast:

- a 256k context Qwen or Gemma runtime can reserve most of a shared dual-GPU
  budget by itself
- ComfyUI may keep several GB resident between workflows
- Frigate or other always-on GPU services never fully leave the box
- raw `llama-server` gives you no queue, no switch lock, no ComfyUI handshake,
  and no crash-aware reload recovery

Guardian adds those missing control surfaces so the host fails gracefully
instead of hard-crashing into CUDA OOM loops or restart storms.

## What Guardian Actually Does

- Single-slot FIFO inference queue with queue timeout, queue polling, and
  `X-Request-Id` / `X-Queue-Wait-Ms` headers
- Model lifecycle ownership through `sudo systemctl start|stop llama-server`
- Hot-reloaded model registry from [config/models.yaml](config/models.yaml),
  including aliases, text and vision runtime fields, and switch policy
- Cooperative VRAM fencing via `POST {comfyui_url}/free` before every load or
  switch
- Auth-gated control plane on `:11434`, with model pinning and a switch
  allowlist
- Dashboard and monitoring surfaces on `:11437`, plus `/metrics` and
  `/api/status`
- Host-specific finetune v2 workflow that tunes `context`, `ngl`, and
  `tensor_split` without mutating `models.yaml` until `--apply`

## Runtime Topology

```text
Clients
  |
  v
Guardian proxy :11434
  - auth
  - queue
  - model switching
  - auto-reload / idle-unload
  - Ollama and OpenAI-compatible endpoints
  |
  v
llama-server :11440
  - official llama.cpp binary
  - started from scripts/start_llama.sh
  - args generated into config/current_model.args

Guardian UI :11437
  - dashboard
  - /api/stats
  - /api/benchmark
```

## Current Host Assumptions

Guardian is currently configured for a shared dual-GPU host with:

- `proxy.vram_limit_mb: 27000` in [config/settings.yaml](config/settings.yaml)
- mixed `tensor_split` profiles in [config/models.yaml](config/models.yaml)
- optional ComfyUI integration at `http://127.0.0.1:8188/free`
- backend path resolution via [app/paths.py](app/paths.py) and
  [scripts/start_llama.sh](scripts/start_llama.sh)

Current high-end runtime examples in [config/models.yaml](config/models.yaml):

- `Qwen3.6-35B-A3B-HauhauCS-Aggressive`: text `262144 / ngl 99 / split 0.36,0.64`,
  vision `262144 / ngl 40 / split 0.36,0.64`
- `Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved`: text
  `262144 / ngl 99 / split 0.39,0.61`, vision
  `262144 / ngl 39 / split 0.46,0.54`
- `gemma-4-31B-it-uncensored-heretic`: text
  `262144 / ngl 60 / split 0.42,0.58`

## Installation

### 1. Python environment

```bash
cd /home/flip/llama_cpp_guardian
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Backend prerequisites

Guardian does not spawn the backend directly with `subprocess.Popen`. It
expects:

- the official `llama-server` binary at
  `${LLAMA_CPP_OFFICIAL_ROOT}/build/bin/llama-server`
- GGUFs in `${MODELS_DIR}` (default: sibling `../models`)
- a `llama-server` systemd unit that starts
  [scripts/start_llama.sh](scripts/start_llama.sh)
- Guardian to have permission to run `sudo systemctl start llama-server` and
  `sudo systemctl stop llama-server`

Important: run the combined Guardian service with `python -m app.main`.
Starting only `uvicorn app.proxy.server:app` gives you the proxy API but not
the dashboard on `:11437`.

### 3. Configure the repo

Edit these files before the first load:

- [config/models.yaml](config/models.yaml): model paths, aliases, runtime
  fields, pinning, switch allowlist, idle unload
- [config/settings.yaml](config/settings.yaml): queue timeout, VRAM budget,
  timeout tiers, ComfyUI URL, maintenance window
- [config/api_keys.json](config/api_keys.json): API keys used by clients

Create the first key with the bundled helper:

```bash
python scripts/generate_key.py local-dev --prefix flip
```

### 4. Start Guardian

```bash
cd /home/flip/llama_cpp_guardian
source venv/bin/activate
python -m app.main
```

This starts:

- the authenticated Guardian proxy on `http://127.0.0.1:11434`
- the dashboard on `http://127.0.0.1:11437`
- background startup verification and idle-unload watching

## Quickstart

### Load a model

```bash
export GUARDIAN_KEY="flip_your_key_here"

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-35b-uncensored"}' \
  http://127.0.0.1:11434/admin/load
```

### Send a chat completion

```bash
curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-35b-uncensored",
    "messages": [{"role": "user", "content": "Reply with exactly: GUARDIAN OK"}],
    "max_tokens": 16
  }' \
  http://127.0.0.1:11434/v1/chat/completions
```

### Inspect health and queue state

```bash
curl -sS http://127.0.0.1:11434/healthz

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  http://127.0.0.1:11434/api/status

curl -sS \
  -H "Authorization: Bearer $GUARDIAN_KEY" \
  http://127.0.0.1:11434/v1/queue/status
```

### Open the dashboard

Browse to `http://127.0.0.1:11437/`.

## Key Files

| File | Purpose |
| --- | --- |
| [config/models.yaml](config/models.yaml) | Model registry, aliases, text and vision runtime fields, Guardian policy |
| [config/settings.yaml](config/settings.yaml) | Queue, timeouts, VRAM budget, ComfyUI URL, maintenance schedule |
| [config/api_keys.json](config/api_keys.json) | Bearer and x-api-key registry |
| [config/current_model.args](config/current_model.args) | Generated `llama-server` arguments for the active runtime |
| [scripts/start_llama.sh](scripts/start_llama.sh) | Backend launcher used by the `llama-server` service |
| [data/api_usage_state.json](data/api_usage_state.json) | Persistent usage snapshot for the dashboard |
| [data/model_finetune_v2_results.json](data/model_finetune_v2_results.json) | Append-only finetune v2 results log |

## Security Model

On port `11434`, every endpoint requires authentication except:

- `GET /healthz`
- `GET /metrics`

Guardian accepts:

- `Authorization: Bearer <token>`
- `x-api-key: <token>`
- `api-key: <token>`

Model switching can be restricted with:

- `guardian.pinned_model`
- `guardian.switch_allowlist`

Note: the dashboard port `11437` is a separate UI surface. In current code,
its `/` and `/api/*` endpoints are not auth-gated. Protect that port at the
network or reverse-proxy layer if it is not localhost-only.

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [HARDWARE_TUNING.md](HARDWARE_TUNING.md)
- [API_REFERENCE.md](API_REFERENCE.md)
- [docs/FINETUNE_V2_REQUIREMENTS.md](docs/FINETUNE_V2_REQUIREMENTS.md)
- [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md)# Llama CPP Guardian

Guardian is middleware for `llama-server` that turns a raw inference process into a managed service with request queuing, model lifecycle control, cooperative GPU memory management, API key auth, and benchmark-driven optimization.

It sits between clients and the llama.cpp backend, providing protocol bridging (OpenAI + Ollama APIs), automatic model switching, FIFO request queuing, idle unload/auto-reload, crash detection, and shared GPU coordination with 3rd-party processes.

## Architecture

```
Clients / Apps / Tools
        │
        ▼
┌──────────────────────────────────────┐
│  Guardian Middleware :11434           │
│  ├─ Bearer token auth                │
│  ├─ FIFO request queue (serialized)  │
│  ├─ OpenAI /v1 + Ollama /api bridges │
│  ├─ Model selection / switching      │
│  ├─ Request optimization             │
│  ├─ Persistent API usage monitoring  │
│  ├─ Idle unload & auto-reload        │
│  ├─ VRAM budget enforcement          │
│  └─ 3rd-party GPU process awareness  │
└──────────────────────────────────────┘
        │
        ▼
  llama-server :11440
        │
        ▼
  Official llama.cpp binary

  Dashboard UI :11437
```

## Key Features

### Request Queue

Guardian runs a single-slot backend. Only one inference request is processed at a time — concurrent requests wait in a FIFO queue.

- Transparent to simple clients (just blocks until a slot is free)
- `X-Request-Id` and `X-Queue-Wait-Ms` response headers on every inference response
- `GET /v1/queue/status` — separate polling endpoint for queue position (not queued itself, always responds immediately)
- Configurable timeout → HTTP 429 when exceeded
- Client disconnect detection (cancels queued request)

See [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md) for client implementation patterns and code examples.

### Official Backend Only

Guardian now always launches the official llama.cpp `llama-server` binary. Repo-sensitive paths are resolved from the checkout root or the `LLAMA_CPP_GUARDIAN_ROOT` / `LLAMA_CPP_OFFICIAL_ROOT` environment overrides; per-model fork selection is no longer part of the runtime contract.

### Protocol Bridging

Guardian exposes both API styles simultaneously:

- **OpenAI**: `/v1/chat/completions`, `/v1/models`, `/v1/*`
- **Ollama**: `/api/chat`, `/api/generate`, `/api/tags`, `/api/version`

Automatic model switching: if a request specifies a different model than what's loaded, Guardian switches transparently (subject to pinning and allowlist rules).

### Model Lifecycle Management

- **Model switching** — concurrency-safe via `asyncio.Lock()`, happens inside queue slot
- **Model pinning** — lock the system to a single model via `guardian.pinned_model`
- **Client allowlist** — restrict which API keys can trigger model switches
- **Idle unload** — stops llama-server after configurable idle time, auto-reloads on next request
- **Admin load protection** — `/admin/load` marks heavy model loads as active work so idle unload cannot interrupt them mid-load
- **Crash detection** — records up to 50 crash events with config snapshots
- **Backend verification** — post-switch check confirms the correct model is running
- **Config hot-reload** — re-reads `models.yaml` on every load/switch (no Guardian restart needed)

### 3rd-Party GPU Process Awareness

Guardian operates on shared GPU hardware alongside other processes. Instead of killing competing processes, it cooperates:

- **ComfyUI integration**: Before loading a model, calls `POST /free` to request graceful VRAM release. ComfyUI stays alive and auto-reloads its models on next workflow.
- **3rd-party budgeting**: The VRAM budget (`proxy.vram_limit_mb`) accounts for memory reserved by other GPU processes (Frigate NVR, etc.). These processes are never touched.
- **VRAM scheduling**: Enforces a hard VRAM limit to prevent OOM crashes on multi-GPU setups.

### Security

- **Bearer token auth** on all endpoints via `config/api_keys.json`
- Token format: `{prefix}_{32-char-hex}` (e.g., `flip_abc123...`, `hydro_def456...`)
- **Model pinning** prevents unauthorized model switches
- **Switch allowlist** restricts which clients can trigger model changes

### Dashboard Monitoring

- `http://<host>:11437/` serves the built-in operator dashboard
- `/api/stats` includes live VRAM, active model/cache data, benchmark summaries, and persisted API usage snapshots
- API usage is stored in `data/api_usage_state.json`, so request counts, token totals, top clients, and recent activity survive Guardian restarts
- Key visibility is non-secret: the dashboard shows prefixes and short SHA-256 fingerprints, never full API keys

### Tuning & Optimization

- Guardian-native model finetune CLI: fast binary search for max stable `context` plus coarse-to-fine two-GPU `tensor_split` tuning against `/admin/load`
- `RequestOptimizer` can still read historical benchmark artifacts from `data/benchmark_state.json` and `docs/benchmark_results.json`
- Legacy broad-sweep benchmarking has been moved out of the active runtime path under `app/tweaker/legacy/`
- Dashboard visualization of historical results

For focused per-model tuning, use the root finetune v2 CLI instead of broad sweep benchmarks. Run it without arguments first to list the available options, configured models, and aliases:

```bash
./finetune_v2.py
```

Then run a specific model or alias:

```bash
./finetune_v2.py qwen3.6-35b-heretic-mtp \
  --optimization context \
  --runtime-mode vision \
  --smoke-image-url data:image/png;base64,... \
  --apply
```

The finetune CLI now exposes one high-level goal selector instead of manual context and `ngl` bounds: `--optimization speed`, `--optimization context`, or `--optimization balanced`. `speed` prioritizes the highest stable GPU offload (`ngl`) first and then maximizes context for that offload level. In speed mode, a failed high-context probe now drops straight to the lower half of the search range before doing more split work, and once a narrow success/fail frontier is found Guardian switches to local 1% split refinement near that frontier instead of restarting broad far-away context probes for every alternate split. When that refined state is already running on fumes, Guardian also tightens the local context bisection itself: if both GPUs are below 500 MiB free or any single GPU is below 100 MiB free, it stops making broad post-frontier context jumps and falls back to smaller local context steps instead. If a rebalance move itself fails, Guardian immediately retries the 1% midpoint before giving up, so cases like `0.55 -> 0.53` now still probe `0.54`. `context` keeps walking balanced `ngl` candidates until it finds the highest stable runtime window, stopping early if a candidate already reaches the benchmark ceiling. `balanced` evaluates the full balanced search space and picks the strongest equilibrium between normalized context and `ngl`. In every mode, Guardian keeps `tensor_split` automatically balanced from live per-GPU free-VRAM measurements rather than blindly preferring 50/50. Omit `--split` to let the CLI calibrate and rebalance the split dynamically from live Guardian VRAM measurements. Omit `--ngl` to let the CLI explore the full `0..99` offload range on its own. `--runtime-mode auto` resolves to `text` unless `--smoke-image-url` is present; set `--runtime-mode vision` when you want the tuner to populate `vision_context` / `vision_ngl` / `vision_tensor_split` instead of the text runtime fields. Each individual load/smoke probe is flushed immediately to the configured `--results-file` path, with a `.active` sidecar for the current run, so live runs can be inspected test-by-test while they are still in progress. Reruns reuse compatible historical probes even if the short success-marker text in the smoke prompt changed. When identical cached combinations appear multiple times in history, Guardian now keeps the richest saved probe data instead of letting a later replay with empty VRAM telemetry overwrite an older live measurement. Add `--apply` only when you want the winning runtime fields written back to `config/models.yaml`.

## Running Guardian

Guardian runs as a systemd service (`llama-guardian.service`). For development:

```bash
pip install -r requirements.txt
python3 app/main.py
```

## Configuration

### `config/models.yaml` — Model Registry

Defines per-model runtime behavior:

```yaml
models:
  Qwen3.6-35B-A3B-HauhauCS-Aggressive:
    path: /home/flip/models/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf
    total_layers: 40
    benchmark_context_limit: 262144
    context: 262144
    ngl: 99
    kv_type: q4_0
    mmproj: /home/flip/models/Qwen3.6-35B-A3B-mmproj-BF16.gguf
    vision_context: 262144
    vision_ngl: 40
    vision_tensor_split: "0.36,0.64"
    extra_args: "--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0"

  Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved:
    path: /home/flip/models/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q4_K_M.gguf
    benchmark_context_limit: 262144
    context: 262144
    ngl: 36
    kv_type: q4_0
    tensor_split: "0.55,0.45"
    mmproj: /home/flip/models/Qwen3.6-35B-A3B-mmproj-BF16.gguf
    vision_context: 262144
    vision_ngl: 36
    vision_tensor_split: "0.55,0.45"
    extra_args: "--spec-type draft-mtp --spec-draft-n-max 3 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0"

  Qwen3-VL-30B-A3B-Thinking:
    path: /home/flip/models/Qwen3-VL-30B-A3B-Thinking-Q4_K_M.gguf
    benchmark_context_limit: 524288
    context: 524288
    ngl: 99
    kv_type: f16
    tensor_split: "0.55,0.45"
    mmproj: /home/flip/models/mmproj-Qwen3-VL-30B-A3B-F16.gguf

  Huihui-gemma-4-26B-A4B-it-abliterated:
    path: /home/flip/models/gemma-4-26B-A4B.unsloth-imatrix-UD-Q4_K_XL.gguf
    benchmark_context_limit: 262144
    context: 262144
    ngl: 99
    kv_type: q4_0
    tensor_split: "0.55,0.45"
    mmproj: /home/flip/models/gemma4-26b-a4b-mmproj-BF16.gguf

  gemma-4-31B-it-uncensored-heretic:
    path: /home/flip/models/gemma-4-31B-it-uncensored-heretic-Q4_K_M.gguf
    benchmark_context_limit: 262144
    context: 262144
    ngl: 60
    kv_type: q4_0
    tensor_split: "0.42,0.58"
    mmproj: /home/flip/models/gemma-4-31B-it-mmproj-BF16.gguf
    extra_args: "--repeat-penalty 1.3 --repeat-last-n 128 --dry-multiplier 1.0 --dry-base 1.75 --dry-penalty-last-n 256 --temp 0.6 --top-k 40"

aliases:
  qwen3: "Qwen3-30B-A3B-Thinking-2507"
  qwen3-30b: "Qwen3-30B-A3B-Thinking-2507"
  qwen3-30b-thinking: "Qwen3-30B-A3B-Thinking-2507"
  qwen3-vl: "Qwen3-VL-30B-A3B-Thinking"
  qwen3-vl-30b: "Qwen3-VL-30B-A3B-Thinking"
  qwen3-vl-heretic: "Qwen3-VL-32B-Gemini-Heretic-Uncensored-Thinking"
  qwen3-vl-32b-heretic: "Qwen3-VL-32B-Gemini-Heretic-Uncensored-Thinking"
  qwen3-32b-uncensored: "Qwen3-VL-32B-Gemini-Heretic-Uncensored-Thinking"
  qwen3.6-35b: "Qwen3.6-35B-A3B-HauhauCS-Aggressive"
  qwen3.6-35b-uncensored: "Qwen3.6-35B-A3B-HauhauCS-Aggressive"
  qwen3.6-35b-hauhau: "Qwen3.6-35B-A3B-HauhauCS-Aggressive"
  qwen3.6-35b-aggressive: "Qwen3.6-35B-A3B-HauhauCS-Aggressive"
  qwen3.6-35b-heretic-mtp: "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved"
  qwen3.6-35b-mtp: "Qwen3.6-35B-A3B-Heretic-Native-MTP-Preserved"
  gemma4: "Huihui-gemma-4-26B-A4B-it-abliterated"
  gemma4-heretic: "gemma-4-31B-it-uncensored-heretic"
  gemma4-31b-uncensored: "gemma-4-31B-it-uncensored-heretic"

guardian:
  pinned_model: "Huihui-gemma-4-26B-A4B-it-abliterated"
  switch_allowlist: ["m0nk111", "oelala"]
  idle_unload_minutes: 5
```

Supported per-model fields: `path`, `context`, `benchmark_context_limit`, `ngl`, `kv_type`, `tensor_split`, `mmproj`, `extra_args`, plus optional runtime overrides such as `vision_context`, `vision_ngl`, `vision_tensor_split`, and `text_*` variants.

Guardian hot-reloads the model registry on `/admin/load` and `/v1/models`, so new `models.yaml` entries and aliases take effect without restarting the API service.

Keep one runtime entry per GGUF family. Use API request parameters for reasoning, sampling, and other per-call behavior instead of duplicating `-agent`, `-deep`, or `-max` profile variants in `models.yaml`.

Treat `context` as the live hardware-safe runtime cap, not the model's theoretical or trained window. Keep `benchmark_context_limit` for the model's paper or metadata ceiling. Hugging Face's Qwen3.6-35B-A3B card states `262144` native context with optional YaRN extension up to `1010000`, so Guardian keeps `benchmark_context_limit: 262144` unless you intentionally introduce long-context rope scaling. For the current RTX 3060 + RTX 5060 Ti host, the text-only Qwen3.6 uncensored q4 runtime was re-validated at `262144` without forcing a tensor split, the text-only Native-MTP Heretic q4 profile now loads at the full `262144` with `ngl: 99` and the balanced split `tensor_split: "0.61,0.39"`, and the multimodal Native-MTP vision runtime remains separately tuned at `262144` with `vision_ngl: 36` and `vision_tensor_split: "0.55,0.45"` so mmproj only comes into play on image requests.

- `context`: the active runtime context Guardian actually loads for the model.
- `benchmark_context_limit`: the benchmark or paper ceiling where testing higher stops being useful; Guardian does not use it as the active runtime window.

### `config/settings.yaml` — System Configuration

```yaml
proxy:
  port: 11434
  target: http://localhost:11440
  vram_limit_mb: 27000

queue:
  max_concurrent: 1
  queue_timeout_seconds: 300

timeouts:
  tiers:
    tier_70b: { min_size_mb: 40000, timeout_seconds: 1800 }
    tier_32b: { min_size_mb: 20000, timeout_seconds: 1200 }
    tier_13b: { min_size_mb: 10000, timeout_seconds: 600 }
    tier_8b:  { min_size_mb: 5000,  timeout_seconds: 360 }
    tier_small: { min_size_mb: 0,   timeout_seconds: 600 }

benchmark:
  schedule:
    start_hour: 4
    end_hour: 11
    days: ["mon", "tue", "wed", "thu", "fri"]

services_to_stop: ["caramba-backend", "agent-forge"]
```

### `config/api_keys.json` — API Key Registry

Stores Bearer tokens with client names, creation timestamps, and optional metadata. Generate new keys with `python3 scripts/generate_key.py --name "my-app"`.

## API Reference

For detailed client integration examples (Python, TypeScript), queue-aware patterns, and error handling, see **[docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md)**.

### Inference (queued)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/chat/completions` | POST | OpenAI chat (streaming + non-streaming) |
| `/v1/completions` | POST | OpenAI text completion |
| `/v1/embeddings` | POST | OpenAI embeddings |
| `/api/chat` | POST | Ollama-style chat |
| `/api/generate` | POST | Ollama-style prompt generation |

All inference responses include `X-Request-Id` and `X-Queue-Wait-Ms` headers.

### Queue & Status

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/queue/status` | GET | Queue position, wait time, active requests |
| `/api/status` | GET | Current model, health, VRAM, crash info |
| `/api/crashes` | GET | Crash history (up to 50 records) |

### Model Management

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/models` | GET | OpenAI model list |
| `/api/tags` | GET | Ollama model list |
| `/api/version` | GET | Ollama version compat |
| `/admin/load` | POST | Force-load a specific model |
| `/admin/unload` | POST | Stop llama-server (free VRAM) |

### Session & Benchmark

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/session/save` | POST | Save conversation context |
| `/api/session/load` | POST | Load conversation context |
| `/api/session/list` | GET | List saved sessions |
| `/api/stats` | GET | Dashboard metrics (VRAM, models, records) |
| `/api/benchmark` | GET | Benchmark results & summary |
| `/api/benchmark/start` | POST | Trigger benchmark suite |
| `/api/benchmark/stop` | POST | Stop running benchmark |

All endpoints require `Authorization: Bearer <token>`.

## Directory Structure

```text
app/
├── engine/
│   └── manager.py       # ModelManager: lifecycle, switching, VRAM, crash detection
├── proxy/
│   ├── server.py        # FastAPI app: all endpoints, idle watcher
│   ├── queue.py         # InferenceQueue: FIFO semaphore, status reporting
│   ├── auth.py          # Bearer token auth (api_keys.json)
│   └── optimizer.py     # RequestOptimizer: benchmark-driven tuning
├── scheduler/
│   └── manager.py       # SchedulerManager: maintenance windows, service control
├── tweaker/
│   ├── finetune_v2_runner.py    # Guardian-backed runtime tuning
│   ├── finetune_v2_telemetry.py # v2 GPU identity/VRAM telemetry
│   └── legacy/                 # Deprecated v1 finetune + benchmark sweep code
├── ui/
│   └── index.html       # Dashboard (Tailwind dark mode, Chart.js)
└── main.py              # GuardianService: startup orchestration

config/
├── models.yaml          # Model registry + guardian security config
├── settings.yaml        # System config (ports, VRAM, timeouts, queue, scheduler)
├── api_keys.json        # API key store
├── current_model.args   # Runtime: active llama-server CLI args
└── current_model.env    # Runtime: per-model env vars (optional)

finetune_v2.py          # Operator entrypoint for Guardian finetune v2

scripts/
├── start_llama.sh       # Backend startup wrapper (reads current_model.args)
├── generate_key.py      # CLI key generation
├── test_system.py       # End-to-end system test
├── benchmark_context.py # Context size benchmarking
├── finetune_v2_model_config.py # Compatibility wrapper for finetune_v2.py
├── finetune_model_config.py # Legacy v1 tuning wrapper
├── stress_test.py       # Load testing
└── ...                  # Analysis, vision tests, model sync

data/
└── benchmark_state.json # Persisted benchmark queue + results
└── model_finetune_results.json # Finetune run history

docs/
├── CLIENT_INTEGRATION.md     # Client API guide with code examples
├── BENCHMARK_SUMMARY.md      # Global rankings and model comparisons
├── CONTEXT_BENCHMARKS.md     # Optimal context sizes per model
├── REAL_BENCHMARK_RESULTS.md # Empirical test results
└── LLM_TERMINOLOGY.md       # Model collection overview + glossary
```

## GPU Environment

Guardian runs on a dual-GPU host and coordinates VRAM with 3rd-party processes:

| GPU | VRAM | Role |
|-----|------|------|
| RTX 3060 (cuda:0) | 12GB | Model weight storage (tensor split) |
| RTX 5060 Ti (cuda:1) | 16GB | Primary compute + model weights |

**3rd-party GPU processes** (accounted for in VRAM budget, never killed):
- **Frigate NVR**: ~440MB (ffmpeg hardware decoding) — always running
- **ComfyUI**: Releases VRAM on request via `/free` API — cooperative sharing

Models use configured tensor splits (e.g., `"0.57,0.43"`) to distribute weights across both GPUs. Guardian now only loads `mmproj` for requests that actually contain image input, so a vision-capable model can keep one text runtime and one `vision_*` runtime in the same `models.yaml` entry without paying projector VRAM overhead on normal text traffic.

## Related Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — Architecture and design decisions
- [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md) — Client API guide with queue-aware patterns
- [docs/BENCHMARK_SUMMARY.md](docs/BENCHMARK_SUMMARY.md) — Model performance rankings
- [docs/REAL_BENCHMARK_RESULTS.md](docs/REAL_BENCHMARK_RESULTS.md) — Empirical benchmark results
- [docs/CONTEXT_BENCHMARKS.md](docs/CONTEXT_BENCHMARKS.md) — Context size recommendations
- [docs/LLM_TERMINOLOGY.md](docs/LLM_TERMINOLOGY.md) — Model glossary and collection overview
