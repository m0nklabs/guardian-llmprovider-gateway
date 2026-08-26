# Guardian 2.0 — Master Implementation Plan

> One phased plan that turns the three architecture plans into working software:
> **provider-unification** (`docs/LAN_GPU_BACKENDS.md`), **gateway/manager split**
> (`docs/GATEWAY_MANAGER_SPLIT.md`), and **per-provider config files**
> (`docs/CONFIG_PROVIDER_FILES.md`) — plus moving the live deployment to the new
> repo (`guardian-llmprovider-gateway`), renaming the service, and producing a
> full file register.
>
> Status: **approved plan, not yet implemented.** Each phase has its own
> acceptance criteria, tests, and deployment impact (hot-reload vs. restart).
> Phases are sequential; dependencies are stated explicitly.
>
> The canonical copy of this plan lives in this file. The GitHub issue is a
> snapshot. `AGENTS.md` Skills links here.

---

## 0. Context & goals

- **Today:** one monolith `app/` serves local models (one managed
  llama-server process on `127.0.0.1:11440`) and cloud providers (6 entries in
  `config/providers.settings.yaml`). Local lifecycle (`app/engine/manager.py`,
  ~1637 lines) is entangled with registry/choice/discovery (~509 lines) and
  traffic-aware logic (idle-unload reads the queue).
- **Goal state (operator 2026-08-26):**
  1. **Everything that serves models is a provider.** Local becomes one
     (managed) provider entry; the Windows PC (14700K) and cloud providers are
     external entries. A per-GPU-host `caretaker-llama-cpp` daemon owns the
     local llama-server lifecycle behind a thin control API.
  2. **One config file per provider** (`config/providers/<name>.settings.yaml`),
     local/cloud distinction visible in the name (`ai-kvm2-local`,
     `14700k-local`, `openrouter`, `google`, …). No more defaults/overrides
     merge layer.
  3. **Guardian = gateway + raw logger** (capture WAL, replay); redaction and
     dataset building stay in Keanu. Unchanged by this plan.
  4. **New repo is the production build place**: `m0nklabs/guardian-llmprovider-gateway`
     (full git history, clean tree). Service renamed, deployment moved, legacy
     repo frozen.
  5. **A complete file register** (`docs/FILE_REGISTER.md`): every tracked file,
     its function, and the processes/files it relates to.

### Guiding principles

- **Backwards compatibility at every step**: existing local aliases, cloud
  addresses, and client behaviour must keep working (recognition is a superset,
  never a narrowing).
- **Test before claiming fixed**: `py_compile` + focused pytest, then the full
  pre-restart gate (`scripts/pre_restart_check.py`) before any restart.
- **Restarts are operator-run.** The agent's own model traffic routes through
  Guardian; a restart drops the session. The lead never restarts Guardian
  itself; the operator does, from outside the session, after the gate passes.
- **No hardcoded deployment literals.** All paths/ports/names flow through
  `config/` or `app/paths.py`.
- **One writer per file** during implementation; lead owns cross-file
  synthesis, workers do scoped implementation/measurement.

### Phase dependency graph

```
F0 Foundation (rename/new-dir/service)
   │
F1 File register (docs/FILE_REGISTER.md)          ── feeds F2..F7
   │
F2 Per-provider config files (CONFIG_PROVIDER_FILES)
   │
F3 Local as managed provider entry (LAN_GPU_BACKENDS step 1)
   │
F4 Untangle registry/choice/discovery from manager.py (GATEWAY_MANAGER_SPLIT step 0)
   │
F5 caretaker-llama-cpp daemon + local passive via contract (GATEWAY_MANAGER_SPLIT steps 1–2)
   │
F6 Windows/14700K provider + caretaker on Windows (LAN_GPU_BACKENDS step 2 + split step 3)
   │
F7 Cut-over to new dir, freeze legacy, optional RPC (Optie B)
```

---

## F0 — Foundation: rename, new directory, service (no behaviour change)

**Goal:** the new repo is the single source of truth; names/units/docs no
longer reference the legacy identity; the repo is public.

### F0.1 Repo identity & visibility
- Repo `m0nklabs/guardian-llmprovider-gateway` is **public** (done together
  with this issue).
- `m0nklabs/llama-cpp-guardian` = legacy/archive (description already marked;
  production still runs from `/home/flip/llama_cpp_guardian` until F7).
