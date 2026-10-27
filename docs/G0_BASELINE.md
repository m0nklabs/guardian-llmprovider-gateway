# G0 baseline: reproducible measurements and existing-failure triage

**Issue:** [#40](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/40) (G0-03)
**Recorded:** 2026-10-27, 23:22–23:56 UTC
**Machine:** `ai-kvm2` (192.168.1.35)
**Service measured:** `guardian-llmprovider-gateway.service` (alias `llama-guardian`),
`MainPID 188579`, started `2026-10-25T14:27:01Z`
**Code measured:** `app/` and `config/` are byte-identical to `origin/main`
`1f2d813` ("docs(g0-02): trust-boundary and bypass threat model (#39)") — the
live checkout's `HEAD b30e027` is an ancestor of it and the only differences
are documentation plus `tests/unit/test_g0_endpoint_matrix.py`:

```
$ git diff --stat b30e027..origin/main -- app/ config/ tests/
 tests/unit/test_g0_endpoint_matrix.py | 481 ++++++++++++++++++++++++++++++++++++
```

Everything below is **read-only against the running deployment**: no config
change, no reload, no restart, no systemd action, no `/admin/*` call. The
single mutating call is one `DELETE /v1/queue/requests/{id}` that cancels a
request the harness itself enqueued (issue requirement, §2.4).

Numbers are only claimed where a command produced them. Where a measurement
was not taken, §5 says so and why.

---

## 1. Method

### 1.1 Machine and host state

| Fact | Value | Source |
|---|---|---|
| OS / kernel | Ubuntu 24.04.4 LTS, `6.8.0-139-generic` | `uname -a`, `/etc/os-release` |
| CPU | Intel Xeon E5-2697A v4 @ 2.60 GHz, 24 vCPU | `lscpu` |
| RAM | 108 GiB total, 69 GiB available | `free -g` |
| GPUs | RTX 3060 12288 MiB + RTX 5060 Ti 16311 MiB | `nvidia-smi` |
| Load average at start | 6.90 / 13.27 / 14.63 (1/5/15 min) | `uptime` |
| Uptime | 50 days | `uptime` |

The host is shared: at the time of measurement ~32 Docker containers, an
ffmpeg process (440 MiB) and two small CUDA processes were resident. Absolute
latency therefore reflects a busy host, not an idle benchmark rig; the
p50/p95 spread is the reproducible part.

### 1.2 Request path

Every measurement used the public entry point, so each number includes the
nginx hop:

```
client → 0.0.0.0:11434 (nginx) → 127.0.0.1:11436 (plain HTTP) → Guardian
Guardian → 127.0.0.1:11440 (llama-server)   [local]
Guardian → 127.0.0.1:4000  (LiteLLM Copilot bridge) [cloud]
```

Verified listeners at measurement time: `:11434`, `:11435` (TLS), `:11436`,
`:11437` (dashboard), `:11440` (llama-server, after the cold load), `:11441`
(caretaker), `:4000` (litellm), `:4142` (copilot-api bridge).

### 1.3 Authentication

A named key with `cloud_gateway_access: true` from
`config/guardian.keys.yaml` (selected by `--key-name`, default `oelala`) is
sent as `Authorization: Bearer …`. The key is read from the key store and is
never printed by the harness or recorded in this document.

### 1.4 Timing and percentile definitions

* **Latency** = one whole non-streaming request, measured client-side with
  `time.perf_counter()` from just before `urlopen()` until the full response
  body is read. It therefore contains DNS/loopback connect, nginx, Guardian
  queue wait, upstream inference and the JSON body transfer.
* **Percentile** = nearest-rank: `sorted(values)[ceil(p/100 × n) − 1]`. With
  `n = 20`, p50 is the 10th and p95 the 19th of 20 sorted samples. No
  interpolation, no fitting — a small sample supports exactly this.
* **tokens/s** = `usage.completion_tokens / latency` for that request. For
  very short replies this is dominated by fixed per-request overhead, which is
  why every workload is reported both at `max_tokens=32` (latency) and at
  `max_tokens=256` (throughput).
* Samples are sequential with a 200 ms gap; Guardian's own queue is the only
  source of concurrency, and it stayed empty (`X-Queue-Wait-Ms: 0` on 20/20
  local samples).

### 1.5 Harness and re-run commands

`scripts/g0_baseline_measure.py` (committed with this document) implements
every workload. It is stdlib + PyYAML, read-only, and resolves the key store
from the deployment checkout.

```bash
cd /home/flip/guardian-llmprovider-gateway          # deployment checkout (has the key store)

# discovery, 20 samples
./venv/bin/python scripts/g0_baseline_measure.py models --n 20

# local chat, latency (20) and throughput (5)
./venv/bin/python scripts/g0_baseline_measure.py chat --n 20 --model ai-kvm2-local/qwen/qwen3.8-27b-uncensored-ymq \
    --max-tokens 32 --gap-ms 200
./venv/bin/python scripts/g0_baseline_measure.py chat --n 5 --model ai-kvm2-local/qwen/qwen3.8-27b-uncensored-ymq \
    --max-tokens 256 --prompt "Write the integers from 1 to 150 in ascending order, one per line, no other text."

# one cloud route, latency (20) and throughput (5)
./venv/bin/python scripts/g0_baseline_measure.py chat --n 20 --model github-copilot/openai/gpt-6.1-sol \
    --temperature 1 --max-tokens 32 --gap-ms 200
./venv/bin/python scripts/g0_baseline_measure.py chat --n 5 --model github-copilot/openai/gpt-6.1-sol \
    --temperature 1 --max-tokens 256 --prompt "Write the integers from 1 to 150 in ascending order, one per line, no other text."

# queue depth + one cancellation (self-enqueued request only)
./venv/bin/python scripts/g0_baseline_measure.py queue
```

Running from a worktree (no key store): add
`--keys-file /home/flip/guardian-llmprovider-gateway/config/guardian.keys.yaml`.

`--temperature 1` is not cosmetic: `github-copilot/openai/gpt-6.1-sol` is
configured with `reasoning_effort: high` and rejects `temperature=0` with

```
400 litellm.UnsupportedParamsError: gpt-6.1-sol doesn't support temperature=0
while reasoning is active. Only temperature=1 is supported unless
reasoning_effort resolves to 'none' …
```

That is a client-visible compatibility fact worth keeping (a client sending
the common `temperature: 0` is refused, not downgraded).

---

## 2. Measurements

All samples were taken 2026-10-27 between 23:26 and 23:34 UTC.

| # | Workload | Endpoint / model | n | p50 | p95 | min–max | tokens/s |
|---|---|---|---|---|---|---|---|
| M1 | Discovery | `GET /v1/models` | 20 | **260.10 ms** | **351.53 ms** | 251.67–358.51 ms | — (274 ids) |
| M2 | Local chat, latency | `POST /v1/chat/completions`, `ai-kvm2-local/qwen/qwen3.8-27b-uncensored-ymq`, `max_tokens=32` | 20 | **1284.97 ms** | **1356.14 ms** | 1257.35–1360.68 ms | 24.78 |
| M3 | Local cold start | same, first request from idle-unloaded state | 1 | — | — | **55 856.53 ms** | 0.57 |
| M4 | Local chat, throughput | same, `max_tokens=256` | 5 | 5278.02 ms | 6059.41 ms | 5269.78–6059.41 ms | **33.03** (181 tok) |
| M5 | Cloud chat, latency | `github-copilot/openai/gpt-6.1-sol`, `temperature=1`, `max_tokens=32` | 20 | **1467.37 ms** | **1841.59 ms** | 1355.61–1848.57 ms | 3.94 (6 tok) |
| M6 | Cloud chat, throughput | same, `max_tokens=256` | 5 | 3530.52 ms | 3702.65 ms | 3268.24–3702.65 ms | **72.85** (256 tok) |

All 71 chat/discovery samples returned HTTP 200. No sample was retried or
dropped; the reported `n` is the number of attempts.

### 2.1 M1 — `/v1/models` discovery

274 model ids, `260 ms` p50 for the aggregated catalog (two local providers plus
seven cloud catalogs). The first call of the session (a 1-sample smoke test at
23:22:28) took 452 ms, i.e. catalogue aggregation is mildly cache-warm; the
20-sample series above starts after that warm-up. This is the endpoint a cold
client pays before it can address anything, so 0.26 s is the floor for
"resolve a model name".

### 2.2 M2/M3 — local provider

The model was **not resident** at the start: `llama-server` was not running,
`:11440` was closed, and both GPUs showed ~0.5 GiB used. The first request
therefore paid a cold load: **55.9 s** end-to-end for the `ensure` + load +
first 32 tokens. After that the model stayed resident and the steady-state
series is M2: **1285 ms p50 / 1356 ms p95** with a 131 ms spread, i.e. very
stable for a 27B model on a busy box.

The idle-unload setting that produced the cold state is
`config/providers/ai-kvm2-local.settings.yaml:87` → `guardian.idle_unload_minutes: 5`;
the model self-unloads 5 minutes after the last request (observed again by the
second queue probe at ~23:52, which found `llama-server` gone and therefore
paid a fresh cold load).

Generation throughput (M4, 181 completion tokens) is **33 tok/s**. Note the
model emits `reasoning_content`; `completion_tokens` counts those tokens, so
throughput is measured fairly but a small `max_tokens` is consumed by
reasoning rather than visible text.

Residency at measurement time:

| Item | Value | Source |
|---|---|---|
| Model file | `/home/flip/models/qwen3.8-27b-uncensored-ymq/Qwen3.8-27B-Uncensored-YMQ-XL.gguf` | `/props` |
| Build | `b12147-1837d005f` | `/props` |
| Context | `-c 145000` (per-slot `n_ctx 145152`) | process args, `/props` |
| Slots | `total_slots: 4` | `/props` |
| KV cache | `-ctk turbo4 -ctv turbo4`, `--spec-type draft-mtp` | process args |
| Tensor split | `0.43,0.57` | process args |
| VRAM resident | 11536 / 12288 MiB (RTX 3060), 15551 / 16311 MiB (RTX 5060 Ti) | `nvidia-smi` |

### 2.3 M5/M6 — one cloud route

`github-copilot/openai/gpt-6.1-sol` through the local LiteLLM bridge:
**1467 ms p50 / 1842 ms p95** for a 32-token cap that produced 6 visible
tokens (reasoning consumes most of the budget), and **72.85 tok/s** at
`max_tokens=256` (256 tokens every time, `finish_reason: length`).

**Provider-side variance observed during the same window** (recorded here as
an environmental fact, not as a Guardian defect):

| Model | Result |
|---|---|
| `google/google/gemini-2.5-flash` | upstream `404`, "This model … is no longer available to new users" |
| `google/google/gemini-flash-latest` | upstream `503` "currently experiencing high demand" after **32.04 s** |
| `google/google/gemini-3.8-flash` | no response within **180 s**; client aborted |
| `github-copilot/openai/gpt-6.1-sol` | 20/20 `200` |

So the cloud baseline is route-specific by necessity: during this window the
`google` route could not produce a single baseline sample. A future re-run must
re-check both routes and record which one answered — a "cloud p95" without the
route and model is not a baseline.

Guardian's forwarding itself is not implicated: the failures were upstream
status codes and upstream stalls passed through unchanged. The `google`
provider is configured with `timeout_seconds: 1200`, so a stalled upstream is
waited out for up to 20 minutes rather than cut short (§2.5) — a client that
expects a fast failure will not get one.

### 2.4 Queue depth and cancellation (`DELETE /v1/queue/requests/{id}`)

Probe: request **A** (local, `max_tokens=256`, ~7 s) takes the single slot;
request **B** (local, `max_tokens=32`) from the same key queues behind it; B
is cancelled while still queued.

`GET /v1/queue/status` before the probe:

```json
{"queue_length": 0, "active_count": 0, "max_concurrent": 1,
 "queue_timeout_s": 300, "queue_timeout_enforced": false,
 "wait_policy": "disconnect_or_cancel",
 "stats": {"total_queued": 208, "total_completed": 205, "total_cancelled": 3,
           "total_timeouts": 0, "total_failed": 0, "total_expired": 0}}
```

Observed sequence:

| Step | Observation |
|---|---|
| A running | `active_count: 1`, `queue_length: 0` |
| B submitted | `active_count: 1`, `queue_length: 1`, B `position: 1`, `status: queued` → **max observed depth = 1** |
| `DELETE /v1/queue/requests/f1c377ce-8ce0-415d-9e6b-edc9df909be4` | `200` with `status: "cancelled"`, `cancel_reason: "client_requested_cancel"`, `position: -1` |
| B's client | `HTTP 499` in **61.99 ms**: `{"detail":{"error":"request_cancelled","request_id":"f1c377ce-…","message":"client_requested_cancel"}}` |
| A's client | `HTTP 200` in **6920.4 ms** (256 tokens → 37.0 tok/s), `X-Queue-Wait-Ms: 0` — warm model; the same probe on a cold (idle-unloaded) backend takes ~23 s |
| After | `active_count: 0`, `queue_length: 0`, cancelled counter +1 |

**Observed effect:** cancelling a *queued* request frees its place immediately
(the queue drops back to 0 before A finishes), the slot-holder is unaffected,
and the cancelled client receives a typed `499 request_cancelled` instead of
a hang. The `DELETE` is idempotent-looking from the caller's side (200 +
snapshot) rather than a 404 once the entry is final.

