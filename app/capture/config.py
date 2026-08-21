"""Validated capture configuration for Guardian's capture subsystem.

Configuration is loaded from the ``capture`` section of ``config/settings.yaml``
with environment-variable overrides for secrets.  All values are validated at
construction time so misconfiguration fails fast and loudly during startup
rather than silently dropping events at request time.

Default posture: **disabled**.  The global ``enabled`` switch must be set to
``true`` before any events are captured, regardless of per-client opt-in.
"""

from __future__ import annotations

import os
import uuid
import yaml
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Guardian.Capture.Config")

DEFAULT_CAPTURE_ROOT = "data/capture"
DEFAULT_POLICY_VERSION = "1.0.0"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_CAPTURE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
DEFAULT_MAX_PENDING_EVENTS = 10_000
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024  # 256 MB
DEFAULT_MAX_FILE_AGE_SECONDS = 3600  # 1 hour

# Excluded endpoints — never captured regardless of policy.
EXCLUDED_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/metrics",
    "/version",
    "/api/keys",
    "/admin/",
    "/api/session/",
    "/v1/models",
    "/v1/embeddings",
    "/v1/files",
)

# Ingress protocol constants.
PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OLLAMA = "ollama"

# Route-type constants.
ROUTE_LOCAL = "local"
ROUTE_CLOUD = "cloud"