- `m0nklabs/caretaker-llama-cpp` = empty repo for the manager (filled in F5/F6).

### F0.2 Rename sweep (internal identity)
Replace the legacy identity everywhere in this repo (code, config, deploy,
docs, scripts, tests):
- `llama-guardian` → `guardian-llmprovider-gateway` (service unit, nginx conf
  filenames + internal `map`/`upstream`/`log_format` names, TLS drop-in, docs).
- `llama_cpp_guardian` → `guardian_llmprovider_gateway` (dir-derived names,
  workspace file `llama_cpp_guardian.code-workspace` → new name, script
  variables `LLAMA_CPP_GUARDIAN_*` → `GUARDIAN_LLMPROVIDER_GATEWAY_*` or
  `GUARDIAN_*`).
- `app.main` module name stays (Python package is `app`); no functional rename
  of internal Python module paths — only identity strings that leak into
  service names, logs, file names, and docs.
- Update: `deploy/nginx/*.conf`, `deploy/systemd/*`, `scripts/*` (pre_restart,
  verify_post_restart, guardianctl, verify_prompts), `README.md`,
  `CHANGELOG.md` (new entry), `docs/skills/operator-runbook.md`, docs
  `CLIENT_KEY_LINKING.md`, `LLM_ROUTER.md`, `.github/copilot-instructions.md`,
  `tests/integration/test_live_inference.py` (service-name references).
- Keep **compat aliases** where a rename would break external references:
  e.g. systemd `Alias=llama-guardian.service` so existing `systemctl` users
  keep working until F7.

**Files:** `deploy/**`, `scripts/**`, `README.md`, `CHANGELOG.md`, `docs/**`,
`.github/**`, `llama_cpp_guardian.code-workspace`.
**Tests:** repo tests must stay green (rename is doc/string/deploy-level; the
full pytest suite must pass).
**Deployment impact:** none for the running service yet (legacy still runs);
new unit files are staged in `deploy/systemd/`, not activated until F7.

### F0.3 CI on the new repo
- Wire the org-pool reusable `python-ci` workflow (`m0nklabs/github-action-runners`),
  replacing/augmenting the CodeQL-only workflow.
- Optionally add the long-open item: `scripts/pre_restart_check.py` as a CI
  gate (issue from `20260813_1`).
- **Acceptance:** a push runs lint + pytest on the org runners; green.

---

## F1 — File register (`docs/FILE_REGISTER.md`)

**Goal:** for every tracked file in the repo: its **function** and the
**processes/files it is related to**. This is the map the later phases use to
move code confidently.

### Output shape
One section per top-level area (`app/`, `config/`, `deploy/`, `scripts/`,
`tests/`, `dashboard/`, `docs/`, root files). Per file:

```markdown
| Path | Function | Related processes/files |
|---|---|---|
| `app/engine/manager.py` | llama-server lifecycle: spawn/stop/reload, args build, drift detection, health/crash watchdog, VRAM slot, unload | processes: llama-server (`:11440`), ComfyUI (VRAM); files: `config/models.local.settings.yaml`, `config/current_model.args`, `config/current_model.env`, `app/local_inference/models.py`, `app/proxy/lifespan.py` |
```

### Method
1. **Mechanical pass (script):** for each tracked file, extract module
   docstring + `import` lines (related files/modules), and references to
   config files / ports / service names.
2. **LLM pass (workers):** per area, a fresh-context worker reads the file set
   and writes the Function/Related columns; lead synthesizes the single
   register.
3. **Cross-check:** `grep` for every `app/` import target to confirm the
   "related files" edges are bidirectional and complete.
4. Commit as `docs/FILE_REGISTER.md`; link from `AGENTS.md` (Directory map →
   "full register").

**Acceptance:** 100% of tracked files appear; every `from app.x import` edge
appears in the Related column of both sides; no secrets/paths beyond repo
layout. A stale register after later phases is a bug — F2..F7 update it.

---

## F2 — Per-provider config files (`CONFIG_PROVIDER_FILES`)

**Goal:** `config/providers/<name>.settings.yaml` per provider; the
defaults/overrides merge layer and the separate cloud-overrides file
disappear.

