# Operator Runbook — Llama-CPP Guardian

> Detail skill for deployment, operations, and troubleshooting.
> Referenced from `AGENTS.md`.

## Service lifecycle

```bash
# Start / stop / restart the Guardian proxy (port :11434)
sudo systemctl start guardian-llmprovider-gateway
sudo systemctl stop guardian-llmprovider-gateway
sudo systemctl restart guardian-llmprovider-gateway
sudo systemctl status guardian-llmprovider-gateway

# Verify it's listening
curl -s http://127.0.0.1:11434/healthz

# View recent logs (ignore healthz noise)
journalctl -u guardian-llmprovider-gateway.service --since "5 min ago" --no-pager | grep -v 'GET /healthz'
```

> **Hot reload (since 2026-08-19).** Code changes (`app/*.py`) still
> require `sudo systemctl restart guardian-llmprovider-gateway` — there is NO hot code
> reload. But `settings.yaml` + `cloud_keys.json` changes (providers,
> failover groups, credential links, capture cloud_capture/prefixes,
> failover_health, cloud_retry) now apply WITHOUT restart via:
> `curl -X POST -H "Authorization: Bearer <key>" localhost:11434/api/config/reload`
> (any valid key; port/pid/TLS stay restart-only).

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
./venv/bin/python -c "import yaml; [print(k) for k in yaml.safe_load(open('config/guardian.keys.yaml'))]"

# Named keys exist for: goose, oelala, hydroponics, and others.
```

## Capture permissions — drift check and correction

Every capture artefact — the plain WAL (`guardian_capture_current.jsonl`), the
rotated `.jsonl.gz` archives, their `.sha256` sidecars, the extracted media
files under `media/` and the directories holding them — is supposed to carry
the modes in capture configuration (`capture.file_mode` /
`capture.directory_mode`, currently `0640` / `0750`). Since
[#49](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/49) the
writers apply them explicitly (media since `5a174dc`), so new artefacts no
longer depend on the process umask.

Artefacts written **before** those fixes keep their old, looser mode — the
live `data/capture/media/` tree still holds files from the pre-fix window that
are world-readable. That residue is operator data: the service never rewrites
it. Check it and repair it with `guardianctl`, never with a hand-typed
`chmod -R` (a hardcoded mode can drift from configuration and leaves no audit
trail):

```bash
cd /home/flip/guardian-llmprovider-gateway

# Report drift — the default, and safe: it only reads mode bits.
# Exit status: 0 clean, 1 drift found, 2 configuration/tree error,
#              130 interrupted (Ctrl-C during --apply; changes so far are listed).
./venv/bin/python scripts/guardianctl.py permissions
./venv/bin/python scripts/guardianctl.py permissions --json   # machine-readable, absolute paths

