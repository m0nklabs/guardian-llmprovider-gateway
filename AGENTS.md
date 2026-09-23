# AGENTS.md — Guardian LLM Provider Gateway

> Canonical durable AI-agent context for this repo. Read targeted cold context only when the task requires it.
> Claude Code: `CLAUDE.md` → here. Goose: `.goosehints` → here. Copilot: `.github/copilot-instructions.md` references this.

## Stack

- **Language:** Python 3.14 (venv at `./venv`)
- **Web:** FastAPI + uvicorn + httpx
- **Backend:** llama.cpp (`llama-server` on `:11440`, launched via `scripts/start_llama.sh`)
- **Frontend:** React/Vite/Tailwind dashboard on `:11437` (`dashboard/`), bound to `127.0.0.1`
- **Config:** `config/global.settings.yaml` (proxy, providers, queue, timeouts), `config/providers/<naam>.settings.yaml` (één bestand per provider sinds F2; lokale registry/aliases/guardian in `ai-kvm2-local.settings.yaml`), `config/guardian.keys.yaml` (named API keys)
- **Secrets:** `.env` — `${VAR}` expansion in YAML; never commit secrets
- **Deploy:** systemd unit `guardian-llmprovider-gateway.service` (alias `llama-guardian.service`), productie-checkout `/home/flip/guardian-llmprovider-gateway` — de legacy-dir `/home/flip/llama_cpp_guardian` is frozen (archief). Nginx exposes the public API on `:11434`.
- **TLS:** nginx stream TLS preread multiplexes both `http://192.168.1.35:11434` and `https://192.168.1.35:11434`. It passes TLS unchanged to Guardian on `127.0.0.1:11435` and routes plain HTTP through nginx on `127.0.0.1:11436`. See `deploy/nginx/guardian-llmprovider-gateway-protocol-mux.conf` and `deploy/nginx/guardian-llmprovider-gateway-loopback-http.conf`.
- **TLS trust:** this host trusts the Guardian certificate through `/usr/local/share/ca-certificates/llama-guardian-192.168.1.35.crt`. Other LAN clients must trust that same certificate before connecting without a custom CA setting.
- **Tests:** pytest (`tests/`, `asyncio_mode=auto`)

## Critical rules

- **Verify before claiming a fix.** Run `./venv/bin/python -m py_compile <file>`
  and relevant pytest coverage; use `./venv/bin/python -m pytest tests/ -x`
  when the full suite is warranted. Do not claim success without evidence.
- **Code restarts; configuration hot-reloads.** `app/*.py` changes require
  `sudo systemctl restart llama-guardian`; there is no hot code reload.
  `global.settings.yaml` changes hot-reload through authenticated
  `POST /api/config/reload`. Ports, PIDs, and TLS remain restart-only.
- **Run the pre-restart gate before every restart.**
  `./venv/bin/python scripts/pre_restart_check.py` runs py_compile, pyflakes,
  signature validation, and the full pytest suite. Every gate must pass first.
- **Restarting cuts the agent's own model traffic.** Validate first, notify the
  operator that the session will drop, and have the operator restart outside the
  session. A failed start is not self-healable; recover from outside Guardian.
- **TLS and secrets are strict boundaries.** `GUARDIAN_TLS_CERTFILE` and
  `GUARDIAN_TLS_KEYFILE` are an all-or-nothing pair; keep private keys `0600`
  and preserve the nginx stream multiplexer. Keep keys in `.env` or
  `config/guardian.keys.yaml`, never inline or commit them, and use
  `scripts/generate_key.py` to mint Guardian keys.
- **Keep the split config and routing contract.** Global settings, per-provider
  settings, and Guardian keys remain separate. Cloud models match `models:` or
  `model_prefixes:`; local aliases live in `ai-kvm2-local.settings.yaml`; unknown
  models return `404 model_not_served`. The local `managed: true` provider is
  addressable but never cloud-routed.
- **Preserve cloud authorization and discovery.** Cloud addresses are
  `{provider}/{brand}/{model}` with no `guardian/` prefix or credential-link
  store. `cloud_gateway_access: false` must hide cloud models and return `403`.
  `catalog_url` limits a provider to reachable models; changing its endpoint
  invalidates the corresponding cold-start catalog cache.
- **Treat capability metadata as routing data.** Use local image fallback only
  for configured text-only cloud models with `image_fallback`. Resolve context
  metadata in order: `context_overrides`, cloud overrides, catalog or `/props`,
  then the `131072` fallback.