### New layout
```
config/
├─ global.settings.yaml           (unchanged — cross-cutting)
├─ guardian.keys.yaml             (unchanged — keys)
└─ providers/
   ├─ ai-kvm2-local.settings.yaml   (was models.local.settings.yaml + local provider block)
   ├─ 14700k-local.settings.yaml    (later, F6 — same shape)
   ├─ openrouter.settings.yaml      (base_url + api_key + prefixes + catalog_url + per-model overrides)
   ├─ nvidia.settings.yaml
   ├─ google.settings.yaml
   ├─ openai.settings.yaml
   ├─ poolside.settings.yaml
   └─ groq.settings.yaml
```
- Per-provider file shape per `docs/CONFIG_PROVIDER_FILES.md` §Doelbeeld
  (`enabled`, `base_url`, `api_key: ${VAR}`, `timeout_seconds`,
  `model_prefixes`, `catalog_url`, `catalog_allowlist`, `models:` block for
  per-model overrides). Local file additionally carries `management_url` +
  `local: true` marker + the local model registry.
- Overrides file **removed**; one file per provider is the truth. Secrets stay
  `${VAR}`.

### Code changes
1. `app/paths.py`: `PROVIDERS_DIR = CONFIG_DIR / "providers"`,
   `provider_settings_file(name)`, `provider_names()` (scan `*.settings.yaml`);
   keep `local_models_file()` resolving to `providers/ai-kvm2-local.settings.yaml`
   (compat for scripts/tests).
2. `app/config_loader.py`: two-file deep-merge → directory scan (one dict per
   provider, no merge layer).
3. `app/proxy/providers.py`: `_load_settings_config` reads the directory;
   `context_overrides`/`model_defaults` come from each provider's `models:`
   block (was `models.cloud.overrides.yaml`).
4. `app/engine/manager.py` + `app/local_inference/models.py`: local models via
   the helper (same reader, new path).
5. `app/gateway/context_metadata.py` + `app/cloud_inference/routing.py`:
   `get_override` reads the provider file.
6. Migration script or careful manual split (6 providers, 23 local models,
   ~10 cloud overrides), then delete the 4 old files
   (`providers.settings.yaml`, `providers.overrides.yaml`,
   `models.local.settings.yaml`, `models.cloud.overrides.yaml`).
7. Tests: `tests/legacy` single-file `settings_path=` keeps working (explicit
   path constructor stays); new unit tests for directory scan +
   `*-local` → local-provider recognition.

**Acceptance:** full pytest + pre-restart gate green; after restart
`/v1/models` byte-identical to pre-migration (same aliases, cloud entries,
context metadata); `openrouter/…` and local alias → 200; catalog refresh works
with correct per-provider `credential_status`; **new provider = one file in
`config/providers/` + hot-reload** (the payoff).
**Deployment impact:** code change → **operator-run restart** (gate first).

---

## F3 — Local as a managed provider entry (`LAN_GPU_BACKENDS` step 1)

**Goal:** routing recognises local via the provider registry, not a parallel
branch. `local` is the only `managed: true` entry (Guardian owns its
lifecycle); everything else (Windows, cloud) is a passive endpoint.

### Changes
1. `app/proxy/providers.py`: `CloudProvider.managed: bool` (default False);
   `is_cloud_model`/`get_provider_for_model` and route dispatch treat
   `local/…` as provider resolution.
2. `app/gateway/routing.py` + `app/local_inference/models.py`: local
   recognition comes from the providers registry; existing local aliases keep
   working (recognition is a superset).
3. Catalog: `local` gets its own catalog refresh (llama-server `/v1/models` —
   verified working, advertises the loaded model).
4. Discovery (`/v1/models`): local as today (aliases) but from the same
   registry; Windows models will appear as `windows/<model>` in F6.
5. Failover groups: `local` and `windows` become groupable (local → Windows
   fallback or reverse) — a new capability of the unification.
6. Backwards-compat: all existing local aliases + cloud addresses unchanged.

**Acceptance:** full suite + gate green; restart; `/v1/models` identical;
local alias → 200; failover group `local → windows` prepared (F6 wires the
actual Windows endpoint).
**Deployment impact:** routing refactor → **operator-run restart**.

---

## F4 — Untangle registry/choice/discovery from `manager.py`
(`GATEWAY_MANAGER_SPLIT` step 0)

