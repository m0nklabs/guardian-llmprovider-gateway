#!/usr/bin/env python3
"""Post-restart keanu key-linking via the new claim+link endpoints.

Run AFTER the operator/agent has restarted llama-guardian with commit
c1c603c live. Uses config/api_keys.json tokens without printing them.

Flow:
  1. Claim legacy owner-less credentials (nvidia, openrouter) with the
     'hermes' key (already linked to both — required for the claim).
  2. Link the keanu-factory fingerprint to nvidia + openrouter via hermes.
  3. Link keanu to google via the google owner (claudekvm2 fingerprint).
  4. Verify: list keanu's links + a tiny live cloud probe.

Exit 0 on full success; nonzero with a clear message otherwise.
"""

import hashlib
import json
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:11434"

KEANU_FP = "7e573421cf2a"          # sha256[:12] of keanu-factory .env LLM_API_KEY
HERMES_NAME = "hermes"
CLAUDEKVM2_NAME = "claudekvm2"
CLAIM_KEYS = {"nvidia": "cred_1bdc257b", "openrouter": "cred_4edcf709"}
GOOGLE_CRED = "cred_b620ca88"


def fp_of(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def main() -> int:
    keys = json.loads((REPO / "config/api_keys.json").read_text())
    by_name = {m.get("name"): tok for tok, m in keys.items() if m.get("name")}

    hermes = by_name.get(HERMES_NAME)
    claudekvm2 = by_name.get(CLAUDEKVM2_NAME)
    keanu = next((tok for tok in keys if fp_of(tok) == KEANU_FP), None)
    if not hermes or not claudekvm2 or not keanu:
        print("ERROR: missing key token (hermes/claudekvm2/keanu)", file=sys.stderr)
        return 2
    if fp_of(keanu) != KEANU_FP:
        print(f"ERROR: keanu token fp mismatch: {fp_of(keanu)}", file=sys.stderr)
        return 2

    c = httpx.Client(base_url=BASE, timeout=30)

    def auth(name: str):
        return {"Authorization": f"Bearer {by_name[name]}"}

    # 1. Claim legacy nvidia + openrouter via hermes
    for provider, cred_id in CLAIM_KEYS.items():
        r = c.post(
            "/api/cloud/credentials/claim",
            headers=auth(HERMES_NAME),
            json={"provider": provider, "credential_id": cred_id},
        )
        status = r.json().get("status") if r.headers.get("content-type", "").startswith("application/json") else r.text[:80]
        print(f"claim {provider:10s} -> {r.status_code} {status}")
        if r.status_code not in (200, 409):
            print(f"  unexpected: {r.text[:200]}", file=sys.stderr)
            return 3

    # 2. Link keanu to nvidia + openrouter via hermes (now owner)
    for provider, cred_id in CLAIM_KEYS.items():
        r = c.post(
            "/api/cloud/links",
            headers=auth(HERMES_NAME),
            json={
                "guardian_key_fingerprint": KEANU_FP,
                "provider": provider,
                "credential_id": cred_id,
            },
        )
        print(f"link {provider:10s} -> {r.status_code} {r.text[:120]}")
        if r.status_code != 200:
            print("  unexpected", file=sys.stderr)
            return 4

    # 3. Link keanu to google via the google owner (claudekvm2)
    r = c.post(
        "/api/cloud/links",
        headers=auth(CLAUDEKVM2_NAME),
        json={
            "guardian_key_fingerprint": KEANU_FP,
            "provider": "google",
            "credential_id": GOOGLE_CRED,
        },
    )
    print(f"link google      -> {r.status_code} {r.text[:120]}")
    if r.status_code != 200:
        print("  unexpected", file=sys.stderr)
        return 5

    # 4a. Verify keanu's links now list
    r = c.get("/api/cloud/links", headers=auth(CLAUDEKVM2_NAME))
    print(f"list links (claudekvm2 view) -> {r.status_code}")
    links = r.json().get("links", {})
    keanu_links = links.get(KEANU_FP, {})
    print(f"  keanu providers: {sorted(keanu_links.keys())}")
    if sorted(keanu_links.keys()) != ["google", "nvidia", "openrouter"]:
        print(f"  WARNING: keanu links incomplete: {keanu_links}", file=sys.stderr)
        return 6

    # 4b. Tiny live probe with the keanu token (no secrets printed)
    r = c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {keanu}"},
        json={
            "model": "guardian/openrouter/moonshotai/kimi-k3",
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 8,
        },
        timeout=120,
    )
    print(f"probe keanu openrouter kimi-k3 -> {r.status_code}")
    if r.status_code != 200:
        print(f"  probe body: {r.text[:300]}", file=sys.stderr)
        return 7

    print("SUCCESS: keanu-factory key linked (nvidia+openrouter+google) and probing OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