Two behaviours worth recording as baseline facts:

* `queue_timeout_s: 300` is reported but **`queue_timeout_enforced: false`** —
  a queued request with a live connection can wait longer than 300 s; the
  queue is released by disconnect or explicit cancel
  (`wait_policy: "disconnect_or_cancel"`).
* The same API key cannot occupy two slots: a second request from the same
  key queues even when `max_concurrent` would allow more
  (`_owner_has_active_request`, `app/proxy/queue.py:181-190,249-257`).

### 2.5 Capacity statement

| Bound | Config key → live value | What was measured |
|---|---|---|
| Local concurrency | `queue.max_concurrent` → **1** (`config/global.settings.yaml:41-44`) | 1 active + 1 waiting observed; a second local request never overlaps the first |
| Queue wait ceiling | `queue.queue_timeout_seconds` → **300**, but `queue_timeout_enforced: false` (live status) | No enforced ceiling observed; release is disconnect-or-cancel |
| Queue history | `queue.history_ttl` → 300 s | 205 completed / 3 cancelled retained at probe time |
| Model residency | `config/providers/ai-kvm2-local.settings.yaml:87` `idle_unload_minutes: 5` | Model absent when idle; cold load **55.9 s**; resident while used |
| Backend slots | llama-server `total_slots: 4`, `-c 145000` | Guardian serialises to 1 under load, so 3 of 4 backend slots are unreachable through Guardian |
| VRAM headroom | — | 752 MiB free on the 12 GiB card, 760 MiB on the 16 GiB card while resident |
| Local generation | — | 33.03 tok/s (256-token replies), 24.78 tok/s end-to-end at 32 tokens |
| Cloud concurrency | no Guardian-side bound (cloud requests never enter the inference queue: status stayed `0/0` during the cloud series) | 20 sequential samples, all 200; limit is provider-side |
| Timeouts | cloud providers: `timeout_seconds: 1200` each (`github-copilot`, `google`, `groq`, `nvidia`, `openai`, `openrouter`, `poolside`); local by model-size tier: 360 s (≥5 GB) / 600 s (≥10 GB, and small) / 1200 s (≥20 GB) / 3600 s (≥40 GB), `default_timeout: 600`; `fish.audio`: 300 | A stalled upstream is waited on for up to 20 min (see the >180 s Gemini stall in §2.3) |
| Remote ensure | `failover.remote_ensure_timeout_seconds` → **420** in the *live* config file (origin/main commits 120) | not exercised; recorded because the live value is not the committed one |

