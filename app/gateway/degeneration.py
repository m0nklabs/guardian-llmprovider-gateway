"""Degeneration detection — cut off responses that fall into a repetition loop.

Operator feature request (2026-09-02): when token output starts repeating,
Guardian cuts the stream server-side (standard ``finish_reason: "length"``)
so clients can work with roomier ``max_tokens`` budgets without burning them
on degenerate output.

Design:
- The detector is pure text state (no I/O, no subprocess) — safe on the
  event loop; the structural guard test enforces that this module stays free
  of blocking calls.
- Streaming: feed each extracted content delta; when the CURRENT tail is a
  repeating substring of period ``p`` repeated ``k`` times (``p*k >=
  min_repeat_bytes``), cut the stream and synthesize a clean finish chunk.
- Non-stream: ``run_full`` over the complete text returns the verdict with a
  cut point that keeps exactly one instance of the loop.
- False-positive guards: a period floor (short repetitions like "ha ha ha"
  or "OK OK OK" are period 2-4 and never match), a repeats floor, and a
  minimum repeated-byte total. Thresholds are config-tunable and the whole
  feature has a kill-switch (``degeneration.enabled: false``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("Guardian.Degeneration")

#: mtime-gated config cache — detectors are created per stream; the yaml
#: re-parse only happens when global.settings.yaml actually changed.
_CONFIG_CACHE: dict[str, object] = {}

DEFAULTS = {
    "enabled": True,
    "min_period": 6,
    "max_period": 300,
    "min_repeats": 3,
    "min_repeat_bytes": 240,
    "max_window_chars": 4096,
    "marker_enabled": True,
    "marker_text": "[guardian: generation cut off - repetition loop detected]",
}


@dataclass(frozen=True)
class DegenerationConfig:
    enabled: bool = True
    min_period: int = 6
    max_period: int = 300
    min_repeats: int = 3
    min_repeat_bytes: int = 240
    max_window_chars: int = 4096
    # Human-visible notice appended to the output at cutoff so a reader of
    # the text knows the generation was cut short (machine readers use the
    # standard finish_reason + the capture degeneration_cutoff flag).
    marker_enabled: bool = True
    marker_text: str = "[guardian: generation cut off - repetition loop detected]"


@dataclass(frozen=True)
class DegenerationVerdict:
    """A detected repetition loop at the current tail of the output."""

    period: int
    repeats: int
    repeat_bytes: int

    @property
    def cut_from_end(self) -> int:
        """Bytes to trim from the end to keep exactly ONE loop instance."""
        return self.repeat_bytes - self.period


def load_degeneration_config(path: Path | None = None) -> DegenerationConfig:
    """Read the ``degeneration:`` section of global.settings.yaml (mtime-gated)."""
    try:
        if path is None:
            from app.paths import global_settings_file

            path = global_settings_file()
        st = path.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None

    if cache_key is not None and _CONFIG_CACHE.get("key") == cache_key:
        return _CONFIG_CACHE["value"]

    cfg = DegenerationConfig()
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        section = raw.get("degeneration") or {}
        if isinstance(section, dict):
            cfg = DegenerationConfig(
                enabled=bool(section.get("enabled", DEFAULTS["enabled"])),
                min_period=int(section.get("min_period", DEFAULTS["min_period"])),
                max_period=int(section.get("max_period", DEFAULTS["max_period"])),
                min_repeats=int(section.get("min_repeats", DEFAULTS["min_repeats"])),
                min_repeat_bytes=int(
                    section.get("min_repeat_bytes", DEFAULTS["min_repeat_bytes"])
                ),
                max_window_chars=int(
                    section.get("max_window_chars", DEFAULTS["max_window_chars"])
                ),
                marker_enabled=bool(
                    section.get("marker_enabled", DEFAULTS["marker_enabled"])
                ),
                marker_text=str(
                    section.get("marker_text", DEFAULTS["marker_text"])
                ),
            )
    except Exception as exc:
        logger.warning("Degeneration config load failed (using defaults): %s", exc)

    if cache_key is not None:
        _CONFIG_CACHE["key"] = cache_key
        _CONFIG_CACHE["value"] = cfg
    return cfg


class DegenerationDetector:
    """Streaming-friendly repetition-loop detector (pure text state)."""

    def __init__(self, config: DegenerationConfig | None = None) -> None:
        self.config = config or load_degeneration_config()
        self._window = ""

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def feed(self, delta: str) -> DegenerationVerdict | None:
        """Feed one content delta; return a verdict when the tail is a loop."""
        if not self.config.enabled or not delta:
            return None
        window_cap = self.config.max_window_chars
        self._window = (self._window + delta)[-window_cap:]
        return self._check(self._window)

    def marker_delta(self) -> str | None:
        """Human-visible notice to append at cutoff (None = marker off)."""
        if (
            not self.config.enabled
            or not self.config.marker_enabled
            or not self.config.marker_text
        ):
            return None
        return "\n\n" + self.config.marker_text

    def run_full(self, text: str) -> DegenerationVerdict | None:
        """Non-stream form: check the complete text for a tail loop."""
        if not self.config.enabled or not text:
            return None
        return self._check(text[-self.config.max_window_chars :])

    def _check(self, window: str) -> DegenerationVerdict | None:
        if len(window) < self.config.min_repeat_bytes:
            return None
        max_p = min(self.config.max_period, len(window) // self.config.min_repeats)
        for p in range(self.config.min_period, max_p + 1):
            tail = window[-p:]
            if window[-2 * p : -p] != tail:
                continue  # fast reject: the tail is not (yet) a repeat
            k = 1
            pos = len(window) - p
            while pos >= p and window[pos - p : pos] == tail:
                k += 1
                pos -= p
            if k < self.config.min_repeats or p * k < self.config.min_repeat_bytes:
                continue
            # Fundamental period: any repetition at period p is also a
            # repetition at every multiple — a short echo like "ha ha " (p=3)
            # would otherwise be caught as a period-6 loop. Find the smallest
            # period q; short-echo patterns (q < min_period) are exempt.
            q = p
            for d in range(1, p):
                if p % d == 0 and window[-p:] == window[-d:] * (p // d):
                    q = d
                    break
            if q < self.config.min_period:
                return None  # fundamental period is a short echo — exempt
            # Recount repeats at the fundamental period.
            kq = 1
            pos = len(window) - q
            fundamental_tail = window[-q:]
            while pos >= q and window[pos - q : pos] == fundamental_tail:
                kq += 1
                pos -= q
            if kq >= self.config.min_repeats and q * kq >= self.config.min_repeat_bytes:
                return DegenerationVerdict(period=q, repeats=kq, repeat_bytes=q * kq)
            return None
        return None


def make_detector() -> DegenerationDetector:
    """Create a detector with the current (mtime-cached) config."""
    return DegenerationDetector(load_degeneration_config())


def log_cutoff(model: str, verdict: DegenerationVerdict, duration_s: float | None = None) -> None:
    logger.info(
        "🛑 Degeneration cutoff: model=%s loop_period=%d repeats=%d bytes=%d%s",
        model,
        verdict.period,
        verdict.repeats,
        verdict.repeat_bytes,
        f" duration={duration_s:.1f}s" if duration_s is not None else "",
    )
