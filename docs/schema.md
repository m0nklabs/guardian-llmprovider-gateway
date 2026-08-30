# Capture Record Schema — `guardian_capture_v1`

> Reference for consumers of Guardian's capture WAL (`data/capture/`): field
> semantics, on-disk layout, and the known pitfalls that motivated the
> 2026-08-30 capture-feedback round (items C1–C11). For ad-hoc querying use
> `scripts/capture_query.py` instead of writing throwaway scanners.

## Versioning

- `schema_name` (`guardian_capture_v1`) is the wire contract; readers must
  reject unknown names.
- `schema_version` bumps **additively** within the contract: fields are only
  ever added (or made more reliably present), never removed or renamed.
  Current version: **1.1.0** (2026-08-30). 1.0.0-era records remain readable —
  every 1.1.0 field is optional per record.

## On-disk layout

| File | Meaning |
|---|---|
| `guardian_capture_current.jsonl` | **Active** file — plain JSONL, append + fsync per record. Safe to `tail -f` / stream with `jq` while the gateway writes it. |
| `guardian_capture_<epoch>_<seq>.jsonl.gz` | Rotated files — gzip-compressed on rotation (rotation by uncompressed size `max_file_bytes` or age `max_file_age_seconds`). |
| `guardian_capture_<epoch>_<seq>.jsonl.sha256` | SHA-256 sidecar over the final `.gz` bytes (integrity check for offline consumers). |
| `.capture_state.json` | Writer-internal rotation state. Ignore. |
| `media/` | Extracted binary image payloads, referenced (never embedded) from events. |

Legacy compatibility (pre-1.1.0 writer): the active file used to be a live
gzip stream (`guardian_capture_current.jsonl.gz`). On startup the current
writer renames such a file to a completed-style name as-is — it is never
appended to. Readers of legacy active files must tolerate a truncated final
gzip member (`EOFError` mid-stream); plain active files have no such hazard.

Retention is **infinite by operator decision** (2026-08-26): `retention_days: -1`,
`max_capture_bytes: -1` — cleanup is an offline/Keanu concern, not a gateway
timer. `guardianctl status` shows the configured retention and the
oldest/newest rotated file.

## Event types

One inference request produces a small event sequence sharing one `request_id`:

| `event_type` | When |
|---|---|
| `request_received` | After auth + normalization, before routing (sequence 0). Carries the raw request messages and `request_parameters`. |
| `request_completed` | After a successful response (streaming or not). |
| `request_failed` | On a sanitized error path. |
| `request_cancelled` | On client disconnect/timeout. |

## Base fields (every event)

| Field | Type | Notes |
|---|---|---|
| `schema_name` / `schema_version` | str | Wire contract + additive version. |
| `event_id` | hex | SHA-256 of `{instance_id}|{request_id}|{event_type}|{sequence}`. |
| `event_type`, `sequence`, `request_id` | — | Identity/lifecycle. |
| `timestamp_utc` | ISO-8601 (ms, `Z`) | **Event-build time**: receipt time on `request_received`, completion time on terminal events. Kept for compatibility. |
| `started_at_utc` | ISO-8601 (ms, `Z`) | **1.1.0 (C1)** — wall-clock UTC when capture began tracking the request (≈ request receipt). On `request_received` it equals `timestamp_utc`. |
| `completed_at_utc` | ISO-8601 (ms, `Z`) | **1.1.0 (C1)** — present on terminal events; equals their `timestamp_utc`. Compute request duration as `completed_at_utc - started_at_utc`. |
| `guardian_instance_id`, `client_ref`, `endpoint`, `ingress_protocol`, `route_type`, `requested_model`, `resolved_model`, `capture_policy_version` | — | Context; `client_ref` is an HMAC of the key fingerprint (no keys in records). |
| `caller_request_id` | str ≤256 | **1.1.0 (C6)** — caller-supplied correlation id echoed from the configured inbound headers (`capture.correlation_headers`, default `["x-request-id"]`). Absent when the client did not send one. |
| `app_title` / `app_referer` | str ≤256 | **1.1.0 (C5)** — the client's `x-title` / `http-referer` headers when present (OpenRouter-style app attribution). Never fabricated. |
| `upstream_model`, `provider`, `failover_group` | — | Routing metadata; see provider timing below. |
| `record_auth` | obj | Per-record HMAC-SHA256 (`{alg, key_id, mac}`) when `GUARDIAN_CAPTURE_RECORD_AUTH_SECRET` is set. MAC is computed over the line without the `record_auth` field. |

## `request_received` extras

