# G1-06 — The credential fingerprint is an identity, not a password hash

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Decided** | 2026-10-28 |
| **Supersedes** | none |
| **Superseded by** | none |
| **Issue** | [#76](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/76) |
| **Alerts** | `py/weak-sensitive-data-hashing` #2; `py/clear-text-logging-sensitive-data` #4, #5, #7; `py/clear-text-storage-sensitive-data` #10 |

## Decision

Guardian hashes credentials in exactly two places, and **neither hash
participates in verification**:

| Hash | Where | What it is for | What it may never be used for |
| --- | --- | --- | --- |
| `sha256(token)[:12]` | `app/proxy/auth.py:_token_fingerprint` | the G1-01 identity — rate-limit key, audit `key_fingerprint`, dashboard attribution | authentication, key lookup, anything that accepts it as a credential |
| `sha256(api_key)[:16]` | `app/proxy/providers.py:_catalog_cache_key` | a cache-scope discriminator, so a rotated provider credential cannot reuse a catalog fetched with the old one | storage or comparison of a secret |

A fast hash is correct for both, because **the input is not a password**: a
Guardian key is `prefix` + `secrets.token_hex(16)` = 128 random bits
(`scripts/generate_key.py`, `app/proxy/auth.py:generate_api_key`), and a cloud
provider credential is issued by the upstream provider. There is no low-entropy
secret to make brute-force-resistant, so a slow KDF (PBKDF2/scrypt/argon2)
would buy nothing and cost a hash on every rate-limited request. Key
verification is an exact lookup of the presented token in the key store
(`load_api_keys()[token]`); no digest is compared, so no hash can weaken it. The
truncated digest is never logged, never returned to a client and never written
to the key store.

Consequences that must hold, and are now pinned by tests:

1. The fingerprint is **not** a credential: presenting it as a token is a 401
   (`tests/unit/test_auth.py::TestVerifyApiKey::test_the_identity_fingerprint_is_not_a_credential`).
2. The cache key is **stable per credential** and **different for a different
   credential**, never contains the credential or its first 16 characters, and
   is a 16-character hex digest
   (`tests/unit/test_providers.py::TestCatalogCacheKeyFingerprint`, four tests).
3. The audit record keeps the fingerprint and, separately, the *name* — a label
   that is not a secret (G1-03 §11.2). The name reaches the INFO line only
   through `clean_log_value` and never when it contains the presented token
   (G1-04 addendum).

## The alerts this record dismisses, and why

| Alert | Site | Dismissal reason | Argument |
| --- | --- | --- | --- |
| #2 | `app/proxy/providers.py:649` | **false positive** | The query calls the input a *password* and objects that SHA-256 is not computationally expensive. The input is a 128-bit random credential used as a cache-scope discriminator (see the table above): there is no password storage and no password verification in the path. The docstring at the site now states what the hash protects and what it must never be used for. |
| #7 | `app/proxy/auth.py:694` | **false positive** | The logged expression is `user_data["name"]` — the operator-assigned label of a key-store entry, not the entry's key and not the dict itself. CodeQL's taint arrives at the whole metadata dict (`keys.get(token)`), so it cannot distinguish the label from the credential; a name cannot be replayed as a key. The log line is additionally sanitised and drops a label containing the presented token (G1-04 addendum), so the §11.3 invariant holds even for a mislabelled entry. |
| #4, #5 | `scripts/generate_key.py:82`, `:83` | **won't fix** | This script's entire purpose is to mint a Guardian key and hand it to its owner once, on stdout, in the operator's own terminal. The value is created in that process by `secrets.token_hex(16)` and is not read from storage. Suppressing the print would make the tool useless; writing it to a file instead would *create* a clear-text storage problem. `scripts/generate_key.py` is the documented minter (`README.md`, `scripts/install.sh`, `AGENTS.md`), so this is a deliberate disclosure channel, not a leak. |
| #10 | `tests/unit/test_capture_secret_canary.py:327` | **used in tests** | The file is a test. The "sensitive data" is a synthetic canary (`sk-proj-abc…`, `nvapi-0KeCv…`) and the assertion under test is that the *redacted* event written to a `tmp_path` JSONL contains none of it. The dismissal is pinned by a non-vacuity assertion added at the sink: `scan_for_secrets(secret_response)` must find the canary, so "no findings in the written file" cannot pass vacuously. |

#8 (`app/proxy/auth.py:722`) and #9 (`scripts/test_system.py:32`) are not
dismissed: #8 was removed with the undocumented `__main__` minting CLI and #9
stopped printing key material at all — see the G1-04 addendum.

## What would reopen this decision

- A Guardian key minted from anything less than full-entropy randomness, or a
  password-based login path: then a slow KDF becomes mandatory and this record
  must be superseded.
- Any code that compares a fingerprint against a stored value to authenticate,
  or that accepts a fingerprint as a token.
- Any change that lets the fingerprint, the cache key or the key name into a
  log line, an audit field other than `key_fingerprint`/`key_name`, or an API
  response.

## Evidence

- [#76](https://github.com/m0nklabs/guardian-llmprovider-gateway/issues/76) —
  per-site verdicts and the posted dismissal comments.
- `docs/decisions/G1-01-privilege-scopes.md` — the identity the fingerprint
  carries; `docs/decisions/G1-03-admission-and-audit.md` §11.2/§11.3 — what the
  audit record may contain and the log invariant.
- `docs/decisions/G1-04-log-injection.md` — the log-sink half of the same
  adjudication.
