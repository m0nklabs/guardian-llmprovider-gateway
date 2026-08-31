# Installing Guardian LLM Provider Gateway

Guardian is a self-hosted LLM gateway: one FastAPI service that fronts a
local llama.cpp backend and multiple cloud providers behind a single
OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`, …), with an
Anthropic bridge (`/v1/messages`), a dashboard, and a privacy-aware capture
subsystem.

## Quickstart

```bash
git clone <this repo> guardian && cd guardian
scripts/install.sh --with-systemd
# → fill in .env (provider API keys), then:
sudo systemctl enable --now guardian-llmprovider-gateway.service
curl -k https://127.0.0.1:11434/v1/models -H "Authorization: Bearer <operator-key>"
```

`scripts/install.sh` is idempotent — re-running it upgrades the venv and
re-renders deploy files without touching your `.env`, keys or certificates.

## What the installer does

| Step | Detail |
| --- | --- |
| Preflight | checks Python (3.14 pins, ≥ 3.12 floor), detects the LAN IP |
| venv | builds `venv/` from the exact pins in `requirements.txt`; replaces a symlinked venv with a real one |
| Config bootstrap | creates `.env` from `.env.example` (never overwrites), mints the first operator API key via `scripts/generate_key.py` (shown once) |
| TLS | generates a self-signed pair into `~/.config/guardian-llmprovider-gateway/tls/` (SANs: localhost, hostname, 127.0.0.1, LAN IP) or reuses `--tls-cert/--tls-key` |
| Deploy rendering | substitutes `@INSTALL_DIR@`, `@RUN_USER@`, `@LAN_IP@`, `@LAN_SUBNET@`, `@TLS_CERTFILE@/…` into the systemd/nginx templates → `deploy-rendered/` |
| Systemd (opt-in) | installs the rendered unit + TLS drop-in with sudo (else prints manual steps) |
| Validation | `import app.main`, YAML config parse, port-11434 availability |

Useful flags: `--dir`, `--python`, `--user`, `--tls-dir`, `--skip-venv`,
`--print-only` (render only, never touch system dirs).

## Ports (product defaults)

| Port | Purpose |
| --- | --- |
| 11434 | public API — nginx protocol mux: plain HTTP and TLS on one port |
| 11435 | Guardian TLS loopback (nginx passes TLS through unchanged) |
| 11436 | nginx plain-HTTP loopback → TLS upstream |
| 11437 | dashboard (bound to 127.0.0.1; the LAN exposure is a separate nginx conf with an allowlist) |
| 11440 | llama-server backend |
| 11441 | caretaker daemon (optional, separate repo) |

## TLS trust

The installer generates a self-signed certificate. Clients on the LAN must
trust it before connecting without a custom CA setting, e.g. on Debian/Ubuntu:

```bash
sudo cp ~/.config/guardian-llmprovider-gateway/tls/guardian-<lan-ip>.crt \
        /usr/local/share/ca-certificates/guardian.crt
sudo update-ca-certificates
```

To reuse an existing pair (e.g. from a previous install): pass
`--tls-cert FILE --tls-key FILE`.

## Configuration

- `config/global.settings.yaml` — cross-cutting settings (proxy, queue,
  timeouts, capture, failover health). Hot-reloads via
  `POST /api/config/reload` — code changes still need a restart.
- `config/providers/<name>.settings.yaml` — one file per provider (F2):
  base_url, API key via `${ENV_VAR}`, `catalog_url`, model overrides,
  local registry/aliases in `ai-kvm2-local.settings.yaml`.
- `config/guardian.keys.yaml` — named API keys (gitignored secrets).
- `.env` — all secrets; `${VAR}` expansion everywhere. Start from
  `.env.example`.

Model resolution is name-based and key-independent: cloud models are
`{provider}/{brand}/{model}`, local models are aliases from the local
provider file. See `docs/CONFIG_SCHEMA.md` and `docs/LLM_ROUTER.md`.

## Caretaker daemon (optional)

Remote-first backend lifecycle (ensure/unload) lives in the separate
`caretaker-llamacpp` repository (`deploy/systemd/` there). Set `CARETAKER_KEY`
in `.env` to the daemon's auth token; without it the gateway falls back to
local backend management. **Startup behaviour:** with the daemon reachable,
the gateway adopts the daemon's loaded state; without it, a startup adopts a
live known backend instead of force-switching it (cut-over safety,
`engine/manager.py`).

## Manual install (no installer)

```bash
python3.14 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in
venv/bin/python -m app.main   # binds 0.0.0.0:11434 (TLS off) or GUARDIAN_TLS_*
```

TLS environment variables: `GUARDIAN_TLS_CERTFILE` + `GUARDIAN_TLS_KEYFILE`
(all-or-nothing pair), `GUARDIAN_TLS_HOST` (default `0.0.0.0`, production
uses `127.0.0.1`), `GUARDIAN_TLS_PORT` (default 11434, production 11435),
`GUARDIAN_UI_PORT` (dashboard, default 11437).

## Verify

```bash
venv/bin/python -m pytest tests/ -q          # full suite
scripts/pre_restart_check.py                  # gate before any restart
curl -k https://127.0.0.1:11434/v1/models -H "Authorization: Bearer <key>"
curl -k -X POST https://127.0.0.1:11434/api/cloud/catalog/refresh \
     -H "Authorization: Bearer <key>"        # populate the cloud catalog once
```
