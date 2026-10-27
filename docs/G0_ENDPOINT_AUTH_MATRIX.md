# G0-01 — Endpoint, Auth and Route Matrix

Baseline inventory required by `docs/ROADMAP.md` §4 G0 and backlog row G0-01. It
links every route registered on both FastAPI apps to the handler that serves it,
the authentication dependency the code applies, the privilege class the handler's
semantics require, and the test that invokes it.

**Guard:** `tests/unit/test_g0_endpoint_matrix.py` collects both apps and fails
when this file and the code disagree.

**Route count: 57 — 41 proxy, 16 dashboard.** One row per `(method, path)` of a
registered route object; `HEAD` is folded into `GET` (Starlette adds it to every
`GET` route) and a method-less route is classified by kind: a `Mount` is one
`MOUNT` row, a WebSocket route is `WEBSOCKET`, anything else is `OTHER`. Both apps
also register the four FastAPI documentation routes; they are listed because they
are reachable, not because they belong to the Guardian API.

## Reading the columns

- **Auth dependency** — the dependency present in the route's own `dependant`
  graph. `none` means the handler takes no auth dependency at all.
- **Privilege class** — the class the handler's semantics require:
  `public` = reachable without a key by design; `client` = an authenticated
  caller acting on its own models and requests; `admin` = reads or mutates
  shared state (keys, catalogs, capture, scaler, sessions, diagnostics);
  `operator` = process/model lifecycle and runtime configuration, i.e.
  restart-equivalent impact.
  **The code does not enforce these classes.** Every protected route uses the
  same flat `verify_api_key`, so one valid key reaches `admin` and `operator`
  routes alike — the verified gap recorded in `docs/ROADMAP.md` §G1. This column
  is the target classification for G1-01, not a claim about current enforcement.
- **Covering tests** — up to three tests that invoke the route: an HTTP request
  through the app, or a direct `await` of the endpoint function. `none` means no
  test invokes it. Two markers:
  - `†` — delegate-level: the test calls the implementation the handler
    delegates to (`app.gateway.*`), not the handler itself, so a route-level
    auth or wiring regression would not be caught by it.
  - `tests/integration/…` — live-service test, deselected by default; run with
    `-m integration` against a running Guardian.
- **Handler** — for repo code, the `def` line of the endpoint (`file.py:line`);
  the guard fails when a handler moves out of the documented line range, so a
  stale row cannot survive. Framework-owned handlers carry the module only
  (`fastapi/applications.py`), because pinning a line inside an installed
  dependency would turn the guard red on a routine version bump; the guard still
  checks that the module resolves in the installed package.

## Proxy surface — `app.proxy.server:app` (`:11434`)

