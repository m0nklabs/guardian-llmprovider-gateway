# F5 Gateway-Wiring: lifecycle-aanroepen in kaart (READ-ONLY analyse, 2026-08-28)

> Bron: analyse-worker (7d9db2b1, read-only) voor de F5 gateway-wiring
> (GATEWAY_MANAGER_SPLIT fases 1–2 / IMPLEMENTATION_PLAN §F5). Dit document is
> de blauwdruk: wat verhuist naar de caretaker-daemon (`management_url`),
> wat blijft in de gateway, en welke risico's het contract bepalen.

## 1. Lifecycle-aanroep-sites in de gateway (buiten `engine/manager.py`)

Legenda lifecycle-API's (manager.py): `load` (:644), `unload` (:577),
`switch_model` (:448), `startup_check` (:380), `backend_health_ok` (:436),
`get_current_model` (:432), `verify_backend_model` (:314), `is_unloaded`/
`last_request_time`/`active_requests`/`current_model` (runtime-state),
registry-delegators (bleven in de gateway, F4).

### (a) Traffic-path (per request) — LIFECYCLE NODIG → F5 `/ensure`
- `app/gateway/routing.py:478,490,497` — `is_unloaded` + `load(...)` (auto-reload) | s
- `app/gateway/routing.py:529` — `is_switch_allowed(client_id)` | f (blijft gateway)
- `app/gateway/routing.py:565,576` — `switch_model(...)` / `load(...)` (auto-switch) | s
- `app/gateway/routing.py:511-512,622,...` — `last_request_time`/`active_requests` mutatie | f
- `app/gateway/routing.py:525,550` — `current_runtime_uses_mmproj` | f
- `app/gateway/routing.py:593,754,988` — `mark_vision_validation(...)` | f
- `app/local_inference/ollama.py:288,299,306` — `is_unloaded` + `load(...)` | s
- `app/local_inference/ollama.py:312,314,335` — `is_switch_allowed` + `switch_model(...)` | s
- `app/local_inference/ollama.py:311,326,689,702` — `get_current_model()` | f
- `app/local_inference/ollama.py:353-354,496-497,727,...` — `last_request_time`/`active_requests` | f
- `app/gateway/normalization.py:190,217,...` — `get_vision_capability`/`mark_vision_validation` | f
- `app/local_inference/models.py:251,259-264,279` — herstelpad (§3) | s

**Conclusie:** `load`/`switch_model`/`unload` zitten op de request-hotpath →
dit worden de F5-`/ensure`-calls. `get_current_model`/`is_unloaded`/
`active_requests` zijn lees-operaties → na F5 lokaal gecached of via `GET /status`.

### (b) Background / scheduler
- `app/proxy/lifespan.py:156` — `pinned_model`/`current_model` (startup-target) | f
- `app/proxy/lifespan.py:226-241` — **idle-unload watcher** (`unload()`-aanroep) | **s** (§2)
- `app/proxy/process.py:281-283,323` — `backend_health_ok`, `verify_backend_model`,
  `get_current_model`, `startup_check` (background startup-verificatie) | **s**

### (c) Admin / API-handlers
- `app/proxy/server.py:1145-1148` — **`/admin/unload`** → `is_unloaded` + `unload()`
  (de enige directe lifecycle-call in server.py) | **s**
- `app/gateway/admin_api.py:657` — `/admin/load` → `load(target, ...)` | **s**
- `app/gateway/admin_api.py:633,643,654` — `resolve_model`, `current_model` | f
- `app/gateway/admin_api.py:236-284` — status/crash-history lezing
  (`crash_history`, `_model_verified`, `_last_verification_at`, ...) | f

**Kloof:** verificatie-tijdstempels lezen privévelden van de manager → na F5
via `GET /status` van de caretaker.

### (d) Discovery (status-lezen) — blijft in de gateway
- `app/gateway/model_discovery.py:133-137,165,259,263,300` — `models.keys()`,
  `get_public_model_map()`, `resolve_model()` (registry, F4)
- `app/gateway/context_metadata.py:81,104,129,233-235,244` — `get_current_model`,
  context windows, `get_vision_capability`

**Conclusie:** discovery leest uitsluitend registry-delegators + context — géén
lifecycle. Blijft lokaal in de gateway (geen caretaker-HTT).

## 2. Idle-unload watcher (`app/proxy/lifespan.py:222-241`)

Timer-poll (60s); guards: `idle_unload_minutes` (None = disabled),
`is_unloaded`, `active_requests > 0`, `_inference_queue.active_count/waiting_count`
(active + waiting), daarna `idle_secs >= idle_minutes*60` → `await _model_manager.unload()`.

- **Beslissing blijft in de gateway** (kent queue/requests).
- **Uitvoering → F5:** de `unload()` wordt `POST {management_url}/unload` (Bearer-key).
- **Risico:** `manager.unload()` is nu atomair (`_stop_server`); HTTP `/unload` is een
  afstandsaanroep → vereist idempotentie aan caretaker-zijde ("already unloaded") en
  dat de gateway niet doorgaat met `is_unloaded=False` als de call faalt.

## 3. Connect-error / stale-backend herstel (`app/local_inference/models.py:249-293`)