- `request_messages` — raw messages (media extracted to files).
- `request_parameters` — the **full normalized parameter set** the client
  sent: `stream`, `stream_options`, `temperature`, `max_tokens` /
  `max_completion_tokens`, `tools`, `reasoning`, `stop`, `store`, …
  (**C7 note:** this field already contained the requested parameters; the
  external feedback that "requested parameters are not captured" came from
  scanning `request_completed` events only. Join on `request_id` to get them.)
- `queue_wait_ms`, `grammar_present`, `response_format_present`.

## `request_completed` extras

| Field | Type | Notes |
|---|---|---|
| `response_content` | str | Final assembled content (null when the model returned none). |
| `reasoning_content` | str | Reasoning channel, stored separately from content. |
| `tool_calls` / `tool_results` | list | As reported. |
| `finish_reason` | str \| **null** | **Always present since 1.1.0 (C4)** — `stop` / `length` / `tool_calls` / …; explicit `null` means "upstream did not report one". Before 1.1.0 the key was omitted when unknown — the historical reason truncated calls looked like "finish_reason not captured". |
| `native_finish_reason` | str | **1.1.0 (C4)** — provider-native stop reason (e.g. OpenRouter), when reported. |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | int | Int-coerced defensively (C2 — see pitfalls). |
| `completion_tokens_details` | obj | **1.1.0 (C5)** — upstream usage details **as-is** (OpenAI/OpenRouter shape; contains `reasoning_tokens` — the exact reasoning token count). |
| `native_tokens_reasoning` / `native_tokens_cached` | int | **1.1.0 (C5)** — upstream native token accounting when reported. |
| `cost` | float | **1.1.0 (C5)** — upstream-reported cost when available. |
| `provider_name` | str | **1.1.0 (C5)** — the actual serving backend from the upstream response (e.g. `Baidu`, `Z.AI`), distinct from the configured `provider` (e.g. `openrouter`). |
| `http_status`, `duration_ms`, `attempts` | — | `attempts` is the winning 1-based provider attempt on cloud routes. |
| `streamed` | bool | Compatibility flag — **the ingress leg** (client requested streaming). |
| `streamed_ingress` / `streamed_upstream` | bool | **1.1.0 (C8)** — explicit per-leg flags. |

`request_failed` adds `error_code` / `sanitized_message`; `request_cancelled`
adds `cancel_reason`. Terminal events also carry the 1.1.0 timing/correlation
fields.

## Provider field timing (C11)

`provider` is the *configured* cloud provider (e.g. `openrouter`). It is
resolved during forwarding, so:

- `request_received` (cloud): `provider` is `null` — nothing is resolved yet.
- `request_completed` / `request_failed` / `request_cancelled` (cloud):
  `provider` is populated per (winning) attempt since 1.1.0.

## Known pitfalls for consumers

1. **Completion-time timestamps.** Legacy 1.0.0 records only have
   `timestamp_utc`, which is the *completion* time on terminal events.
   External dashboards showing "request start" will disagree with the capture
   by exactly the request duration (a 39.7-min call lands 39.7 min earlier on
   a start-based dashboard). Use `started_at_utc` on 1.1.0 records.
2. **Int counters.** Token fields are ints in current and historical records
   (verified over the full production corpus — the reported `131072.0` float
   existed only *inside message content*, not as a field value). Filter on
   numeric values, not substrings; `capture_query.py --min-completion N`
   already does the right thing.
3. **Legacy truncated gzip.** Pre-1.1.0 active files are gzip streams that
   may lack a final member — read tolerantly (stop at `EOFError`, keep
   decoded lines) or use `scripts/capture_query.py` / `app.capture.gzip_reader`
   which handle plain and gzip layouts transparently.
4. **Epoch filenames.** Rotated file names use epoch seconds — do not parse
   them as record timestamps; sort by mtime.
5. **Missing ≠ null.** Optional fields are *omitted* when unknown
   (except `finish_reason` on completed events, which is explicitly `null`
   since 1.1.0).

## Querying

```bash
# All records for one client in a time window (tolerates every pitfall above)
./venv/bin/python scripts/capture_query.py --client a1adffde --since 2026-08-30T00:00:00Z

# Wasted-output scan: completions that produced tokens but no content
./venv/bin/python scripts/capture_query.py --empty-content-only --min-completion 1000

# Daily per-client/per-model rollup (calls, tokens, empty-content, cost)
./venv/bin/python scripts/capture_query.py --rollup daily --since 2026-08-28
```

Operational CLI: `scripts/guardianctl.py` (`status`, `files`, `rotate`,
`export`, …) shows capture config, retention, and WAL health.

## Related docs

- `docs/GUARDIAN_KEANU_CAPTURE_PLAN.json` — original capture contract.
- `docs/API_REFERENCE.md` — capture admin endpoints (`/api/capture/status`, `/api/capture/rotate`).
- `docs/ARCHITECTURE.md` — capture subsystem position in the request lifecycle.