**Headline capacity:** this deployment serves **one local inference request at
a time** at ~33 tok/s, with a ~1.3 s p50 for short replies once the model is
resident, and a 56 s first-request penalty after every 5 idle minutes. The
binding constraint is Guardian's `max_concurrent: 1`, not the backend (4
slots) and not the GPU (both cards are ~95 % full at that model/context, so a
second *concurrent* model would not fit either).

---

## 3. Existing-failure triage

### 3.1 The flake: `test_manual_rotate_opens_new_active` — root cause and fix

**Symptom (PR #39, `python-ci`):**

```
tests/unit/test_capture_wal_writer.py::TestWALWriterRotation::test_manual_rotate_opens_new_active
assert '"seq":100' in active_content  →  got "seq":0
```

It passed on rerun, and 12/12 locally — a timing bet, not a logic bug in the
writer. While verifying the fix this session found a **second test with the
identical race**, `TestWALWriterCrashRecovery::test_state_persisted_across_restart`,
which failed on an *unmodified* `origin/main` worktree (see §3.3). Both are
fixed here, because one red-then-green gate is enough to teach reviewers to
re-run instead of read.

**Root cause.** The writer drains the sink on its own task, and each record
costs an `asyncio.to_thread` hop plus a real `fsync`; the *first* iteration
additionally pays the thread-pool warm-up and the startup retention sweep
(`app/capture/wal_writer.py:706-735`; write path `:669-698`, `fsync` at
`:693`). Both tests pushed events, slept a fixed 100–200 ms, then called
`writer.rotate()`, which short-circuits unless the writer has *accounted*
bytes:

```python
def rotate(self) -> str | None:
    if self._active_file is None or self._active_file_size == 0:
        return None                      # app/capture/wal_writer.py:450-460
```

On a loaded runner the batch had not reached the writer's accounting yet, so

1. `rotate()` returned `None` and **did not open a new active file**;
2. the post-rotation batch therefore landed in the *same* file, behind the
   still-draining earlier batch;
3. the assertion ran while the file held `"seq":0` (and friends) but not
   `"seq":100` — exactly the CI message. For the crash-recovery test the same
   window made `assert path is not None` fail instead.

**Evidence — deterministic reproduction.** Replaying the pre-fix test body
with a single 300 ms delay injected into the writer's first
`_write_event_sync` call (standing in for "the writer task had not been
scheduled yet"):

```
rotate() -> None   (None == 'nothing to rotate yet')
assertion '"seq":100' in active -> False
active content: '{"seq":0,…}\n{"seq":1,…}\n{"seq":2,…}\n…'
```

The same injection against the fixed test body passes
(`rotate() -> guardian_capture_1793144060_1.jsonl.gz`, assertion `True`). The
injection snippet is in Appendix A; it needs no repository change.

**A detail that mattered.** The first attempt waited for the *record to be
visible in the file* — and still failed the crash-recovery test. Reason: the
bytes become readable on `flush()`, while `_active_file_size` is incremented
*after* `fsync()`, so a content-based predicate can still call `rotate()` in
that window and get `None`. The predicate that is actually correct is the one
`rotate()` itself uses — writer accounting **and** content:

```python
writer._active_file_size > 0 and _active_contains(active, needle)
```

**Fix (test-only, deterministic).** `tests/unit/test_capture_wal_writer.py`
gains three small helpers and no longer bets on sleeps:

* `_wait_until(predicate, *, message, timeout=10.0, interval=0.01)` — polls
  until true, raises `AssertionError` with the message after the deadline, so a
  genuine regression still fails (10 s later) instead of relying on luck;
* `_active_contains(path, needle)` — reads the active WAL through the existing
  crash-tolerant reader, a missing file means "not yet written";
* `_wait_for_records(writer, active, needle, *, message)` — the combined
  accounting-plus-content wait described above.

Applied to every test that rotates after writing: `test_manual_rotate_opens_new_active`,
`test_manual_rotate_returns_path`, `test_rotated_sidecar_matches_final_gz_bytes`,
`test_state_persisted_across_restart` (both rotations, including after the
restart) and `test_legacy_active_seq_persisted`. No assertion was weakened, no
sleep was lengthened, and `app/capture/wal_writer.py` is untouched.

**Proof — 20 consecutive runs each, all green:**

```bash
cd <worktree>
for i in $(seq 1 20); do
  ./venv/bin/python -m pytest -q \
    tests/unit/test_capture_wal_writer.py::TestWALWriterRotation::test_manual_rotate_opens_new_active \
    && echo "run $i: PASS" || echo "run $i: FAIL"
done
# and the same loop for
# tests/unit/test_capture_wal_writer.py::TestWALWriterCrashRecovery::test_state_persisted_across_restart
```

Result: `run 1: PASS` … `run 20: PASS` for **both** tests (20/20 + 20/20), and
the file's 54 tests pass in one run.

### 3.2 Threat-model §9 `unverified:` items, re-checked

Item numbers follow `docs/G0_TRUST_BOUNDARY_THREAT_MODEL.md` §9.

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | Firewall/ACL between `ai-kvm-2` and `192.168.1.245` | **partially verified** | No host firewall: `ufw` reports `inactive`; `iptables -S` shows `INPUT ACCEPT`, `OUTPUT ACCEPT`, `FORWARD DROP` plus only Docker's own chains; no `nft` filter rules of our own. The hop itself is open per port: TCP `:11441` **open**, `:11450` **open**, `:11440` **closed/filtered**, `:11451` **closed/filtered**; ICMP 0 % loss, 0.48/0.50/0.53 ms RTT. So the filtering that exists is per-service on the Windows side, not a network ACL — but router/switch ACLs for *other* hops remain unobservable from this host. |
| 2 | Whether any container reaches `:11440` | **verified — yes** | `:11440` binds `127.0.0.1` only, and 2 of 32 running containers use `NetworkMode=host`: `glp_windmill_worker`, `glp_windmill_server`. From inside both, `curl http://127.0.0.1:11440/health` returned **HTTP 200**. Loopback binding does not isolate Guardian's backend from host-netns containers; this is now a demonstrated bypass path, not a hypothesis. |
| 3 | Which path produced the 305 world-readable media files | **verified (mechanism + timeline), residual attribution open** | `data/capture/media`: 5359 files — **5054 are `0600`, 305 are `0644`**. All 305 have mtimes `2026-08-26` (1), `2026-08-30` (294) and `2026-09-01 21:11–22:10` (10); **zero** `0644` files exist after `2026-09-02` (4610 newer files are all `0600`). The `UMask=0077` drop-in (`…service.d/20-tls.conf`) is dated `2026-08-31T23:36:20Z` and the unit file `2026-09-01T20:53:02Z`. The writer sets no mode (`app/capture/media.py:67-74`; the actual `target.write_bytes(raw)` is `:73`), so the file inherits the service umask — verified. The 10 post-restart `0644` files cannot have come from a `UMask=0077` unit; they were written by a process with the default `0022` umask (an ad-hoc in-service run), which is **inferred, not proven**. Mitigating fact: `data/capture` itself is `0750`, so those `0644` files are not traversable by other Unix users today. |
| 4 | `windows-gpu-local` / `192.168.1.W` placeholder | **verified — inert, and a duplicate of the live LAN provider** | `192.168.1.W` is neither a valid IPv4 literal nor resolvable: `getent hosts` → nothing, `socket.gethostbyname` → `gaierror -2`, no `/etc/hosts` entry. A request to `windows-gpu-local/windows/qwen3.5-9b` returns `503 {"detail":"Failover candidate 'windows/qwen3.5-9b' failed to load: Caretaker unreachable at http://192.168.1.W:11441"}`; the provider contributes **no ids** to `/v1/models`. The real LAN bypass already exists as its sibling `14700k-local`, whose four hops point at the actual IP `192.168.1.245` (`config/providers/14700k-local.settings.yaml:11,21,24,26`). So it is not a *second* bypass — it is dead config that duplicates B-03 under an unresolvable name. |
| 5 | Authenticated behaviour of the speech endpoints on `192.168.1.245` | **still unknown** (new evidence, no POST issued) | `:11450` answers **unauthenticated** `GET /health` → `200 {"status":"ok","model":"model-design"}` (an information leak: engine and model name without a key), while `GET /`, `/tts/ensure`, `/audio/speech` → `404 {"error":"not found"}` — the documented paths are not routed on that engine, unauthenticated or not. `:11451` (STT) is closed. The authenticated path needs a POST, which this task forbids; therefore the item stays open with the above narrowing. |
| 6 | `[judgement]` "reachable if this hop is compromised" statements | **unchanged judgement** | Still reasoned from configuration; nothing here was turned into an observed attack. |
| 7 | `[judgement]` severity labels in §3/§4 | **unchanged judgement** | Not a measurement target. |
| 8 | Behaviour of the public API under a LAN MITM | **still unknown** | No capture of a real client's key on the wire was attempted. Additional fact confirmed while measuring: all 71 samples used **cleartext** `http://…:11434` and were served normally, so the cleartext entry is not merely accepted but is the normal path for LAN clients. |
| 9 | Caretaker-repository claims (`_yaml_vram_limit_mb` / `OverflowError`, watchdog, OOM reporting) | **verified** (the repo is on this host now) | `/home/flip/caretaker-llamacpp` @ `6561f03`, remote `m0nklabs/caretaker-llamacpp`. `_yaml_vram_limit_mb()` is at `caretaker/manager.py:421-448` and does `int(raw)` inside `except (TypeError, ValueError)`; `int(float('inf'))` raises `OverflowError`, which is **not** a `TypeError`/`ValueError` subclass (reproduced: `OverflowError: cannot convert float infinity to integer`, `isinstance(OverflowError(), (TypeError, ValueError)) == False`). Caretaker issue #17 is open and describes exactly this; guardian #34 is open for the crash/OOM blindness. The "watchdog never started" / "OOM reporting undecided" halves are still quoted from issue text, not observed. |
| 10 | Exact route count behind the 45 `Depends(verify_api_key)` lines | **verified — reconciled exactly** | Matrix: **57** rows (`41 proxy + 16 dashboard`), of which **13** have `auth = none` and **44** have `verify_api_key`. Code: **45** `Depends(verify_api_key)` sites (9 `app/main.py` + 36 `app/proxy/server.py`). The one-site delta is `get_version` (`app/proxy/server.py:1187`): an `async def` with an auth dependency but **no route decorator and no other registration** — dead code, not a route. Every 44 rows map 1:1 onto a decorated route (checked programmatically by method+path). Guard tests pass: `tests/unit/test_g0_endpoint_matrix.py` → **8 passed**. |

Two side findings from the same pass, recorded but **not** fixed here (they
belong to G1/G0-04, and this task changed no production code):

* `get_version` (unrouted `GET`-style handler for Ollama's `/version`) is dead
  code carrying an auth dependency; either register it or delete it.
* `windows-gpu-local.settings.yaml` should be deleted or pointed at
  `192.168.1.245`; as committed it is dead weight that reads like a live
  provider.

### 3.3 Test failures observed during this run, and attribution

| # | Failure | Where observed | Attribution | Status |
|---|---|---|---|---|
| F1 | `test_capture_wal_writer.py::TestWALWriterRotation::test_manual_rotate_opens_new_active` | `python-ci` on PR #39 (external; quoted in issue #40) | **pre-existing** latent race, not caused by any change in this PR | **fixed** (test-only, §3.1) |
| F2 | `test_capture_wal_writer.py::TestWALWriterCrashRecovery::test_state_persisted_across_restart` | this session, 23:47 UTC, running the suite on a **pristine `origin/main`** worktree while a queue/cancellation probe was cold-loading the 27B model from disk on the same host | **pre-existing** latent race, same class as F1. *Not* caused by this PR — the failing tree contained none of this PR's changes. The load that exposed it came from this session's own measurement. Note: 12 busy CPU loops alone did **not** reproduce it (see the table), so the window is dominated by **I/O latency** (`fsync`), not CPU starvation | **fixed** (same helpers, §3.1) |
| — | any other failing test | full suite, all four runs below | — | **none observed** |

The failing assertion for F2, on the unmodified tree:

```
    result = writer2.rotate()
>   assert result is not None
E   assert None is not None
tests/unit/test_capture_wal_writer.py:895: AssertionError
```

Full suite, same interpreter and the same `pytest` configuration as the
restart gate (integration/live markers deselected via `addopts` in
`pyproject.toml`):

```bash
cd <worktree> && ./venv/bin/python -m pytest tests/ -q
```

| Tree | Conditions | Result |
|---|---|---|
| `origin/main` @ `1f2d813` (pristine worktree) | **quiet** | 1863 passed, 20 deselected, **0 failed** (51.81 s) |
| `origin/main` @ `1f2d813` (pristine worktree) | **heavy I/O** (concurrent 27B cold-load + queue probe) | 1862 passed, 20 deselected, **1 failed** — F2 |
| `origin/main` @ `1f2d813` (pristine worktree) | 12 busy CPU loops | 1863 passed, 20 deselected, **0 failed** (49.16 s) — F2 did *not* reproduce, i.e. the window is `fsync` latency, not CPU starvation |
| `docs/g0-03-baseline-40` (this PR) | quiet | **1863 passed, 20 deselected, 0 failed** (50.24 s) |
| `docs/g0-03-baseline-40` (this PR) | 12 busy CPU loops | **1863 passed, 20 deselected, 0 failed** (48.87 s) |

Every run collected the same 1883 tests (1863 selected + 20 deselected), so the
merged-head (`origin/main` `1f2d813`) count is **1863 passed / 20 deselected /
0 failed — while the machine is quiet**. Under I/O-heavy load the unfixed tree
loses a test to F2; the fixed tree did not lose one in any condition tried.
That difference is the point of this triage: the suite was not reliably green,
it was green when the box was idle.

The 20 deselected are the `integration` / `finetune_v2_live` markers — the
tests that need a live backend. They are not part of the green gate and are not
counted as passing.

Flake loops after the fix: **20/20 PASS** (named test) and **20/20 PASS**
(crash-recovery test), §3.1.

## 4. What this baseline does and does not say

* It is a **single-host, single-model, single-window** baseline. p95 of 20
  samples is a coarse bound; a 1-in-20 tail event is not characterised.
* Local numbers are for `qwen3.8-27b-uncensored-ymq` with the production launch
  configuration (`-c 145000`, `turbo4` KV, 2-GPU tensor split). Any other model
  is a different baseline.
* Cloud numbers are for `github-copilot/openai/gpt-6.1-sol` with
  `reasoning_effort: high`. The `google` route produced no usable sample
  during the window; re-run before quoting a cloud figure.
* "Capacity" here is *what was observed within policy*: one concurrent local
  request. This document deliberately does **not** include a saturation or
  sustained-load test (§5).

---

## 5. Measurements that were not taken, and why

| Not measured | Reason |
|---|---|
| **Speech (TTS/STT) request through Guardian** | No local speech backend is up for `ai-kvm2-local` (`127.0.0.1:11450` has no listener). The only reachable speech host, `192.168.1.245:11450`, serves `/health` and `404`s the documented paths without credentials; issuing a real synthesis would be a POST to a live daemon, which the issue (and the G0-02 threat model) forbids. Blocked → owner needs an explicit "POST the speech daemon once" decision. |
| **STT** | `192.168.1.245:11451` is closed/filtered and nothing listens locally; no backend to call. |
| **p95 under concurrency > 1, saturation, sustained load** | Would require generating load against the production service. The issue explicitly says not to; only the minimal observation (one queued second request, §2.4) was made. |
| **A second local model (swap comparison)** | Requires swapping the model on the live daemon — forbidden. The model that was selected (`config/current_model.args`) is the one measured. |
| **Cold-load latency for other models** | Same reason: each cold load is a model swap on the GPU. |
| **Router/switch ACL behaviour between LAN hops** | Not observable from this host; only endpoint reachability per port was measured (§3.2 item 1). |
| **LAN MITM / key capture on `:11434`** | Requires intercepting a real client's traffic; not performed and not authorised here. |
| **Caretaker watchdog / OOM-reporting runtime behaviour** | Reproducing an OOM-kill of the production backend is out of scope; the code-level claim was verified instead (§3.2 item 9). |

---

## 6. Follow-ups created by this record (not fixed here)

1. `windows-gpu-local` is dead config duplicating the live `14700k-local`
   provider — delete or correct (§3.2 item 4).
2. `get_version` is an unrouted handler carrying an auth dependency — register
   or delete (§3.2 item 10).
3. `queue_timeout_seconds: 300` is reported as *not enforced*; either enforce
   it or stop advertising it (§2.4).
4. Three of the backend's four llama-server slots are unreachable through
   Guardian while `max_concurrent: 1`; that is a capacity decision for G1/G3,
   recorded here, not changed.
5. The ten `0644` capture files created after the `UMask=0077` drop-in were
   written by a non-service process; if a documented ad-hoc replay path exists,
   it should set the mode explicitly (§3.2 item 3).
6. `google` route: no baseline sample possible during this window (retired
   model, 503, and a >180 s stall). Re-measure before relying on a cloud p95.

---

## Appendix A — deterministic flake reproduction (no repository change)

```python
# Replays the PRE-FIX test body with a 300 ms delay on the writer's first record.
# Expect: rotate() -> None and an active file containing "seq":0 but not "seq":100.
import asyncio, time, shutil
from pathlib import Path
from app.capture.config import CaptureConfig
from app.capture.gzip_reader import read_all_text
from app.capture.sink import CaptureEvent, CaptureSink
from app.capture.wal_writer import ACTIVE_FILENAME, CaptureWALWriter

ROOT = Path("/tmp/g0-flake-repro")
ACTIVE = ROOT / ACTIVE_FILENAME

async def main():
    shutil.rmtree(ROOT, ignore_errors=True); ROOT.mkdir(parents=True)
    config = CaptureConfig(enabled=True, local_capture=True, cloud_capture=False,
        instance_id="repro", policy_version="1.0.0", capture_root=str(ROOT),
        max_file_bytes=1 << 20, max_file_age_seconds=3600, retention_days=7,
        max_pending_events=100, file_mode=0o640, directory_mode=0o750)
    sink = CaptureSink(max_pending_events=config.max_pending_events)
    writer = CaptureWALWriter(sink, config)

    original, first = writer._write_event_sync, {"done": False}
    def slow(event):
        if not first["done"]:
            first["done"] = True
            time.sleep(0.300)          # CI: writer task not yet scheduled
        return original(event)
    writer._write_event_sync = slow

    await writer.start()
    for i in range(10):
        sink.try_put(CaptureEvent(data={"seq": i, "data": "x" * 50}))
    await asyncio.sleep(0.2)           # pre-fix wait #1
    print("rotate() ->", writer.rotate())      # -> None: nothing written yet
    for i in range(5):
        sink.try_put(CaptureEvent(data={"seq": 100 + i, "data": "y" * 50}))
    await asyncio.sleep(0.2)           # pre-fix wait #2
    content = read_all_text(ACTIVE)
    print('assert \"seq\":100 ->', '"seq":100' in content)
    await writer.stop()

asyncio.run(main())
```

Run it with the repository venv and `PYTHONPATH` pointing at the checkout.
