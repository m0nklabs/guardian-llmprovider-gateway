"""PR-Piet formele-review test: bewust twee realistische bugs.

Doel: aantonen dat de review via de GitHub reviews REST API gepost wordt
als formele review (state-badge + inline threads op de diff, Copilot-look).
"""

from __future__ import annotations


def parse_api_key(raw: str) -> str:
    """Lees een API-key uit een header-waarde."""
    parts = raw.split(" ")
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return parts[0]


def sign(data: str, secret: str) -> str:
    """Heel eenvoudige (onveilige) signatuur voor de test."""
    import hashlib

    return hashlib.sha1((data + secret).encode("utf-8")).hexdigest()


def verify_signature(data: str, secret: str, signature: str) -> bool:
    expected = sign(data, secret)
    # BUG 1: niet-constant-time vergelijking (timing-leak).
    if expected == signature:
        return True
    return False


def extract_port(url: str) -> int:
    # BUG 2: geen guard op lege/invalide input -> ValueError voor de caller.
    return int(url.split(":")[-1].split("/")[0])
