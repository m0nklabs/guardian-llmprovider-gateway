# Guardian TODO List

## 2026-05-16 Operational Hardening Sprint

- [x] Refactor `max_context` to `benchmark_context_limit` across config, code, tests, and docs while keeping OpenAI-compatible `max_context` output where clients still require it.
- [x] Make Guardian bind `11434` immediately after restart by moving startup verification out of blocking FastAPI startup.
- [x] Make PID-file handling restart-safe and wait briefly for an existing `11434` listener to release during systemd handoff.
- [x] Expand `/api/status` with generation-safe operation status, proxy listener diagnostics, backend verification timestamps, and routing recommendations.
- [x] Expand `/api/status` further with explicit switch ownership, current requested target, live queue state, and last successful backend verification timing for faster operator debugging.
- [x] Make `/admin/load` accept aliases and serialize with the shared model-switch lock.
- [x] Improve `model: auto` ergonomics by preferring tool-friendly sibling profiles for reasoning-heavy model families.
- [x] Improve Ollama bridge ergonomics by falling back to reasoning output when a reasoning model emits no visible content.
- [x] Add regression coverage for startup/status generation handling and preferred tool-model selection.
- [x] Add a live restart-race regression that restarts the active Guardian systemd service, performs an immediate alias load, checks status, and runs a mini chat.
- [x] Validate the changes with focused unit tests, live metadata probes, restart smoke checks, and practical end-to-end load/inference tests.