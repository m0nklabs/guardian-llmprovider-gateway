<!-- See AGENTS.md (repo root) for the canonical source of truth. This file is Copilot-native. -->
# GitHub Copilot Instructions

This repo is **Llama-CPP Guardian** — a Python 3.14 FastAPI proxy that
sits in front of `llama-server` (:11440) and provides auth, queueing,
model switching, cloud routing (OpenRouter + NVIDIA + Poolside), and an Anthropic
API bridge. It runs as systemd service `llama-guardian.service` on
`:11434`. Frontend dashboard on `:11437`.

## Core requirement

**Always test before claiming fixed:**
```bash
./venv/bin/python -m py_compile <changed_file>
./venv/bin/python -m pytest tests/ -x
```

## Where to look

- **Full canonical rules** (critical rules, directory map, skills): read `AGENTS.md` in the repo root.
- **Cloud model routing logic:** `docs/LLM_ROUTER.md`
- **Streaming keepalives** are mandatory on all cloud paths — see `app/proxy/server.py` `_iter_sse_lines_with_watchdog`.
- **Config:** `config/settings.yaml`, `config/models.yaml`, `config/api_keys.json`
- **Restart required** to apply code changes — no hot reload.
