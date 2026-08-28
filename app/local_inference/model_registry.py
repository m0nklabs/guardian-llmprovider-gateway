"""Local model registry + choice/discovery.

Phase F4 (GATEWAY_MANAGER_SPLIT step 0): the registry/choice/discovery
concerns formerly embedded in ``app.engine.manager.ModelManager`` now live
here in a standalone :class:`ModelRegistry`. It is the owner of the local
model registry data (``models`` / ``config_path`` / the vision-capability
cache) and all choice/discovery logic, and is used by both ``ModelManager``
(lifecycle) and the gateway modules.

Pure structural refactor — no behaviour change. Bodies are moved verbatim.
The runtime state that ``ModelManager`` owns (``current_model`` /
``current_vision_enabled``, the pinned/verified/backend model bookkeeping,
and the live launch-args file path) is read through the bound owner
(``bind_runtime_state``) so this module observes the manager's authoritative
state and honors test monkeypatching of
``app.engine.manager.CURRENT_MODEL_ARGS_FILE``.
"""

import copy
import logging
import re
import yaml
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.paths import local_models_file

logger = logging.getLogger("model-registry")

MISMATCH_MODEL_NAME = "__MISMATCH__"


@dataclass
class VisionCapability:
    """Runtime multimodal capability state for a configured model."""
    configured: bool
    mmproj: Optional[str]
    mmproj_exists: bool
    status: str
    signature: Tuple[str, str]
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "mmproj": self.mmproj,
            "mmproj_exists": self.mmproj_exists,
            "status": self.status,
            "validated": self.status in {"supported", "unsupported", "loading", "load_failed", "misconfigured"},
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
        }