@dataclass(frozen=True)
class CaptureConfig:
    """Immutable, validated capture configuration.

    Instances are reconstructed (not mutated) when settings are reloaded.
    """

    #: Global kill switch — must be true for any capture to occur.
    enabled: bool = False

    #: Capture local-inference requests (llama-server).
    local_capture: bool = False

    #: Capture cloud-provider responses (requires provider terms review).
    cloud_capture: bool = False

    #: When true, only cloud models matching ``allowed_cloud_models`` (or their
    #: namespace prefixes in ``cloud_model_prefixes``) are captured.
    #: When false, all cloud models are captured (subject to per-client opt-in).
    cloud_allowlist_enabled: bool = True

    #: Explicit cloud models permitted for capture (e.g. ["openai/gpt-4o", "anthropic/claude-3.5-sonnet"]).
    #: Matched exactly, case-sensitive.
    allowed_cloud_models: List[str] = field(default_factory=list)

    #: Namespace prefixes that permit capture of any model under them
    #: (e.g. ["openai/", "anthropic/", "nvidia/"]).  Checked when
    #: ``cloud_allowlist_enabled`` is True and the model is not in
    #: ``allowed_cloud_models``.
    cloud_model_prefixes: List[str] = field(default_factory=lambda: [
        "openai/", "anthropic/", "google/", "meta-llama/", "deepseek/",
        "qwen/", "mistralai/", "z-ai/", "minimax/", "poolside/",
        "moonshotai/", "nvidia/",
    ])

    #: When true, only clients whose HMAC client_ref is in allowed_client_refs
    #: are captured.  Per-client opt-in can never override a disabled global switch.
    per_client_opt_in: bool = True

    #: List of allowed HMAC-SHA-256 client_ref values (for per-client opt-in).
    allowed_client_refs: List[str] = field(default_factory=list)

    #: Policy for system prompt fields: "strip" | "capture"
    system_prompts: str = "strip"

    #: Policy for reasoning content: "strip" | "capture"
    reasoning: str = "strip"

    #: Policy for tool definitions: "strip" | "capture"
    tool_definitions: str = "capture"

    #: Policy for tool calls: "strip" | "capture"
    tool_calls: str = "capture"

    #: Policy for tool results: "strip" | "capture"
    tool_results: str = "strip"

    #: Policy for image data: "strip" | "hash_and_metadata"
    images: str = "hash_and_metadata"

    #: Policy for unknown content block types: "strip" | "capture"
    unknown_content_blocks: str = "strip"

    #: File retention in days.
    retention_days: int = DEFAULT_RETENTION_DAYS

    #: Maximum total bytes captured on disk before oldest files are purged.
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES

    #: Maximum number of pending events in the async queue.
    max_pending_events: int = DEFAULT_MAX_PENDING_EVENTS

    #: Maximum size of a single JSONL file before rotation.
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    #: Maximum age of an active JSONL file before rotation.
    max_file_age_seconds: int = DEFAULT_MAX_FILE_AGE_SECONDS

    #: File mode for capture files (0o640 = owner rw, group r).
    file_mode: int = 0o640

    #: Directory mode for capture dir (0o750 = owner rwx, group rx).
    directory_mode: int = 0o750

    #: Root directory for capture files.
    capture_root: str = DEFAULT_CAPTURE_ROOT

    #: Capture policy version (recorded on every event).
    policy_version: str = DEFAULT_POLICY_VERSION

    #: Guardian instance identifier (stable across restarts when persisted).
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    #: The client_ref secret is NOT stored here; it is read from the environment
    #: at runtime by :func:`compute_client_ref`.

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        """Validate all fields; raise ValueError on misconfiguration."""
        # Policy field validation
        valid_policies = {
            "system_prompts": {"strip", "capture"},
            "reasoning": {"strip", "capture"},
            "tool_definitions": {"strip", "capture"},
            "tool_calls": {"strip", "capture"},
            "tool_results": {"strip", "capture"},
            "unknown_content_blocks": {"strip", "capture"},
        }
        for field_name, allowed in valid_policies.items():
            value = getattr(self, field_name)
            if value not in allowed:
                raise ValueError(
                    f"capture.{field_name}='{value}' is not in allowed set {allowed}"
                )

        if self.images not in ("strip", "hash_and_metadata"):
            raise ValueError(
                f"capture.images='{self.images}' must be 'strip' or 'hash_and_metadata'"
            )

        if self.retention_days < 0:
            raise ValueError("capture.retention_days must be >= 0")

        if self.max_capture_bytes < 0:
            raise ValueError("capture.max_capture_bytes must be >= 0")

        if self.max_pending_events < 1:
            raise ValueError("capture.max_pending_events must be >= 1")

        if self.max_file_bytes < 1:
            raise ValueError("capture.max_file_bytes must be >= 1")

        if self.max_file_age_seconds < 1:
            raise ValueError("capture.max_file_age_seconds must be >= 1")

        # File modes: must not be world-readable or world-writable
        if self.file_mode & 0o007:
            raise ValueError(
                f"capture.file_mode={oct(self.file_mode)} must not grant world access"
            )
        if self.directory_mode & 0o007:
            raise ValueError(
                f"capture.directory_mode={oct(self.directory_mode)} must not grant world access"
            )

        # Per-client opt-in implies allowed refs exist
        if self.per_client_opt_in and not self.enabled:
            logger.debug("per_client_opt_in is moot because capture is disabled")

    @property
    def is_active(self) -> bool:
        """True when capture is enabled and at least one route type is allowed."""
        if not self.enabled:
            return False
        return self.local_capture or self.cloud_capture

    def should_capture_route(self, route_type: str) -> bool:
        """Return True when the given route type is enabled for capture."""
        if route_type == ROUTE_LOCAL:
            return self.local_capture
        if route_type == ROUTE_CLOUD:
            return self.cloud_capture
        return False

    def should_capture_client(self, client_ref: Optional[str]) -> bool:
        """Return True when the client is allowed to be captured.

        When ``per_client_opt_in`` is False (and global is enabled), all
        authenticated clients are captured.  When True, only clients whose
        ``client_ref`` appears in ``allowed_client_refs`` are captured.
        A missing ``client_ref`` (unauthenticated) is never captured.
        """
        if not self.enabled:
            return False
        if not self.per_client_opt_in:
            return client_ref is not None
        if not client_ref:
            return False
        return client_ref in self.allowed_client_refs

    def is_endpoint_excluded(self, endpoint: str) -> bool:
        """Return True for admin/health/metrics endpoints that are never captured."""
        normalized = endpoint.strip()
        for prefix in EXCLUDED_PATH_PREFIXES:
            if normalized.startswith(prefix):
                return True
        return False


