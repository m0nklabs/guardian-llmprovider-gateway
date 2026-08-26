"""PR-Piet schone-PR test #2: correcte helpers zonder bewuste problemen.

Doel: aantonen dat bij een schone tier-1 de formele review als COMMENT wordt
gepost (geen REQUEST_CHANGES, geen inline threads) en tier 2 draait.
"""

from __future__ import annotations


def clamp(value: int, low: int, high: int) -> int:
    """Begrens een waarde aan [low, high] (low <= high)."""
    if low > high:
        raise ValueError("low mag niet groter zijn dan high")
    return max(low, min(high, value))


def is_even(value: int) -> bool:
    """True als value even is."""
    return value % 2 == 0
