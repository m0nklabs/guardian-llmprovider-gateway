# Operator Runbook — Llama-CPP Guardian

> Detail skill for deployment, operations, and troubleshooting.
> Referenced from `AGENTS.md`.

## Service lifecycle

```bash
# Start / stop / restart the Guardian proxy (port :11434)
sudo systemctl start llama-guardian
sudo systemctl stop llama-guardian
sudo systemctl restart llama-guardian
sudo systemctl status llama-guardian

# Verify it's listening
curl -s http://127.0.0.1:11434/healthz

# View recent logs (ignore healthz noise)
journalctl -u llama-guardian.service --since "5 min ago" --no-pager | grep -v 'GET /healthz'
```

> **No hot reload.** Code changes (`app/*.py`) and provider config changes
> (`config/settings.yaml`) both require a restart to take effect.

## Backend (llama-server :11440)

```bash
# Health check
curl -s http://127.0.0.1:11440/health

# Guardian manages this backend via systemd 'llama-server' unit:
sudo systemctl status llama-server
# Logs:
journalctl -u llama-server.service --since "5 min ago" --no-pager | tail -20
```

## API keys

```bash
# Mint a new Guardian API key
./venv/bin/python scripts/generate_key.py

# List keys (structure, masked)
python3 -c "import json; [print(k) for k in json.load(open('config/api_keys.json'))]"

# Named keys exist for: goose, oelala, hydroponics, and others.
```

## Discovering served models

```bash
KEY="<your_guardian_api_key>"
curl -s -H "Authorization: Bearer $KEY" \
  http://192.168.1.G:11434/v1/models | python3 -m json.tool
```

The served list is built from `config/models.yaml` (local aliases) +
`config/settings.yaml` (`providers.*.models`, cloud). Prefix-based
cloud models (via `model_prefixes`) are routable but don't appear in
the discovery list — see `docs/LLM_ROUTER.md`.

## Common errors and fixes

### `404 model_not_served`

The client sent a model name not in the served set. Check:
1. Is it a local alias? → `config/models.yaml`
2. Is it a cloud model? → matches a `model_prefixes` namespace or
   explicit `models:` entry in `config/settings.yaml`
3. Is it a `guardian/{provider}/{model}` per-key route? → requires a
   linked cloud credential (`cloud_keys.py`)

### `403 cloud_credential_not_linked`

A `guardian/{provider}/{model}` route requires a per-key linked cloud
credential. Use prefix-based routing (e.g. `anthropic/claude-...`)
instead if the global provider key should be used.

### `Upstream idle timeout exceeded` (client-side)

The cloud streaming pass-through path wasn't sending keepalives.
All streaming paths now pass `heartbeat_interval_s` to the watchdog
(15s SSE `: guardian-keepalive` comments). If this recurs, check
`STREAM_HEARTBEAT_INTERVAL_S` is set on every `_iter_sse_lines_with_watchdog`
call site (local model + cloud + ollama endpoints).

### `503 provider_unavailable`

A cloud provider's API key env var is unset or empty. Check `.env`:
```bash
grep -E 'OPENROUTER_API_KEY|NVIDIA_API_KEY|POOLSIDE_API_KEY' .env
```

## Config files

| File | Purpose | Edit + restart? |
|---|---|---|
| `config/settings.yaml` | Proxy port, providers, queue, timeout tiers | restart |
| `config/models.yaml` | Model registry (aliases, runtime, tensor_split) | restart |
| `config/api_keys.json` | Named API keys | restart |
| `.env` | Secrets (env var expansion) | restart |
| `scripts/start_llama.sh` | llama-server launch args | restart llama-server |

## When making code changes

1. Edit `app/*.py`
2. Run the full pre-restart gate (py_compile + pyflakes + signature check + pytest):
   ```bash
   ./venv/bin/python scripts/pre_restart_check.py
   ```
   All four gates must PASS before restarting — a startup-breaking error is
   not self-healable because the agent's own model traffic routes through
   Guardian (see AGENTS.md Critical rules).
3. `sudo systemctl restart llama-guardian` — deploy
4. `curl -s http://127.0.0.1:11434/healthz` — verify it's back up
5. Watch logs: `journalctl -u llama-guardian.service -f | grep -v healthz`