def load_capture_config(settings_path: Optional[Path] = None) -> CaptureConfig:
    """Load capture configuration from settings.yaml with env-var overrides.

    Returns a default (disabled) config if the section is absent or the file
    is unreadable.
    """
    if settings_path is None:
        settings_path = (
            Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        )

    capture_section: Dict[str, Any] = {}
    try:
        if settings_path.exists():
            with open(settings_path, "r") as f:
                full_cfg = yaml.safe_load(f) or {}
            capture_section = full_cfg.get("capture", {}) or {}
    except Exception as exc:
        logger.warning("Failed to load capture config from %s: %s — using defaults", settings_path, exc)
        capture_section = {}

    # The client_ref secret is never stored in YAML; read from env at runtime.
    # It is NOT passed into the config object itself.

    def _get(key: str, default: Any) -> Any:
        val = capture_section.get(key, default)
        if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
            env_var = val[2:-1]
            val = os.environ.get(env_var, "")
        return val

    file_mode_raw = _get("file_mode", 0o640)
    dir_mode_raw = _get("directory_mode", 0o750)

    config = CaptureConfig(
        enabled=bool(_get("enabled", False)),
        local_capture=bool(_get("local_capture", False)),
        cloud_capture=bool(_get("cloud_capture", False)),
        cloud_allowlist_enabled=bool(_get("cloud_allowlist_enabled", True)),
        allowed_cloud_models=list(_get("allowed_cloud_models", [])),
        cloud_model_prefixes=list(_get("cloud_model_prefixes", [
            "openai/", "anthropic/", "google/", "meta-llama/", "deepseek/",
            "qwen/", "mistralai/", "z-ai/", "minimax/", "poolside/",
            "moonshotai/", "nvidia/",
        ])),
        per_client_opt_in=bool(_get("per_client_opt_in", True)),
        allowed_client_refs=list(_get("allowed_client_refs", [])),
        system_prompts=str(_get("system_prompts", "strip")),
        reasoning=str(_get("reasoning", "strip")),
        tool_definitions=str(_get("tool_definitions", "capture")),
        tool_calls=str(_get("tool_calls", "capture")),
        tool_results=str(_get("tool_results", "strip")),
        images=str(_get("images", "hash_and_metadata")),
        unknown_content_blocks=str(_get("unknown_content_blocks", "strip")),
        retention_days=int(_get("retention_days", DEFAULT_RETENTION_DAYS)),
        max_capture_bytes=int(_get("max_capture_bytes", DEFAULT_MAX_CAPTURE_BYTES)),
        max_pending_events=int(_get("max_pending_events", DEFAULT_MAX_PENDING_EVENTS)),
        max_file_bytes=int(_get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)),
        max_file_age_seconds=int(_get("max_file_age_seconds", DEFAULT_MAX_FILE_AGE_SECONDS)),
        file_mode=int(file_mode_raw, 8) if isinstance(file_mode_raw, str) else file_mode_raw,
        directory_mode=int(dir_mode_raw, 8) if isinstance(dir_mode_raw, str) else dir_mode_raw,
        capture_root=str(_get("capture_root", DEFAULT_CAPTURE_ROOT)),
        policy_version=str(_get("policy_version", DEFAULT_POLICY_VERSION)),
        instance_id=str(_get("instance_id", str(uuid.uuid4()))),
    )

    logger.info(
        "Capture config loaded: enabled=%s, local=%s, cloud=%s, cloud_allowlist=%s, "
        "allowed_cloud_models=%d, per_client_opt_in=%s, policy_version=%s",
        config.enabled,
        config.local_capture,
        config.cloud_capture,
        config.cloud_allowlist_enabled,
        len(config.allowed_cloud_models),
        config.per_client_opt_in,
        config.policy_version,
    )
    return config