# Normalise the drifting entries — OPERATOR ACTION ON PRODUCTION DATA.
# Prints every change as path + old mode + new mode; nothing else changes.
./venv/bin/python scripts/guardianctl.py permissions --apply
```

What it does:

- **Report mode is the default and changes absolutely nothing.** Drift is an
  entry that grants a bit the configured mode does not (looser than
  configuration) or that lacks owner access the configuration implies
  (`owner-read`/`owner-write`, plus `owner-execute` on directories). It covers
  files and directories, follows no symlink, and still reports a file the owner
  cannot read (only mode bits are read, never content).
- **Artefacts that are tighter than configuration are not drift** and are left
  alone — e.g. `.capture_state.json` at `0600` against a configured `0640`.
  `--apply` sets each *drifting* entry exactly to its configured mode and does
  not touch anything else.
- **The modes come from `capture.file_mode`/`capture.directory_mode`.** A
  settings file that is missing, unparsable, has no `capture:` section or
  carries a rejected mode is a clear error (exit 2); the command refuses to
  compare a live tree against a guessed default.
- `--root DIR` inspects another tree instead of the configured capture root;
  use it to rehearse on a copy or on a temporary tree. The root is resolved to
  an absolute path before the scan, and `--json` reports absolute paths.
- **The walk is descriptor-based, so it cannot leave the tree.** Every directory
  is opened with `O_DIRECTORY | O_NOFOLLOW` relative to its parent's descriptor
  and its `st_dev`/`st_ino` is compared with what the listing said; children are
  read with `scandir(fd)` / `stat(name, dir_fd=fd)`. A directory swapped for a
  symlink mid-walk is therefore never traversed (it is reported as a skipped
  symlink), and no file outside the capture root can be audited.
- **A correction only ever touches the entry it audited.** `--apply` re-opens the
  parent directory (identity-checked), reaches the entry by name relative to it
  with `O_NOFOLLOW | O_NONBLOCK`, re-checks the entry's own `st_dev`/`st_ino`,
  and `fchmod`s that descriptor. A symlink or type replacement (`ELOOP`,
  `ENOTDIR`) and a replaced inode are reported failures with a non-zero exit;
  nothing outside the capture tree can be changed. `O_NONBLOCK` is why a FIFO
  swapped in for an entry fails immediately instead of blocking the open.
- **A stale scan never widens a mode.** The drift is recomputed from the mode
  read on the descriptor, so an entry someone tightened in the meantime (e.g.
  `0644` → `0600` after the scan) is left alone and reported as "changed
  meanwhile"; `--apply` only ever normalises what still drifts. A corrected entry
  prints the mode the correction actually replaced, not the scan value.
- **The audit trail is written as it happens.** Every applied change is printed
  to **stderr** the moment its `fchmod` succeeds (not at the end), so an
  interrupted `--apply` — Ctrl-C while a later entry is opened — still lists
  every mode it already changed, and `--json` keeps stdout one clean document.
  The run then exits **130** with a partial summary; re-run to finish.
- **The modes come from one validated read.** The settings file is read and
  validated once, and the config is built from that document — not re-read. The
  runtime loader's documented fallback (a writer must keep working) substitutes
  `0640`/`0750` when the file is unreadable, which would grant group read to a
  tree configured `0600`/`0700`; the audit never lets that happen mid-run.
- **An entry it cannot open is a reported failure, not a repair**: a file at
  `0000` (or a directory the owner cannot read) denies the open the correction
  needs, so it is listed and the exit status is non-zero; fix those by hand.
  Likewise an **unreadable directory aborts the whole audit** (exit 2) instead
  of reporting a partial tree as clean — a walk that never entered a directory
  cannot vouch for what is inside it, and a path replaced by something of another
  type (`ENOTDIR`) is a replacement, not a disappearance.

`--apply` on production data is the operator's call, and it is a `chmod` on
user content that is already on disk: run the report first, read the list, then
decide. An independent read-only cross-check of the world bits:

```bash
cd /home/flip/guardian-llmprovider-gateway/data/capture
find . -type f -perm -004 | wc -l      # expect 0 on a clean tree
find . -type f -perm -004 -printf '%TY-%Tm-%Td %TH:%TM %m %p\n' | sort | head
```

The service restart that activates a writer change is the usual
`scripts/pre_restart_check.py` gate plus the restart batch; never `chmod` a
live capture tree without an explicit operator decision (issue #49 forbids a
silent rewrite).

## Discovering served models

```bash
KEY="<your_guardian_api_key>"
curl -s -H "Authorization: Bearer $KEY" \
  http://192.168.1.G:11434/v1/models | python3 -m json.tool