**Goal:** move the ~509 registry/choice/discovery lines out of
`app/engine/manager.py` into the gateway layer. Pure relocation, **no
behaviour change** — this makes the future split boundary clean and shrinks
"the manager" to its real core.

### What moves (per the split analysis)
- `resolve_model`, `get_preferred_tool_model`, `get_advertised_context_window`,
  vision-capability cache, `get_public_model_map` → gateway modules
  (`app/gateway/` or `app/local_inference/models.py`).
- `app/local_inference/models.py` resolution/sizes/timeouts (~130 lines)
  already sits in the gateway side — confirmed.
- Manager keeps: spawn/stop/reload, args build, launch-signature drift
  detection, health/crash watchdog + auto-restart, unload, VRAM-slot
  acquire/release, `reload_backend_after_connect_error`, idle-unload watcher
  (~1050 lines core).

**Acceptance:** same tests pass with the same outcomes; `manager.py` shrinks
toward the lifecycle core; no public behaviour change; `git diff` reviewed
before push. Update `docs/FILE_REGISTER.md`.
**Deployment impact:** code change → **operator-run restart** (after gate).

---

## F5 — caretaker-llama-cpp daemon; local becomes passive
(`GATEWAY_MANAGER_SPLIT` steps 1–2)

**Goal:** the local llama-server lifecycle runs in a separate process
(`caretaker-llama-cpp`) next to llama-server, behind a thin control API. The
gateway no longer owns the local lifecycle; it talks to the manager via
`management_url` + `POST /ensure`.

### Manager control API (thin)
```
GET  /status            → loaded model + gpu/vram status
POST /ensure {model}    → load/swap (VRAM slot); idempotent
POST /unload            → unload (idle-unload with traffic input from gateway)
```
- OpenAI inference stays direct to llama-server (`http://127.0.0.1:11440/v1`).
- **Traffic contract:** gateway passes active request/queue counts (or only
  calls `/unload` when safe); idle decision stays in the gateway, execution in
  the manager.