class ModelRegistry:
    """Registry of local models + the choice/discovery logic built on it.

    Owns the local ``models`` dict, the ``config_path`` it loads from, and the
    vision-capability cache. Runtime-dependent helpers read the lifecycle
    ``ModelManager``'s authoritative state (``current_model`` /
    ``current_vision_enabled`` / pinned / verified / backend bookkeeping /
    launch-args path) through the bound owner set by :meth:`bind_runtime_state`.
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = str(local_models_file())
        self.config_path = Path(config_path)
        # Runtime state mirror (the authoritative values live on the bound
        # lifecycle owner; these provide a sane standalone default).
        self._current_model: Optional[str] = None
        self._current_vision_enabled: bool = False
        self._owner: Optional[Any] = None
        self.models = self._load_config()
        self._vision_capabilities: Dict[str, VisionCapability] = {}
        self._sync_vision_capabilities()

    def bind_runtime_state(self, owner: Any) -> None:
        """Attach the lifecycle ``ModelManager`` so runtime-dependent helpers
        read the manager's authoritative runtime state (``current_model`` /
        ``current_vision_enabled`` / pinned / verified / backend bookkeeping /
        live launch-args path). Called once by ``ModelManager.__init__``."""
        self._owner = owner

    # -- runtime state accessors (read the authoritative bound owner) --------
    @property
    def _current_model_value(self) -> Optional[str]:
        if self._owner is not None:
            return self._owner.current_model
        return self._current_model

    @property
    def _current_vision_enabled_value(self) -> bool:
        if self._owner is not None:
            return self._owner.current_vision_enabled
        return self._current_vision_enabled

    def _read_launch_args(self) -> str:
        """Return the current launch-args file text, read through the bound
        owner (which reads the monkeypatchable ``app.engine.manager`` global)."""
        if self._owner is not None:
            return self._owner._read_launch_args_file()
        raise FileNotFoundError("no bound runtime owner (launch args unavailable)")

    # ------------------------------------------------------------------------

    def _load_config(self) -> Dict:
        if not self.config_path.exists():
            logger.warning(f"Config not found at {self.config_path}")
            return {}
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f).get("models", {})

    def _refresh_model_registry(self) -> None:
        """Reload models.yaml-derived registry state for hot config edits."""
        self.models = self._load_config()
        self._sync_vision_capabilities()

    def _load_aliases(self) -> Dict[str, str]:
        """Load model aliases from models.yaml aliases section."""
        try:
            with open(self.config_path, "r") as f:
                cfg = yaml.safe_load(f)
            aliases = cfg.get("aliases", {})
            if aliases:
                logger.info(f"🏷️  Loaded {len(aliases)} model aliases")
            return aliases
        except Exception:
            return {}

    def resolve_model(self, name: str) -> str:
        """Resolve a model name or alias to the canonical model name.

        Lookup order:
        1. Exact match in models dict
        2. Alias lookup from models.yaml aliases section
        3. Case-insensitive match against model names
        Raises ValueError if not found.
        """
        self._refresh_model_registry()

        # 1. Exact match
        if name in self.models:
            return name

        # 2. Alias lookup
        aliases = self._load_aliases()
        if name in aliases:
            target = aliases[name]
            if target in self.models:
                logger.info(f"🏷️  Resolved alias {name!r} → {target!r}")
                return target
            logger.warning(f"⚠️ Alias {name!r} points to {target!r} which is not in models config")

        # 3. Case-insensitive fallback
        name_lower = name.lower()
        for model_name in self.models:
            if model_name.lower() == name_lower:
                logger.info(f"🏷️  Resolved case-insensitive {name!r} → {model_name!r}")
                return model_name

        raise ValueError(f"Model '{name}' not found in configuration (no alias match)")

    def resolve_reload_target(self, requested_model: Optional[str] = None) -> str:
        """Return a configured model that is safe to use for backend reloads."""
        self._refresh_model_registry()

        candidates: List[Optional[str]] = []
        if self._owner is not None and self._owner._pinned_model:
            candidates.append(self._owner._pinned_model)
        if requested_model and requested_model not in {"auto", MISMATCH_MODEL_NAME}:
            try:
                candidates.append(self.resolve_model(requested_model))
            except ValueError:
                pass
        candidates.extend(
            [
                self._current_model_value,
                self._owner._last_verified_model if self._owner is not None else None,
                self._owner._last_backend_model if self._owner is not None else None,
            ]
        )

        for candidate in candidates:
            if candidate and candidate != MISMATCH_MODEL_NAME and candidate in self.models:
                return candidate

        for candidate in self.models:
            if candidate != MISMATCH_MODEL_NAME:
                return candidate

        raise ValueError("No configured model is available for backend reload")

    def _uses_reasoning(self, config: Dict) -> bool:
        extra_args = str(config.get("extra_args", ""))
        if "--reasoning off" in extra_args:
            return False
        return "--reasoning on" in extra_args or self._reasoning_budget(config) is not None

    def _reasoning_budget(self, config: Dict) -> Optional[int]:
        extra_args = str(config.get("extra_args", ""))
        match = re.search(r"--reasoning-budget(?:=|\s+)(-?\d+)", extra_args)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _resolve_vision_mmproj(self, config: Dict[str, Any]) -> Optional[str]:
        """Return the mmproj path used for vision runtime, if any."""
        mmproj = str(config.get("vision_mmproj") or config.get("mmproj") or "").strip()
        return mmproj or None

    def _resolve_runtime_value(self, config: Dict[str, Any], key: str, *, enable_vision: bool) -> Any:
        """Return the effective runtime value for text or vision mode."""
        override_key = f"vision_{key}" if enable_vision else f"text_{key}"
        override_value = config.get(override_key)
        if override_value not in (None, ""):
            return override_value
        return config.get(key)

    def _resolve_runtime_vision_flag(self, model_name: str, enable_vision: Optional[bool]) -> bool:
        """Resolve whether a load/switch should start the model with mmproj."""
        config = self.models.get(model_name, {})
        if not self._resolve_vision_mmproj(config):
            return False
        if enable_vision is None:
            if model_name == self._current_model_value:
                return self._current_vision_enabled_value
            return False
        return bool(enable_vision)

    def build_runtime_config(
        self,
        model_name: str,
        *,
        enable_vision: Optional[bool] = None,
        context_hint: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build the effective runtime config for text or vision mode."""
        if model_name not in self.models:
            raise ValueError(f"Model {model_name} not found in configuration")

        runtime_config = copy.deepcopy(self.models[model_name])
        vision_enabled = self._resolve_runtime_vision_flag(model_name, enable_vision)

        runtime_config["context"] = self._resolve_runtime_value(runtime_config, "context", enable_vision=vision_enabled)
        runtime_config["ngl"] = self._resolve_runtime_value(runtime_config, "ngl", enable_vision=vision_enabled)

        # Client context hint: clamp to a safe range. Always cap at the configured
        # context (never enlarge beyond config — clients can't grow the model's KV).
        # Floor at 4096 (llama-server requires a sane minimum).
        if context_hint is not None:
            cfg_ctx = runtime_config.get("context") or 4096
            hinted = max(4096, min(int(context_hint), int(cfg_ctx)))
            runtime_config["context"] = hinted

        tensor_split = self._resolve_runtime_value(runtime_config, "tensor_split", enable_vision=vision_enabled)
        if tensor_split not in (None, ""):
            runtime_config["tensor_split"] = tensor_split
        else:
            runtime_config.pop("tensor_split", None)

        if vision_enabled:
            mmproj = self._resolve_vision_mmproj(runtime_config)
            if mmproj:
                runtime_config["mmproj"] = mmproj
        else:
            runtime_config.pop("mmproj", None)

        return runtime_config

    def _build_crash_config_snapshot(
        self,
        model_name: str,
        *,
        runtime_config: Optional[Dict[str, Any]] = None,
        vision_enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Capture both the configured profile and the resolved runtime shape for crash reports."""
        snapshot = copy.deepcopy(self.models.get(model_name, {}))
        if vision_enabled is not None:
            snapshot["runtime_mode"] = "vision" if vision_enabled else "text"
        if runtime_config is not None:
            snapshot["effective_runtime_config"] = copy.deepcopy(runtime_config)
        return snapshot

    def current_runtime_uses_mmproj(self, model_name: Optional[str] = None) -> bool:
        """Return whether the current backend args include the model's mmproj path."""
        target = model_name or self._current_model_value
        config = self.models.get(target, {})
        mmproj_candidates = [self._resolve_vision_mmproj(config)]
        base_mmproj = str(config.get("mmproj", "")).strip() or None
        if base_mmproj and base_mmproj not in mmproj_candidates:
            mmproj_candidates.append(base_mmproj)

        try:
            args = self._read_launch_args()
        except Exception:
            return self._current_vision_enabled_value if target == self._current_model_value else False

        return any(candidate and candidate in args for candidate in mmproj_candidates)

    def current_launch_context(self) -> Optional[int]:
        """Return the context window (-c) the currently running backend was launched
        with, or None if the args file is unreadable/unparsable. Convenience accessor
        for the client context-hint feature (lets clients discover the active ctx)."""
        try:
            args = self._read_launch_args()
        except Exception:
            return None
        match = re.search(r"-c (\d+)", args)
        if not match:
            return None
        return int(match.group(1))

    def _is_tool_friendly_config(self, config: Dict) -> bool:
        extra_args = str(config.get("extra_args", ""))
        profile_role = str(config.get("profile_role", "")).strip().lower()
        return (
            config.get("tool_profile") is True
            or profile_role in {"agent", "tool", "tool_agent"}
            or "chat-template-file" in extra_args
            or not self._uses_reasoning(config)
        )

    def _matching_model_candidates(self, model_name: str) -> List[str]:
        config = self.models.get(model_name, {})
        if not config:
            return []

        path = config.get("path")
        mmproj = self._resolve_vision_mmproj(config)
        candidates: List[str] = []
        for candidate_name, candidate_cfg in self.models.items():
            if candidate_name == model_name:
                continue
            if candidate_cfg.get("path") != path:
                continue
            if self._resolve_vision_mmproj(candidate_cfg) != mmproj:
                continue
            candidates.append(candidate_name)
        return candidates

    def _sort_preferred_candidates(self, model_names: List[str]) -> List[str]:
        def sort_key(name: str):
            cfg = self.models.get(name, {})
            context = self.get_runtime_context_window(name) or 0
            budget = self._reasoning_budget(cfg)
            bounded_reasoning = budget is not None and budget > 0
            return (
                0 if "Agent" in name else 1,
                0 if self._is_tool_friendly_config(cfg) else 1,
                0 if bounded_reasoning else 1,
                budget if bounded_reasoning else 999999,
                -context,
                name,
            )

        return sorted(model_names, key=sort_key)

    def get_preferred_tool_model(self, model_name: Optional[str] = None) -> Optional[str]:
        """Return a tool-friendly sibling profile for a model family when available."""
        target = model_name or self._current_model_value
        config = self.models.get(target)
        if not config:
            return None
        if self._is_tool_friendly_config(config):
            return target

        candidates = [
            name for name in self._matching_model_candidates(target)
            if self._is_tool_friendly_config(self.models.get(name, {}))
        ]
        if not candidates:
            return target
        return self._sort_preferred_candidates(candidates)[0]

    def get_preferred_reasoning_model(self, model_name: Optional[str] = None) -> Optional[str]:
        """Return the deepest reasoning-capable sibling profile for a model family."""
        target = model_name or self._current_model_value
        config = self.models.get(target)
        if not config:
            return None
        if self._uses_reasoning(config) and self._reasoning_budget(config) == -1:
            return target

        candidates = [
            name for name in self._matching_model_candidates(target)
            if self._uses_reasoning(self.models.get(name, {}))
        ]
        if not candidates:
            return target

        def sort_key(name: str):
            cfg = self.models.get(name, {})
            extra_args = str(cfg.get("extra_args", ""))
            context = self.get_runtime_context_window(name) or 0
            unbounded_reasoning = "--reasoning-budget -1" in extra_args
            return (
                0 if unbounded_reasoning else 1,
                -context,
                name,
            )

        return sorted(candidates, key=sort_key)[0]

    def get_advertised_context_window(self, model_name: str) -> Optional[int]:
        """Return a conservative context window to advertise to clients.

        Use the active runtime profile size only, then reserve a small headroom
        buffer so clients compact before hitting the llama.cpp hard limit.

        The separate benchmark_context_limit value in models.yaml is treated as
        a benchmark or paper ceiling, not as part of Guardian's runtime sizing
        logic.
        """
        config = self.models.get(model_name, {})
        runtime_context = self.get_runtime_context_window(model_name)

        if runtime_context is None:
            return None

        advertised_override = config.get("advertised_context")
        if isinstance(advertised_override, int) and advertised_override > 0:
            return min(advertised_override, runtime_context)

        headroom = max(1024, min(4096, runtime_context // 32))
        return max(1024, runtime_context - headroom)

    def get_runtime_context_window(self, model_name: str) -> Optional[int]:
        """Return the active runtime profile context for a model, if set."""
        config = self.models.get(model_name, {})
        vision_enabled = model_name == self._current_model_value and self._current_vision_enabled_value
        configured_context = self._resolve_runtime_value(
            config,
            "context",
            enable_vision=vision_enabled,
        )
        if configured_context is None:
            configured_context = config.get("ctx")
        if isinstance(configured_context, int) and configured_context > 0:
            return configured_context
        return None

    def get_benchmark_context_limit(self, model_name: str) -> Optional[int]:
        """Return the non-runtime benchmark ceiling from models.yaml.

        This mirrors the config's benchmark_context_limit semantics: the paper
        or tested upper bound where further benchmark attempts stop being useful.
        Guardian should not treat it as the active runtime context.
        """
        config = self.models.get(model_name, {})
        benchmark_context_limit = config.get("benchmark_context_limit")
        if isinstance(benchmark_context_limit, int) and benchmark_context_limit > 0:
            return benchmark_context_limit
        return None

    def get_public_model_map(self) -> Dict[str, str]:
        """Return public model IDs mapped to their canonical model names.

        Include both canonical model names and valid aliases so OpenAI-compatible
        clients can look up metadata using the exact ID they use
        for inference requests.
        """
        self._refresh_model_registry()
        public_models: Dict[str, str] = {name: name for name in self.models}

        for alias, target in self._load_aliases().items():
            if alias in public_models:
                continue
            if target not in self.models:
                logger.warning(f"⚠️ Skipping alias '{alias}' in public model list; target '{target}' not found")
                continue
            public_models[alias] = target

        return public_models

    def _vision_signature(self, config: Dict) -> Tuple[str, str]:
        return (
            str(config.get("path", "")).strip(),
            str(self._resolve_vision_mmproj(config) or "").strip(),
        )

    def _sync_vision_capabilities(self) -> None:
        """Refresh cached multimodal capability state from the current config."""
        previous = getattr(self, "_vision_capabilities", {})
        refreshed: Dict[str, VisionCapability] = {}

        for model_name, config in self.models.items():
            mmproj = self._resolve_vision_mmproj(config)
            signature = self._vision_signature(config)
            existing = previous.get(model_name)

            if not mmproj:
                refreshed[model_name] = VisionCapability(
                    configured=False,
                    mmproj=None,
                    mmproj_exists=False,
                    status="text_only",
                    signature=signature,
                )
                continue

            mmproj_exists = Path(mmproj).exists()
            if not mmproj_exists:
                refreshed[model_name] = VisionCapability(
                    configured=True,
                    mmproj=mmproj,
                    mmproj_exists=False,
                    status="misconfigured",
                    signature=signature,
                    last_error=f"mmproj file not found: {mmproj}",
                )
                continue

            if existing and existing.signature == signature and existing.status in {"supported", "unsupported", "loading", "load_failed"}:
                refreshed[model_name] = VisionCapability(
                    configured=True,
                    mmproj=mmproj,
                    mmproj_exists=True,
                    status=existing.status,
                    signature=signature,
                    last_checked_at=existing.last_checked_at,
                    last_error=existing.last_error,
                )
                continue

            refreshed[model_name] = VisionCapability(
                configured=True,
                mmproj=mmproj,
                mmproj_exists=True,
                status="unverified",
                signature=signature,
            )

        self._vision_capabilities = refreshed

    def get_vision_capability(self, model_name: str) -> Dict[str, Any]:
        """Return multimodal capability metadata for a configured model."""
        capability = self._vision_capabilities.get(model_name)
        if capability is None:
            return VisionCapability(
                configured=False,
                mmproj=None,
                mmproj_exists=False,
                status="unknown",
                signature=("", ""),
            ).to_dict()
        return capability.to_dict()

    def reset_vision_validation(self, model_name: str) -> None:
        """Reset runtime validation after a fresh backend load or switch."""
        capability = self._vision_capabilities.get(model_name)
        if capability is None:
            return
        if not capability.configured:
            capability.status = "text_only"
            capability.last_error = None
            capability.last_checked_at = None
            return
        if not capability.mmproj_exists:
            capability.status = "misconfigured"
            capability.last_error = f"mmproj file not found: {capability.mmproj}"
            capability.last_checked_at = None
            return
        capability.status = "unverified"
        capability.last_error = None
        capability.last_checked_at = None

    def mark_vision_validation(self, model_name: str, status: str, error: Optional[str] = None) -> None:
        """Persist the latest observed runtime multimodal state for a model."""
        capability = self._vision_capabilities.get(model_name)
        if capability is None:
            return

        checked_at = datetime.now(UTC).isoformat()
        if not capability.configured:
            capability.status = "text_only"
            capability.last_error = error
            capability.last_checked_at = checked_at
            return
        if not capability.mmproj_exists:
            capability.status = "misconfigured"
            capability.last_error = f"mmproj file not found: {capability.mmproj}"
            capability.last_checked_at = checked_at
            return

        capability.status = status
        capability.last_error = error
        capability.last_checked_at = checked_at
