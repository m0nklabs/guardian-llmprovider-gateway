"""Dynamic request scaler for Guardian middleware.

Analyzes prompt complexity and queue pressure to inject adaptive
``thinking_budget_tokens`` and ``max_tokens`` when the client hasn't
set them explicitly.  All values are *soft defaults* — never overrides
explicit client params.

Configurable via ``config/settings.yaml`` under the ``scaler`` key.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pathlib import Path

logger = logging.getLogger("Scaler")

# ---------------------------------------------------------------------------
# Defaults — used when settings.yaml has no scaler section
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "log_decisions": True,
    "profiles": {
        "trivial": {
            "max_chars": 200,
            "max_messages": 2,
            "thinking_budget": 256,
            "max_tokens": 1024,
        },
        "simple": {
            "max_chars": 800,
            "max_messages": 4,
            "thinking_budget": 1024,
            "max_tokens": 4096,
        },
        "moderate": {
            "max_chars": 4000,
            "max_messages": 12,
            "thinking_budget": 4096,
            "max_tokens": 8192,
        },
        "complex": {
            "max_chars": 15000,
            "max_messages": 30,
            "thinking_budget": 8192,
            "max_tokens": 16384,
        },
        # Anything beyond 'complex' thresholds → deep (unlimited thinking)
        "deep": {
            "thinking_budget": -1,
            "max_tokens": 32768,
        },
    },
    "queue_pressure": {
        "moderate_threshold": 2,
        "heavy_threshold": 4,
        "moderate_thinking_factor": 0.5,
        "heavy_thinking_factor": 0.25,
        "moderate_max_tokens_factor": 0.75,
        "heavy_max_tokens_factor": 0.5,
    },
}


def _load_scaler_config() -> Dict[str, Any]:
    """Load scaler config from settings.yaml, merged with defaults."""
    config = dict(_DEFAULT_CONFIG)
    try:
        path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        if path.exists():
            with open(path, "r") as f:
                file_cfg = yaml.safe_load(f) or {}
            scaler_cfg = file_cfg.get("scaler", {})
            if scaler_cfg:
                # Shallow merge top-level keys
                for key in ("enabled", "log_decisions"):
                    if key in scaler_cfg:
                        config[key] = scaler_cfg[key]
                # Deep merge profiles
                if "profiles" in scaler_cfg:
                    for profile_name, profile_vals in scaler_cfg["profiles"].items():
                        if profile_name in config["profiles"]:
                            config["profiles"][profile_name].update(profile_vals)
                        else:
                            config["profiles"][profile_name] = profile_vals
                # Deep merge queue_pressure
                if "queue_pressure" in scaler_cfg:
                    config["queue_pressure"].update(scaler_cfg["queue_pressure"])
    except Exception as e:
        logger.warning(f"Failed to load scaler config: {e}. Using defaults.")
    return config


class DynamicScaler:
    """Injects adaptive thinking_budget_tokens and max_tokens into requests.

    All decisions are *soft*: explicit client values are NEVER overridden.
    """

    def __init__(self) -> None:
        self.config = _load_scaler_config()
        self._config_mtime: float = 0.0
        logger.info(
            f"⚡ DynamicScaler initialized (enabled={self.config['enabled']}, "
            f"profiles={list(self.config['profiles'].keys())})"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        """Re-read settings.yaml (called on each request for hot-reload)."""
        try:
            path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
            if path.exists():
                mtime = path.stat().st_mtime
                if mtime > self._config_mtime:
                    self.config = _load_scaler_config()
                    self._config_mtime = mtime
                    logger.info("🔄 Scaler config reloaded")
        except Exception:
            pass  # Non-fatal

    def scale_request(
        self,
        body: Dict[str, Any],
        waiting_count: int = 0,
        active_count: int = 0,
        client_id: str = "",
    ) -> Dict[str, Any]:
        """Analyze and optionally enrich *body* with scaling parameters.

        Parameters
        ----------
        body : dict
            The parsed JSON request body (mutated in-place AND returned).
        waiting_count : int
            Number of requests currently waiting in the inference queue.
        active_count : int
            Number of requests currently being processed.
        client_id : str
            Identifier of the requesting client (for logging).

        Returns
        -------
        dict
            The (possibly mutated) body, plus a ``_scaler_meta`` key with
            decision details (stripped before forwarding).
        """
        self.reload_config()

        if not self.config.get("enabled", True):
            return body

        # Respect explicit client values — NEVER override
        has_thinking_budget = "thinking_budget_tokens" in body
        has_max_tokens = "max_tokens" in body

        if has_thinking_budget and has_max_tokens:
            # Client set both — nothing to do
            if self.config.get("log_decisions"):
                logger.debug(f"📋 [{client_id}] Client set explicit budget+tokens — scaler skipped")
            return body

        # Analyze prompt complexity
        messages = body.get("messages", [])
        profile_name, complexity = self._classify_complexity(messages)
        profile = self.config["profiles"].get(profile_name, {})

        # Base values from profile
        base_thinking = profile.get("thinking_budget", -1)
        base_max_tokens = profile.get("max_tokens", 8192)

        # Apply queue pressure adjustments
        thinking_budget, max_tokens = self._apply_queue_pressure(
            base_thinking, base_max_tokens, waiting_count
        )

        # Inject only missing fields
        injected = []
        if not has_thinking_budget and thinking_budget != -1:
            body["thinking_budget_tokens"] = thinking_budget
            injected.append(f"thinking_budget={thinking_budget}")
        if not has_max_tokens:
            body["max_tokens"] = max_tokens
            injected.append(f"max_tokens={max_tokens}")

        if self.config.get("log_decisions") and injected:
            pressure_label = self._pressure_label(waiting_count)
            logger.info(
                f"⚡ [{client_id}] Scaler: profile={profile_name} "
                f"complexity={complexity} pressure={pressure_label} "
                f"→ {', '.join(injected)}"
            )

        return body

    # ------------------------------------------------------------------
    # Complexity classification
    # ------------------------------------------------------------------

    def _classify_complexity(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[str, Dict[str, Any]]:
        """Classify prompt complexity into a profile name.

        Returns ``(profile_name, complexity_metrics)``.
        """
        total_chars = 0
        num_messages = len(messages)
        has_system = False
        has_images = False

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                has_system = True

            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # Multi-modal: list of content parts
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total_chars += len(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            has_images = True
                            total_chars += 2000  # Image ≈ 2k chars of complexity

        complexity = {
            "total_chars": total_chars,
            "num_messages": num_messages,
            "has_system": has_system,
            "has_images": has_images,
        }

        profiles = self.config["profiles"]

        # Match against thresholds (ordered from simplest to most complex)
        for profile_name in ("trivial", "simple", "moderate", "complex"):
            profile = profiles.get(profile_name, {})
            max_chars = profile.get("max_chars", float("inf"))
            max_msgs = profile.get("max_messages", float("inf"))
            if total_chars <= max_chars and num_messages <= max_msgs:
                return profile_name, complexity

        return "deep", complexity

    # ------------------------------------------------------------------
    # Queue pressure
    # ------------------------------------------------------------------

    def _apply_queue_pressure(
        self,
        base_thinking: int,
        base_max_tokens: int,
        waiting_count: int,
    ) -> Tuple[int, int]:
        """Reduce budgets proportionally to queue pressure.

        Returns ``(adjusted_thinking, adjusted_max_tokens)``.
        """
        qp = self.config["queue_pressure"]
        heavy = qp.get("heavy_threshold", 4)
        moderate = qp.get("moderate_threshold", 2)

        if waiting_count >= heavy:
            t_factor = qp.get("heavy_thinking_factor", 0.25)
            m_factor = qp.get("heavy_max_tokens_factor", 0.5)
        elif waiting_count >= moderate:
            t_factor = qp.get("moderate_thinking_factor", 0.5)
            m_factor = qp.get("moderate_max_tokens_factor", 0.75)
        else:
            return base_thinking, base_max_tokens

        # Don't reduce unlimited thinking to a number — keep unlimited
        if base_thinking == -1:
            adjusted_thinking = -1
        else:
            adjusted_thinking = max(128, int(base_thinking * t_factor))

        adjusted_max_tokens = max(512, int(base_max_tokens * m_factor))

        return adjusted_thinking, adjusted_max_tokens

    def _pressure_label(self, waiting_count: int) -> str:
        qp = self.config["queue_pressure"]
        if waiting_count >= qp.get("heavy_threshold", 4):
            return "heavy"
        if waiting_count >= qp.get("moderate_threshold", 2):
            return "moderate"
        return "none"