- **404/503 recovery:** gateway catches `model_not_loaded`/503 and retries
  with `/ensure` (same recovery paths as today's connect-error).

### In-repo first (monorepo daemon)
- Per the plan: keep it in this repo as a second process + systemd unit
  (`caretaker-llama-cpp.service`) first; splitting into the separate
  `m0nklabs/caretaker-llama-cpp` repo is the **last** step, only once stable.
- `local` becomes a passive provider entry:
  ```yaml
  providers:
    local:
      base_url:       http://127.0.0.1:11440/v1
      management_url: http://127.0.0.1:11441
      managed: false
  ```
- Old direct lifecycle code removed from the gateway; `engine/manager.py`
  becomes the caretaker package root (moved under `caretaker/` or a new
  top-level `manager/` dir per F4 outcome).

**Acceptance:** both services run side-by-side; local inference through the
gateway → 200 with the model ensured via `/ensure`; idle-unload only fires
when the gateway says the queue is empty; gateway restart does NOT drop the
loaded model; manager restart → gateway recovers via `/ensure`; full suite +
gate green.
**Deployment impact:** new systemd unit + code change → **operator-run
restart** of gateway; new `caretaker-llama-cpp.service` started by operator.

---

## F6 — Windows/14700K provider + caretaker on Windows
(`LAN_GPU_BACKENDS` step 2 + split step 3)

**Goal:** the 14700K Windows GPU serves models via the LAN as a passive
provider, managed by its own caretaker instance. Multiple models in parallel
across the LAN (the operator's stated goal — not one split model).

### Windows side (operator)
1. llama.cpp CUDA release on the Windows PC; per model a process
   (`llama-server.exe -m <model>.gguf --host 0.0.0.0 --port 11440/11441/... -ngl 99 --api-key <lan-key>`).
2. caretaker-llama-cpp on Windows as a Windows service (NSSM/scheduled task),
   reading its **own** `models.local.settings.yaml` (14700K GGUF paths) — no
   copy of the Linux list.
3. Firewall: open inbound ports; GGUF local on Windows (or SMB share).

### Gateway side (mostly config)
- New provider file `config/providers/14700k-local.settings.yaml`
  (base_url `http://192.168.1.x:11440/v1`, `api_key: ${WINDOWS_LAN_KEY}`,
  `management_url: http://192.168.1.x:11441`, `catalog_url: /v1/models`).
- `windows/…`/`14700k-local/…` model addresses appear in discovery + routing
  automatically via the provider machinery (F2+F3).
- Optional: `lan: true`-style flag to allow keyless llama-server if the
  operator prefers not to set `--api-key` (recommended: always set it).
- Context metadata: llama-server reports no `context_length` → per-model
  `context_window` overrides in the provider file (same pattern as NVIDIA).

**What works for free:** catalog discovery, cloud-style routing, SSE
streaming + heartbeat, raw capture (cloud path), failover groups
(`local → 14700k-local`).
**What does NOT apply:** lifecycle/idle-unload/auto-switch on the Windows GPU
(operator manages it); VRAM scheduler does not see the Windows GPU.
**Acceptance:** `curl http://<win-ip>:11440/v1/models` → 200; catalog refresh
shows the new provider with `credential_status: ok`;
`14700k-local/<model>` chat (non-stream + stream) → 200 with capture
`request_completed` in the WAL; gate + `verify_post_restart.py` green.
**Deployment impact:** provider file = **config-only → hot-reload**; caretaker
on Windows = Windows-side ops.

---

## F7 — Cut-over to the new directory & freeze legacy (+ optional RPC)

**Goal:** production runs from `/home/flip/guardian-llmprovider-gateway`.

### Steps (operator-run, coordinated)
1. Pre-restart gate green in the **new** dir (venv symlink, `.env`, keys,
   `data/` already present — copied 2026-08-26).
2. Stage new systemd unit `guardian-llmprovider-gateway.service`
   (`WorkingDirectory=/home/flip/guardian-llmprovider-gateway`,
   `ExecStart=<new-dir>/venv/bin/python3.14 -m app.main`, `Alias=llama-guardian.service`)
   + `caretaker-llama-cpp.service` (from F5) + llama-server unit (unchanged).
3. Stage renamed nginx confs (`deploy/nginx/guardian-*`) + TLS drop-in; keep
   the same public endpoints (`:11434` TLS-mux, `:11437` dashboard) so clients
   are unaffected.
4. Operator: stop legacy `llama-guardian.service`, start the new units,
   `nginx -t` + reload, run `verify_post_restart.py`.
5. Verify: `/v1/models`, local + cloud inference, capture status, dashboard.
6. **Freeze legacy:** `/home/flip/llama_cpp_guardian` keeps its repo link to
   `m0nklabs/llama-cpp-guardian` (archive); no further deploys from there.
7. Update `AGENTS.md` (OPERATIONALLY CRITICAL section flips to the new dir;
   production no longer runs from legacy) + `docs/FILE_REGISTER.md`.

### Optional later — Optie B (llama.cpp RPC)
- One model spanning both GPUs via `llama-rpc-server` + `--rpc <pc>:50052`.
  Feasible on 1 Gbit for chat (~5–15% overhead); not for prefill-heavy
  workloads. Stays an engine-start-arg, **not** a provider entry. Only if the
  operator wants a single model that fits on no one GPU.

**Acceptance:** new dir serves everything; legacy stopped; clients (pi, goose,
ollama, dashboard) unaffected; full gate green; rollback = restart legacy unit.

---

## Cross-cutting: file register maintenance

`docs/FILE_REGISTER.md` (F1) is a living document: every phase that renames,
moves, or adds files must update it in the same commit. A stale register is a
bug (same rule as a stale handoff in AGENTS.md).

## Open questions to resolve before/during F5–F6

1. Monorepo-with-two-daemons vs. split into the separate
   `caretaker-llama-cpp` repo immediately (plan recommends in-repo first).
2. Does the manager itself do `POST /pick` (move choice into manager) or does
   the gateway keep the choice and the manager only executes?
   (Recommended: gateway keeps the choice.)
3. Where does `scheduler/manager.py` (maintenance/services-stopper) end up?
   (Stays in the gateway or moves to systemd/cron — operator choice.)
4. Windows PC IP/port, which GGUFs, how much VRAM on the 14700K.
5. Provider naming confirmed: `ai-kvm2-local`, `14700k-local`.
6. `--api-key` on Windows llama-server (recommended: yes) vs. a keyless
   `lan: true` flag.