`reload_backend_after_connect_error(path, error)`: `get_current_model` →
`resolve_auto_reload_model` → lock → `backend_health_ok()` (healthy → no-op) →
`_run_guardian_operation(operation=lambda: _model_manager.load(reload_model))`;
bij `ModelLoadError`/andere exceptie → HTTP 503.

Call-sites (alleen local-route, cloud-raakt dit niet):
- `app/gateway/routing.py:686-688` (streaming) — na `httpx.ConnectError` retry-once, dan 502.
- `app/gateway/routing.py:931-932` (non-streaming) —zelfde, retry-fout → 502.
- `app/local_inference/ollama.py`: **geen** connect-error recovery (streams breken af).

**F5-variant:** beide paden + de kern worden `POST {management_url}/ensure`
(model=reload_model); retry-once-semantiek en de "is de backend dood"-beslissing
blijven in de gateway.

## 4. Config (wat verhuist naar caretaker-config)

- `app/paths.py:161 local_models_file()` → `config/providers/ai-kvm2-local.settings.yaml`.
- `config/providers/ai-kvm2-local.settings.yaml`:
  - `:3` **`management_url: http://127.0.0.1:11441` — staat er AL** (F5-klaar).
  - `:2` `base_url: http://127.0.0.1:11440/v1`, `:4` `local: true`, `:5` `models:` blok.
  - `:304-307` `guardian:` blok: `pinned_model`, `switch_allowlist`, `idle_unload_minutes: 5`.
- Lifecycle-args (uit `models:`-lijst): `path`, `context`/`ctx`, `tensor_split`,
  `mmproj`, `extra_args`, `ngl`, `kv_type`, `cuda_visible_devices`,
  `draft_model_path`/`spec_*`, `n_slots` → met de lifecycle mee naar caretaker-config.
- `config/global.settings.yaml`: `services.comfyui_url` (`_get_comfyui_url`,
  manager.py:614-622) → caretaker (VRAM-vrijgave).
- **Blijft in gateway:** `base_url`/`management_url`/`enabled`/`local`
  (provider-routing) + registry-mirror blijft `models:`-lijst lokaal lezen (F4).

## 5. Wat NIET verhuist (blijft in de gateway)

- **Registry/keuze/discovery (F4, `model_registry.py`):** `models`, `get_public_model_map`,
  `resolve_model`, `resolve_reload_target`, preferred tool/reasoning, context windows,
  `build_runtime_config`.
- **Vision:** `get_vision_capability`, `mark_vision_validation`, `current_runtime_uses_mmproj`.
- **Switch-permissie:** `is_switch_allowed` (security → gateway beslist, caretaker past toe).
- **Runtime-state readouts die de gateway blijft serveren** (admin/status/metrics):
  `current_model`, `pinned_model`, `is_unloaded`, `idle_unload_minutes`,
  crash-history, verificatie-status — via gecachte `GET /status` i.p.v. directe lezing.

**Direct in server.py te vervangen:** `/admin/unload` (:1142-1148) → `POST .../unload`.

## 6. Risico's / afhankelijkheden (bepalen het F5-contract)

1. **Queue-lezen in idle-unload = de kern van het contract.** Beslissing (lokaal) +
   uitvoering (HTTP) splitst een eerder atomair pad. Race "request binnen terwijl
   `/unload` onderweg is" → caretaker-side lock + gateway houdt guards.
2. **`/admin/unload` is een directe lifecycle-call buiten admin_api** → knip nodig.
3. **Retry-eenmalig maar multi-aanroeper** (stream + non-stream onder dezelfde
   `_model_switch_lock`) → na F5 gelijkwaardige lock/serialisatie rond `/ensure`.
4. **Runtime-state bij discovery/admin/metrics** (metrics.py:250-255 leest live)
   → gateway moet een **gecachte status-spiegel** onderhouden (periodiek `GET /status`
   + push van load/unload-respons), anders worden admin `server_status` en metrics stale.
5. **`idle_unload_minutes`** wordt nu uit `guardian:`-blok gelezen (manager.py:568-575)
   én door admin `server_status` → één bron van waarheid kiezen (config-duplicatie-risico).
6. **`pinned_model`/`switch_allowlist`** zijn security + config-path (manager.py:220-243) →
   waarde verhuist naar caretaker-config; gateway blijft ze lezen voor de switch-gate.
7. **Cloud-only setups:** connect-error-recovery mag cloud-routes niet raken (alleen
   local-route vangt `ConnectError`, routing.py:205-comment).

## Kern-afbakening F5

- **Naar caretaker (`management_url` 11441, al in config):** `load`/`switch_model`/
  `unload` (spawn/stop/reload/VRAM-free) + health-verificatie-uitvoering
  (`startup_check`, `verify_backend_model`, `backend_health_ok`, crash-detectie) +
  model/lifecycle-args uit `models:`/`guardian:`.
- **Blijft in gateway:** registry/keuze/discovery (F4), `is_switch_allowed`-beslissing,
  idle-unload-beslissing (queue + active_requests), vision-capability-cache,
  connect-error-retry-once-orkestratie, admin/status/metrics-readouts (bovenop
  gecachte `GET /status`).
- **API-contract F5 fases 1–2:** `POST /ensure` (load/switch/check), `POST /unload`,
  `GET /status` (health/current_model/is_unloaded/crash/verified/timestamps),
  allen met Bearer-key (`CARETAKER_KEY`).