- **Preserve streaming and grammar contracts.** Every SSE path passes
  `heartbeat_interval_s=STREAM_HEARTBEAT_INTERVAL_S` to
  `_iter_sse_lines_with_watchdog`. Local OpenAI passes `response_format`,
  `json_schema`, and GBNF unchanged; cloud strips GBNF and `json_schema`, keeps
  native `response_format`, and capture records only presence flags.
- **Do not hardcode deployment values.** Keep paths, ports, URLs, filenames, and
  timeouts in configuration or `app/paths.py`; inject them when extracting code.
- **Diagnose dashboard empty shells as auth first.** Dashboard `/api/*` requests
  require a Bearer key; verify `guardian_dashboard_api_key` in localStorage
  before treating an empty dashboard as a code failure.

## Task-directed documentation

- **Cloud routing, model resolution, or cloud access:** read `docs/LLM_ROUTER.md`.
- **Configuration schema or provider files:** read `docs/CONFIG_SCHEMA.md` and
  `docs/CONFIG_PROVIDER_FILES.md`.
- **Anthropic bridge, API, clients, or architecture:** read the applicable
  `docs/ANTHROPIC_BRIDGE.md`, `docs/API_REFERENCE.md`,
  `docs/CLIENT_INTEGRATION.md`, or `docs/ARCHITECTURE.md`.
- **Deployment, TLS, or host operations:** read `docs/skills/operator-runbook.md`.
- **Hardware tuning or the Guardian 2.0 roadmap:** read `docs/HARDWARE_TUNING.md`
  or `docs/IMPLEMENTATION_PLAN.md`; consult `docs/HANDOFF.md` for current state.

## Repository guide

- `app/proxy/` owns authentication, provider discovery, failover, queues,
  streaming, process state, metrics, and service lifespan.
- `app/gateway/` owns extracted routing, normalization, streaming, model
  discovery, admin API, sessions, context metadata, and caretaker wiring.
- `app/cloud_inference/`, `app/local_inference/`, and `app/capture/` own cloud
  forwarding, local model execution, and privacy-aware capture respectively.
- `config/` contains global, per-provider, and Guardian-key configuration.
- `scripts/pre_restart_check.py` is the restart gate;
  `scripts/update_guardian_config.py` mutates live configuration; and
  `scripts/guardianctl.py` manages capture operations.

## State, maintenance, and runners

- Read `docs/HANDOFF.md` only to continue active work, report current status, or
  address a named open item. Append same-session behavior changes and repository
  findings to the handoff or `docs/AGENT_JOURNAL.md`; completed handoffs go to
  `docs/ARCHIVED_HANDOFFS.md`.
- Historical detail, including prior capture phases and resolved decisions, lives
  in `docs/AGENT_CONTEXT_ARCHIVE.md`. The exact pre-condensation source is
  `docs/archive/agent-instructions-2026-09-22.md`; neither archive is startup
  context.
- `CLAUDE.md` and `.goosehints` are relative symlinks to this file. Keep hot
  instructions durable; place dated status and tool details in cold documents.
- Reuse the organization-level self-hosted runner pool; never add a project
  runner. GPU jobs use `[self-hosted, Linux, gpu]` and
  `/home/flip/github-action-runners/bin/gpu-run.sh` so GPU work stays serial.
  Reuse suitable organization workflows and never commit workflow secrets.

## Git, PR, and review

- Operator-facing notes may be Dutch; code, APIs, and public documentation are
  English. Use the configured implementing-model commit identity; never replace
  it with the reviewer identity.
- PR-Piet is the repository PR-review workflow. Post `/review` from a human
  account after the final commit and after every later push; bot senders are
  ignored. Apply correct suggested changes in sensible batches.
- Human merge is the default. An agent may merge only when the latest review
  covers the merge head, CI is green, no findings or threads remain open, and a
  closing PR comment confirms each finding was resolved or answered with
  evidence. Never auto-approve.
- Treat review findings as hypotheses. Verify before accepting or rejecting;
  use independent evidence for uncertain or high-impact conclusions. Preserve
  `if`/`elif` routing chains and pin changed public contracts with tests.
- A cancelled review run is a failed check: rerun it. If a deep `/review` times
  out on a large diff, rerun the automatic `pull_request` review.