```

The served list is built from `config/models.local.settings.yaml` (local aliases) +
`config/settings.yaml` (`providers.*.models`, cloud). Prefix-based
cloud models (via `model_prefixes`) are routable but don't appear in
the discovery list — see `docs/LLM_ROUTER.md`.

## Common errors and fixes

### `404 model_not_served`

The client sent a model name not in the served set. Check:
1. Is it a local alias? → `config/models.local.settings.yaml`
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
| `config/models.local.settings.yaml` | Model registry (aliases, runtime, tensor_split) | restart |
| `config/guardian.keys.yaml` | Named API keys | restart |
| `.env` | Secrets (env var expansion) | restart |
| `scripts/start_llama.sh` | llama-server launch args | restart llama-server |

## When making code changes

1. Edit `app/*.py`
2. **Put the checkout on the merge target** — the unit runs this checkout, so a
   restart deploys whatever `HEAD` it is on.

   **If the checkout is behind `main`, bootstrap first**: the tool does not exist
   in that tree yet, and a copy of `prepare_deployment.py` cannot start because it
   imports its sibling `scripts/pre_restart_check.py`, which that tree lacks too.
   Run it from a `main` worktree and point `--repo` at the live checkout:
   ```bash
   cd /home/flip/guardian-llmprovider-gateway && \
     git fetch origin && \
     git worktree add --detach /tmp/main-tool origin/main && \
     /home/flip/guardian-llmprovider-gateway/venv/bin/python \
         /tmp/main-tool/scripts/prepare_deployment.py \
         --repo /home/flip/guardian-llmprovider-gateway
   ```
   That is the **plan** — a dry run that writes nothing. Review it, then re-run
   the last command **with `--apply` appended, still from the worktree**:
   ```bash
   /home/flip/guardian-llmprovider-gateway/venv/bin/python \
       /tmp/main-tool/scripts/prepare_deployment.py \
       --repo /home/flip/guardian-llmprovider-gateway --apply
   ```
   `--apply` cannot be run from the live tree first: until that switch lands the
   live tree carries neither script, so the worktree tool is the only one that
   exists. The `&&` chain stops on a failed step, so a pre-existing
   `/tmp/main-tool` makes the block fail instead of running stale code, and
   `--repo` is what makes it work across trees: the code runs from the `main`
   worktree, the plan and the writes target the live checkout.

   After that switch the tool runs normally from its own tree:
   ```bash
   ./venv/bin/python scripts/prepare_deployment.py            # plan + diff, no writes
   ./venv/bin/python scripts/prepare_deployment.py --apply    # backup, stash, switch, install
   ```
   It proves the switch is a fast-forward, refuses when the tree carries commits
   `main` lacks or when `app/`/`scripts/` is dirty, backs up
   `config/global.settings.yaml` before any write, and reconciles that file as
   `main`'s version plus the operator's local delta (the 420 s remote-ensure
   window, the queue-timeout choice, the `cloud_retry` decision comment). It
   never restarts, pushes or merges — it prints the next three commands and
   stops.
3. Run the full pre-restart gate (deployment head + py_compile + pyflakes +
   signature check + pytest):
   ```bash
   ./venv/bin/python scripts/pre_restart_check.py
   ```
   Every blocking gate must PASS before restarting — a startup-breaking error is
   not self-healable because the agent's own model traffic routes through
   Guardian (see AGENTS.md Critical rules).
4. `sudo systemctl restart guardian-llmprovider-gateway` — deploy
5. **Run the post-restart verifier from the deployed checkout, as the service
   user** — this is the step that proves the restart actually deployed what was
   merged (`active` alone does not):
   ```bash
   cd /home/flip/guardian-llmprovider-gateway
   ./venv/bin/python scripts/verify_post_restart.py
   ./venv/bin/python scripts/verify_post_restart.py --json   # machine use
   ```
   Exit `0` = every check passed; `1` = at least one check failed (the running
   deployment is NOT the merged contract); `2` = no failures but something
   could not be verified — read the `SKIP` reasons; `3` = **refused**: this
   checkout is not the tree the service runs in, so nothing was graded. The
   script is read-only: it probes the `MainPID` listener on
   `https://127.0.0.1:11435`, reads the journald log and the capture tree, and
   never uploads a large body (the body-bound check declares an oversized
   `Content-Length` and sends zero bytes).
6. `curl -s http://127.0.0.1:11434/healthz` — verify it's back up
7. Watch logs: `journalctl -u guardian-llmprovider-gateway.service -f | grep -v healthz`

### The restart sequence proves the tree, not just the code

The unit runs this checkout, so `systemctl restart` deploys whatever `HEAD` the
checkout is on — which is how a restart on 2026-10-29 would have deployed
`b30e027` (a feature branch 29 commits *behind* `main`) while every gate passed
([#85](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/85)).
The sequence is therefore **bootstrap from a `main` worktree when the checkout is
behind (below) → `scripts/prepare_deployment.py --apply` (which does the `git
checkout main && git pull --ff-only` and the config reconciliation) →
`scripts/pre_restart_check.py` → `sudo systemctl restart
guardian-llmprovider-gateway` → `scripts/verify_post_restart.py`**, and each
step now names the commit it is talking about: the gate's first step prints
`[PASS]/[FAIL]/[WARN] deployment head — on <branch> @ <sha>, origin/main <sha>,
N commits not in main` and FAILs when `HEAD` is not `origin/main` in either
direction (ahead: unreviewed work; behind: old code), when `app/` or `scripts/`
is dirty, and — with `--expect-head <sha>` — when `HEAD` is not the commit the
operator pinned; a dirty `config/global.settings.yaml` is only a WARN because
that file is runtime input and the live copy is the operator's local delta. The
verifier then prints `[INFO] deployed commit — source=deployed-tree` (and
`deployed_commit` in `--json`), read from `/proc/<MainPID>/cwd` →
`git -C <root> rev-parse HEAD`, so a green verdict names what it graded. Neither
line is a verdict on its own: an unprovable head is a `SKIP`, never a pass, and
a deliberately old deployment is legitimate.

**The first run on a checkout behind `main`** is the one case where the tool
cannot start from its own tree: neither `scripts/prepare_deployment.py` nor its
sibling `scripts/pre_restart_check.py` exists there yet, and a copy of the file
dies on the import. Bootstrap from a `main` worktree and point `--repo` at the
live checkout:

```bash
cd /home/flip/guardian-llmprovider-gateway && \
  git fetch origin && \
  git worktree add --detach /tmp/main-tool origin/main && \
  /home/flip/guardian-llmprovider-gateway/venv/bin/python \
      /tmp/main-tool/scripts/prepare_deployment.py \
      --repo /home/flip/guardian-llmprovider-gateway
```

That is the plan; re-run the last command with `--apply` appended — still from
the worktree, because the live tree has neither script until that switch lands.
The code runs from the `main` worktree; the plan and the writes target the live
checkout. After that switch the tool runs normally from its own tree and the
sequence above applies unchanged.

### What the post-restart verifier checks

One line per item, and **every line names its source** (`source=process`,
`live-api`, `deployed-code`, `deployed-config`, `deployed-file`, `journal`), so
a reader can tell a process fact from a file fact: service identity (`MainPID`
owns `127.0.0.1:11435`, not just `active`), the queue wait budget, capture media
modes (`0750` directory / `0640` files), the removed `get_version` (the module
the process *loaded*, parsed from `/proc/<MainPID>/cwd`, plus a `404` on
`/api/version`), privilege-scope grace (a `scope-grace` line in the journal), the
`/metrics` gate (`401` anonymous), the **live** destination-policy source and the
flagging of the `192.168.1.245` cleartext hops (read from `/api/status` →
`policy`, never from a YAML file next to the script), admission bounds (queue
depth, resolved body bound, `guardian_admission_rejections_total`), the
metadata-only audit record, the `Bearer`-free log invariant, `/v1` endpoint
confinement, the backend-death seam on `/api/status`, plus the retained
key-store, config-merge and cloud-catalog-credential checks.

**The tree guard.** Before any check runs, the script asks the service where it
lives (`/proc/<MainPID>/cwd`, cross-checked with `systemctl show -p
WorkingDirectory`) and compares that with its own checkout. A different tree — or
a service that is down, so the tree cannot be determined — is **refused**
(exit `3`, both paths named, no verdict printed). This is the whole point: a
verifier that grades another checkout must not print `PASS` for code that is not
running ([#84](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/84)).

Run it **as the service user** (or root) for that reason: `/proc/<MainPID>/cwd`
must be readable. The unit's `WorkingDirectory` is only a cross-check, never
evidence — a `daemon-reload` can change it without restarting the process, so an
unreadable cwd makes the tree unknown and the run refuses instead of guessing.

`--allow-foreign-tree` is for **review and CI only**: it lets the run proceed
from any checkout, but every check whose source is not the live HTTP API reports
`SKIP` with *"does not prove the deployment"* instead of a verdict, and the run
ends with an explicit "this run graded <script tree>, not the deployment". It
never turns a process-level check into a PASS.

A `FAIL` on a config-derived item (destination allowlist source, admission
bound) means the *live* `config/global.settings.yaml` has no such block and the
service is running on the shipped default: merge the block from the repository
copy (both are hot-reloadable) and re-run the verifier.

A `SKIP` means the verifier could not verify that item and says why — a missing
scoped key (`admin`, `capture-export`), no media file written since the restart,
a grace window that already closed, or a foreign-tree run. It is never a quiet
PASS.
