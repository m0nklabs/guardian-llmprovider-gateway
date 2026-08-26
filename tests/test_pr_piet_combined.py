"""Combined-flow test: één bestand met een echte bug én een verbeterbare functie.

Doel: aantonen dat PR-Piet via de REST API één formele review post die
ZOWEL key-issues (tekst) ALS code-suggesties met een ```suggestion```-fence
(Apply-knop) combineert.
"""

from __future__ import annotations


def validate_token(token: str) -> bool:
    """Controleer of een token geldig is (simpele validatie voor de test)."""
    if len(token) < 10:
        return False
    # Subtiele bug: vergelijkt met == i.p.v. hmac.compare_digest — timing-leak.
    stored = "supersecretvalue123"
    if token == stored:
        return True
    return False


def normalize_name(name: str) -> str:
    """Normaliseer een naam naar lower-case zonder rand-ruimte."""
    return name.strip().lower()