| # | Surface | Method | Path | Handler | Auth dependency | Privilege | Covering tests |
|---|---------|--------|------|---------|-----------------|-----------|----------------|
| 1 | proxy | GET | `/openapi.json` | `fastapi/applications.py` | none | public | none |
| 2 | proxy | GET | `/docs` | `fastapi/applications.py` | none | public | none |
| 3 | proxy | GET | `/docs/oauth2-redirect` | `fastapi/applications.py` | none | public | none |
| 4 | proxy | GET | `/redoc` | `fastapi/applications.py` | none | public | none |
| 5 | proxy | POST | `/api/chat` | `app/proxy/server.py:1171` | `verify_api_key` | client | `tests/unit/test_model_switch_verification.py::test_ollama_chat_fails_closed_when_backend_serves_other_model`<br>`tests/unit/test_model_switch_verification.py::test_ollama_chat_same_model_reload_still_succeeds` |
| 6 | proxy | POST | `/api/generate` | `app/proxy/server.py:1179` | `verify_api_key` | client | `tests/unit/test_model_switch_verification.py::test_ollama_generate_fails_closed_when_backend_serves_other_model` |
| 7 | proxy | GET | `/api/tags` | `app/proxy/server.py:1191` | `verify_api_key` | client | `tests/integration/test_live_inference.py::TestRestartRace::test_api_tags` |
| 8 | proxy | GET | `/healthz` | `app/proxy/server.py:1200` | none | public | none |
| 9 | proxy | GET | `/v1/models` | `app/proxy/server.py:1247` | `verify_api_key` | client | `tests/unit/test_server.py::test_list_models_includes_cloud_models`<br>`tests/unit/test_server.py::test_list_models_includes_vision_metadata`<br>`tests/unit/test_server.py::test_list_models_includes_failover_groups` |
| 10 | proxy | GET | `/v1/models/{model_id:path}/endpoints` | `app/proxy/server.py:1253` | `verify_api_key` | client | `tests/unit/test_model_endpoints.py::test_cloud_model_served_by_two_providers_yields_two_endpoints` †<br>`tests/unit/test_model_endpoints.py::test_unserved_model_gets_no_fabricated_endpoint` †<br>`tests/unit/test_model_endpoints.py::test_cloud_access_disabled_returns_403` † |
| 11 | proxy | GET | `/v1/models/{model_id:path}` | `app/proxy/server.py:1268` | `verify_api_key` | client | `tests/unit/test_server.py::test_get_model_metadata_resolves_public_alias`<br>`tests/unit/test_server.py::test_get_model_metadata_returns_cloud_model`<br>`tests/unit/test_server.py::test_get_model_metadata_returns_404_for_unknown_failover_group` |
| 12 | proxy | POST | `/api/show` | `app/proxy/server.py:1278` | `verify_api_key` | client | `tests/unit/test_server.py::test_ollama_show_reports_context_for_cloud_model`<br>`tests/unit/test_server.py::test_show_model_failover_address_stays_cloud_branch` |
| 13 | proxy | POST | `/admin/unload` | `app/proxy/server.py:1286` | `verify_api_key` | operator | `tests/unit/test_lifespan_caretaker.py::test_admin_unload_delegates_to_caretaker`<br>`tests/unit/test_lifespan_caretaker.py::test_admin_unload_falls_back_to_local_unload_on_caretaker_unavailable`<br>`tests/unit/test_lifespan_caretaker.py::test_admin_unload_503_on_definitive_caretaker_refusal` |
| 14 | proxy | POST | `/admin/load` | `app/proxy/server.py:1380` | `verify_api_key` | operator | `tests/unit/test_server.py::test_admin_load_returns_400_for_runtime_override_validation_error`<br>`tests/unit/test_server.py::test_admin_load_passes_kv_type_runtime_override` |
| 15 | proxy | GET | `/api/keys` | `app/proxy/server.py:1389` | `verify_api_key` | admin | none |
| 16 | proxy | POST | `/api/keys` | `app/proxy/server.py:1395` | `verify_api_key` | admin | none |
| 17 | proxy | GET | `/api/cloud/ratelimit-stats` | `app/proxy/server.py:1402` | `verify_api_key` | admin | none |
| 18 | proxy | GET | `/api/cloud/providers` | `app/proxy/server.py:1408` | `verify_api_key` | admin | none |
| 19 | proxy | GET | `/api/cloud/catalog` | `app/proxy/server.py:1414` | `verify_api_key` | admin | `tests/unit/test_server.py::test_list_cloud_catalog_admin_reports_provider_state` |
| 20 | proxy | POST | `/api/cloud/catalog/refresh` | `app/proxy/server.py:1420` | `verify_api_key` | admin | `tests/unit/test_server.py::test_refresh_cloud_catalog_admin_force_refresh` |
| 21 | proxy | POST | `/api/config/reload` | `app/proxy/server.py:1426` | `verify_api_key` | operator | `tests/unit/test_config_reload.py::test_admin_reload_config_orchestrates_all_subsystems` †<br>`tests/unit/test_config_reload.py::test_reload_config_skips_retry_defaulting_when_settings_failed` † |
| 22 | proxy | GET | `/api/cloud/models` | `app/proxy/server.py:1438` | `verify_api_key` | admin | none |
| 23 | proxy | GET | `/api/crashes` | `app/proxy/server.py:1445` | `verify_api_key` | admin | none |
| 24 | proxy | GET | `/api/status` | `app/proxy/server.py:1451` | `verify_api_key` | admin | `tests/integration/test_live_inference.py::TestStatus::test_api_status`<br>`tests/integration/test_live_inference.py::TestRestartRace::test_api_status_vram_fields` |
| 25 | proxy | GET | `/api/capture/status` | `app/proxy/server.py:1457` | `verify_api_key` | admin | none |
| 26 | proxy | POST | `/api/capture/rotate` | `app/proxy/server.py:1463` | `verify_api_key` | admin | none |
| 27 | proxy | GET | `/metrics` | `app/proxy/server.py:1472` | none | public | `tests/integration/test_live_inference.py::TestRestartRace::test_metrics_endpoint` |
| 28 | proxy | GET | `/api/scaler` | `app/proxy/server.py:1488` | `verify_api_key` | admin | none |
| 29 | proxy | PUT | `/api/scaler` | `app/proxy/server.py:1494` | `verify_api_key` | admin | none |
| 30 | proxy | POST | `/api/scaler/reset` | `app/proxy/server.py:1500` | `verify_api_key` | admin | none |
| 31 | proxy | POST | `/api/scaler/recommend` | `app/proxy/server.py:1506` | `verify_api_key` | admin | none |
| 32 | proxy | GET | `/v1/queue/status` | `app/proxy/server.py:1515` | `verify_api_key` | client | `tests/integration/test_live_inference.py::TestQueueBehavior::test_status_shows_active_during_inference`<br>`tests/integration/test_live_inference.py::TestRestartRace::test_queue_status` |
| 33 | proxy | GET | `/v1/queue/requests/{request_id}` | `app/proxy/server.py:1521` | `verify_api_key` | client | `tests/unit/test_server.py::test_queue_request_status_endpoint_returns_snapshot`<br>`tests/unit/test_server.py::test_queue_request_status_endpoint_is_isolated_per_api_key` |
| 34 | proxy | DELETE | `/v1/queue/requests/{request_id}` | `app/proxy/server.py:1527` | `verify_api_key` | client | `tests/unit/test_server.py::test_cancel_queue_request_endpoint_marks_running_request` |
| 35 | proxy | GET | `/v1/{path:path}` | `app/proxy/server.py:1535` | `verify_api_key` | client | none |
| 36 | proxy | POST | `/v1/audio/speech` | `app/proxy/server.py:1647` | `verify_api_key` | client | `tests/unit/test_tts_route.py::test_success_primary_provider_and_expanded_keys` †<br>`tests/unit/test_tts_routes.py::test_cloud_route_forwards_canonical_payload` †<br>`tests/unit/test_tts_routes.py::test_cloud_route_failure_is_exact_no_local_fallback` † |
| 37 | proxy | POST | `/v1/audio/transcriptions` | `app/proxy/server.py:1653` | `verify_api_key` | client | `tests/unit/test_stt_routes.py::test_cloud_route_forwards_brand_model_id_and_language` †<br>`tests/unit/test_stt_routes.py::test_unknown_provider_returns_404_model_not_served` †<br>`tests/unit/test_stt_routes.py::test_cloud_error_body_never_reaches_client_detail` † |
| 38 | proxy | POST | `/v1/{path:path}` | `app/proxy/server.py:1659` | `verify_api_key` | client | `tests/unit/test_model_switch_verification.py::test_v1_switch_verify_fails_closed_when_backend_serves_other_model`<br>`tests/unit/test_grammar_passthrough.py::test_local_passthrough_preserves_all_gcd_fields_byte_identical`<br>`tests/unit/test_server.py::test_v1_post_forwards_cloud_model_to_provider_not_local` |
| 39 | proxy | POST | `/api/session/save` | `app/proxy/server.py:1854` | `verify_api_key` | admin | none |
| 40 | proxy | POST | `/api/session/load` | `app/proxy/server.py:1860` | `verify_api_key` | admin | none |
| 41 | proxy | GET | `/api/session/list` | `app/proxy/server.py:1866` | `verify_api_key` | admin | none |

## Dashboard surface — `app.main:app` (`127.0.0.1:11437`)

| # | Surface | Method | Path | Handler | Auth dependency | Privilege | Covering tests |
|---|---------|--------|------|---------|-----------------|-----------|----------------|
| 1 | dashboard | GET | `/openapi.json` | `fastapi/applications.py` | none | public | none |
| 2 | dashboard | GET | `/docs` | `fastapi/applications.py` | none | public | none |
| 3 | dashboard | GET | `/docs/oauth2-redirect` | `fastapi/applications.py` | none | public | none |
| 4 | dashboard | GET | `/redoc` | `fastapi/applications.py` | none | public | none |
| 5 | dashboard | MOUNT | `/static` | `app/main.py:73` | none | public | none |
| 6 | dashboard | GET | `/` | `app/main.py:75` | none | public | none |
| 7 | dashboard | GET | `/favicon.ico` | `app/main.py:79` | none | public | none |
| 8 | dashboard | GET | `/api/stats` | `app/main.py:85` | `verify_api_key` | admin | `tests/unit/test_main.py::test_get_stats_includes_api_usage` |
| 9 | dashboard | GET | `/api/benchmark` | `app/main.py:172` | `verify_api_key` | admin | none |
| 10 | dashboard | POST | `/api/benchmark/start` | `app/main.py:236` | `verify_api_key` | admin | none |
| 11 | dashboard | POST | `/api/benchmark/stop` | `app/main.py:241` | `verify_api_key` | admin | none |
| 12 | dashboard | GET | `/api/keys` | `app/main.py:249` | `verify_api_key` | admin | none |
| 13 | dashboard | POST | `/api/keys` | `app/main.py:264` | `verify_api_key` | admin | none |
| 14 | dashboard | GET | `/api/cloud/providers` | `app/main.py:280` | `verify_api_key` | admin | none |
| 15 | dashboard | GET | `/api/cloud/catalog` | `app/main.py:288` | `verify_api_key` | admin | `tests/unit/test_main.py::test_both_surfaces_serve_the_same_catalog_payload`<br>`tests/unit/test_main.py::test_dashboard_cloud_catalog_delegates_to_the_shared_implementation` |
| 16 | dashboard | POST | `/api/cloud/catalog/refresh` | `app/main.py:303` | `verify_api_key` | admin | `tests/unit/test_main.py::test_dashboard_cloud_catalog_refresh_delegates` |

## Uncovered routes

34 of the 57 routes list no test at all. Eleven of those are framework
surfaces (the four FastAPI docs routes per app, the `/static` mount, `/` and
`/favicon.ico`), leaving **23 app-owned routes with no listed test**. On top of
those, **4 app-owned routes are covered only at delegate level** (`†`): their
tests call the `app.gateway.*` implementation, never the handler, so a
route-level auth or wiring regression slips through. That makes
**27 app-owned routes without route-level coverage** — the G1 test backlog. The
guard keeps the `none` list below in sync with the `none` cells above.

- `GET` `/openapi.json` — proxy
- `GET` `/docs` — proxy
- `GET` `/docs/oauth2-redirect` — proxy
- `GET` `/redoc` — proxy
- `GET` `/healthz` — proxy
- `GET` `/api/keys` — proxy
- `POST` `/api/keys` — proxy
- `GET` `/api/cloud/ratelimit-stats` — proxy
- `GET` `/api/cloud/providers` — proxy
- `GET` `/api/cloud/models` — proxy
- `GET` `/api/crashes` — proxy
- `GET` `/api/capture/status` — proxy
- `POST` `/api/capture/rotate` — proxy
- `GET` `/api/scaler` — proxy
- `PUT` `/api/scaler` — proxy
- `POST` `/api/scaler/reset` — proxy
- `POST` `/api/scaler/recommend` — proxy
- `GET` `/v1/{path:path}` — proxy
- `POST` `/api/session/save` — proxy
- `POST` `/api/session/load` — proxy
- `GET` `/api/session/list` — proxy
- `GET` `/openapi.json` — dashboard
- `GET` `/docs` — dashboard
- `GET` `/docs/oauth2-redirect` — dashboard
- `GET` `/redoc` — dashboard
- `MOUNT` `/static` — dashboard
- `GET` `/` — dashboard
- `GET` `/favicon.ico` — dashboard
- `GET` `/api/benchmark` — dashboard
- `POST` `/api/benchmark/start` — dashboard
- `POST` `/api/benchmark/stop` — dashboard
- `GET` `/api/keys` — dashboard
- `POST` `/api/keys` — dashboard
- `GET` `/api/cloud/providers` — dashboard

## Provenance

Collected from `app.proxy.server:app` and `app.main:app` at import time
(`app.routes`, `route.methods`, `route.dependant`, `endpoint.__code__`). No
server is started, no network or model call is made. The dashboard `/static`
mount is created by `app/main.py` only when the working directory is the repo
root; the guard restores it through the app's own `_configure_static_mount`
helper so the set does not depend on the invocation directory.
