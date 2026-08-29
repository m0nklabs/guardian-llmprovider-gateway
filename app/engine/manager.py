import asyncio
import copy
import hashlib
import json
import logging
import math
import subprocess
import yaml
import time
import re
import shlex
import httpx
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass
from datetime import UTC, datetime

from app.local_inference.model_registry import (
    MISMATCH_MODEL_NAME,
    ModelRegistry,
    VisionCapability,
)
from app.paths import (
    CURRENT_MODEL_ARGS_FILE,
    CURRENT_MODEL_ENV_FILE,
    CURRENT_MODEL_SIG_FILE,
    LLAMA_SLOTS_DIR,
    OFFICIAL_LLAMA_SERVER_BIN,
    global_settings_file,
    local_models_file,
)

logger = logging.getLogger("model-manager")

MAX_CRASH_HISTORY = 50  # Keep last N crash records


@dataclass
class CrashRecord:
    """Record of a llama-server crash event."""
    timestamp: str
    model: str
    error_message: str
    exit_code: Optional[int] = None
    config_snapshot: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
            "config_snapshot": self.config_snapshot,
        }


class ModelLoadError(Exception):
    """Raised when llama-server fails to load a model."""
    def __init__(self, message: str, crash_record: Optional[CrashRecord] = None):
        super().__init__(message)
        self.crash_record = crash_record


class ModelManager:
    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = str(local_models_file())
        self.config_path = Path(config_path)
        self.registry = ModelRegistry(config_path=config_path)
        self.registry.bind_runtime_state(self)
        # Mirror the registry's registry data so existing call-sites/tests that
        # read ModelManager.models / ModelManager._vision_capabilities keep working.
        # The delegators that drive a registry-level refresh (resolve_model /
        # resolve_reload_target / get_public_model_map) re-sync these mirrors via
        # _sync_registry_mirror() so a hot config edit never leaves them stale.
        self.models = self.registry.models
        self._vision_capabilities: Dict[str, VisionCapability] = self.registry._vision_capabilities
        self.server_process: Optional[int] = None # Systemd manages main process, but we might control it via systemctl
        self.server_url = "http://127.0.0.1:11440"
        self.crash_history: List[CrashRecord] = []
        self.last_crash: Optional[CrashRecord] = None

        # === SECURITY: Model pinning & switch protection ===
        self._pinned_model: Optional[str] = self._load_pinned_model()
        self._switch_allowlist: Set[str] = self._load_switch_allowlist()
        self._model_verified = False  # True after startup verification passes
        self._last_verification_at: Optional[str] = None
        self._last_successful_verification_at: Optional[str] = None
        self._last_verified_model: Optional[str] = None
        self._last_backend_model: Optional[str] = None
        self.current_vision_enabled = False

        # Initial model: use pinned model if set, otherwise fallback
        self.current_model = self._pinned_model or self._detect_initial_model()
        logger.info(f"📌 Initial model set to: {self.current_model}")
        self.current_vision_enabled = self.current_runtime_uses_mmproj(self.current_model)

        # === VRAM management: unload state and idle tracking ===
        self.is_unloaded: bool = False  # True when llama-server stopped to free VRAM
        self.last_request_time: float = time.time()  # Used for idle-unload timeout
        self.active_requests: int = 0  # Counter for in-flight requests (prevents idle-unload during streaming)

    # --- Registry composition: current runtime state + launch-args accessor.
    # ModelRegistry reads these through the bound owner (bind_runtime_state)
    # so it always observes the manager's authoritative current_model /
    # current_vision_enabled and the monkeypatchable CURRENT_MODEL_ARGS_FILE.
    def _read_launch_args_file(self) -> str:
        """Return the current launch-args file text (module attribute read at
        call time so tests can monkeypatch CURRENT_MODEL_ARGS_FILE)."""
        return CURRENT_MODEL_ARGS_FILE.read_text()

    # --- Registry delegators: same public names/signatures so gateway modules
    # and tests that call _model_manager.<method> keep working unchanged.
    def _sync_registry_mirror(self) -> None:
        """Re-point the manager's registry-data mirrors at the registry's
        current dicts. Called after any delegator that drives a registry-level
        refresh (resolve_model / resolve_reload_target / get_public_model_map),
        so a hot config edit never leaves ModelManager.models /
        _vision_capabilities stale (used by direct readers in gateway modules)."""
        self.models = self.registry.models
        self._vision_capabilities = self.registry._vision_capabilities

    def resolve_model(self, name: str) -> str:
        try:
            return self.registry.resolve_model(name)
        finally:
            self._sync_registry_mirror()

    def resolve_reload_target(self, requested_model: Optional[str] = None) -> str:
        try:
            return self.registry.resolve_reload_target(requested_model)
        finally:
            self._sync_registry_mirror()

    def get_preferred_tool_model(self, model_name: Optional[str] = None) -> Optional[str]:
        return self.registry.get_preferred_tool_model(model_name)

    def get_preferred_reasoning_model(self, model_name: Optional[str] = None) -> Optional[str]:
        return self.registry.get_preferred_reasoning_model(model_name)

    def get_advertised_context_window(self, model_name: str) -> Optional[int]:
        return self.registry.get_advertised_context_window(model_name)

    def get_runtime_context_window(self, model_name: str) -> Optional[int]:
        return self.registry.get_runtime_context_window(model_name)

    def get_benchmark_context_limit(self, model_name: str) -> Optional[int]:
        return self.registry.get_benchmark_context_limit(model_name)

    def get_public_model_map(self) -> Dict[str, str]:
        try:
            return self.registry.get_public_model_map()
        finally:
            self._sync_registry_mirror()

    def get_vision_capability(self, model_name: str) -> Dict[str, Any]:
        return self.registry.get_vision_capability(model_name)

    def reset_vision_validation(self, model_name: str) -> None:
        self.registry.reset_vision_validation(model_name)

    def mark_vision_validation(self, model_name: str, status: str, error: Optional[str] = None) -> None:
        self.registry.mark_vision_validation(model_name, status, error)

    def current_runtime_uses_mmproj(self, model_name: Optional[str] = None) -> bool:
        return self.registry.current_runtime_uses_mmproj(model_name)

    def current_launch_context(self) -> Optional[int]:
        return self.registry.current_launch_context()

    def build_runtime_config(
        self,
        model_name: str,
        *,
        enable_vision: Optional[bool] = None,
        context_hint: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.registry.build_runtime_config(
            model_name, enable_vision=enable_vision, context_hint=context_hint
        )

    def _build_crash_config_snapshot(
        self,
        model_name: str,
        *,
        runtime_config: Optional[Dict[str, Any]] = None,
        vision_enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return self.registry._build_crash_config_snapshot(
            model_name, runtime_config=runtime_config, vision_enabled=vision_enabled
        )

    def _resolve_runtime_vision_flag(self, model_name: str, enable_vision: Optional[bool]) -> bool:
        return self.registry._resolve_runtime_vision_flag(model_name, enable_vision)

    def _resolve_runtime_value(self, config: Dict[str, Any], key: str, *, enable_vision: bool) -> Any:
        return self.registry._resolve_runtime_value(config, key, enable_vision=enable_vision)

    def _sync_vision_capabilities(self) -> None:
        self.registry._sync_vision_capabilities()
        self._vision_capabilities = self.registry._vision_capabilities

    def _uses_reasoning(self, config: Dict) -> bool:
        return self.registry._uses_reasoning(config)

    def _reasoning_budget(self, config: Dict) -> Optional[int]:
        return self.registry._reasoning_budget(config)

    def _load_aliases(self) -> Dict[str, str]:
        return self.registry._load_aliases()

    def _refresh_model_registry(self) -> None:
        """Reload registry state and re-sync the manager's mirrors so both stay
        in lock-step after a hot config edit."""
        self.registry._refresh_model_registry()
        self.models = self.registry.models
        self._vision_capabilities = self.registry._vision_capabilities

    # --- Pinned model config (persisted in models.yaml under 'guardian:') ---
    def _load_pinned_model(self) -> Optional[str]:
        """Load pinned_model from models.yaml guardian section."""
        try:
            with open(self.config_path, "r") as f:
                cfg = yaml.safe_load(f)
            pinned = cfg.get("guardian", {}).get("pinned_model")
            if pinned:
                logger.info(f"🔒 Model pin active: {pinned}")
            return pinned
        except Exception:
            return None

    def _load_switch_allowlist(self) -> Set[str]:
        """Load set of client names allowed to trigger model switches."""
        try:
            with open(self.config_path, "r") as f:
                cfg = yaml.safe_load(f)
            allowlist = cfg.get("guardian", {}).get("switch_allowlist", [])
            if allowlist:
                logger.info(f"🔑 Switch allowlist: {allowlist}")
            return set(allowlist)
        except Exception:
            return set()

    @property
    def pinned_model(self) -> Optional[str]:
        return self._pinned_model

    def _detect_initial_model(self) -> str:
        """Detect which model the backend is running by reading current_model.args.
        Falls back to first model in config if detection fails.
        """
        # Prefer the persisted launch signature: it names the last-launched model
        # authoritatively (arg-scoring is fragile when the config changed).
        persisted = self._read_persisted_signature()
        if persisted and persisted.get("model") in self.models:
            logger.info(f"🔍 Detected last-launched model from signature file: {persisted['model']}")
            return persisted["model"]

        try:
            args_file = self.config_path.parent / "current_model.args"
            if args_file.exists():
                args = args_file.read_text().strip()
                args_tokens = set(shlex.split(args))
                candidates = []
                for model_name, config in self.models.items():
                    model_path = config.get("path")
                    if not model_path or model_path not in args:
                        continue

                    score = 1
                    context = str(config.get("context", config.get("ctx", 4096)))
                    tensor_split = str(config.get("tensor_split", "")).strip()
                    mmproj = str(config.get("mmproj", "")).strip()
                    extra_args = str(config.get("extra_args", "")).strip()

                    if f"-c {context}" in args:
                        score += 2
                    if tensor_split and f"--tensor-split {tensor_split}" in args:
                        score += 2
                    if mmproj and mmproj in args:
                        score += 2
                    if extra_args:
                        extra_tokens = shlex.split(extra_args)
                        if all(token in args_tokens for token in extra_tokens):
                            score += 10 + len(extra_tokens)
                        else:
                            score -= 2
                    else:
                        score += 1

                    candidates.append((score, len(extra_args), model_name))

                if candidates:
                    _, _, detected_model = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
                    logger.info(f"🔍 Detected running model from args file: {detected_model}")
                    return detected_model
        except Exception as e:
            logger.warning(f"Failed to detect initial model: {e}")
        # Fallback: first model in config
        fallback = next(iter(self.models.keys()), "unknown")
        logger.warning(f"⚠️ Could not detect running model, falling back to: {fallback}")
        return fallback

    def is_switch_allowed(self, client_id: str) -> bool:
        """Check if a client is allowed to trigger model switches.
        If no allowlist is configured, all clients can switch (backward compat).
        If allowlist exists, only listed clients can switch.
        """
        self._switch_allowlist = self._load_switch_allowlist()
        if not self._switch_allowlist:
            return True  # No allowlist = unrestricted (backward compat)
        return client_id in self._switch_allowlist

    async def verify_backend_model(self) -> bool:
        """SECURITY: Verify the actual running llama-server model matches what Guardian thinks.

        Checks the llama-server process commandline to extract the real .gguf path,
        then matches it against the expected model config.
        Returns True if match, False if mismatch detected.
        """
        try:
            actual_gguf = self._get_backend_model_path()
            if not actual_gguf:
                logger.warning("⚠️ Could not detect running backend model (no llama-server process?)")
                return False

            expected_config = self.models.get(self.current_model, {})
            expected_gguf = expected_config.get("path", "")

            if actual_gguf == expected_gguf:
                logger.info(f"✅ Backend model verified: {self.current_model} ({Path(actual_gguf).name})")
                self._model_verified = True
                self._last_verification_at = datetime.now(UTC).isoformat()
                self._last_successful_verification_at = self._last_verification_at
                self._last_verified_model = self.current_model
                self._last_backend_model = self.current_model
                return True
            else:
                # MISMATCH — find which model is actually loaded
                actual_model_name = self._identify_model_by_path(actual_gguf)
                logger.error(
                    f"🚨 MODEL MISMATCH! Guardian thinks: {self.current_model} "
                    f"but backend runs: {actual_model_name or 'UNKNOWN'} ({Path(actual_gguf).name})"
                )
                self._model_verified = False
                self._last_verification_at = datetime.now(UTC).isoformat()
                self._last_backend_model = actual_model_name
                return False
        except Exception as e:
            logger.error(f"❌ Backend verification failed: {e}")
            self._last_verification_at = datetime.now(UTC).isoformat()
            return False

    def _get_backend_model_path(self) -> Optional[str]:
        """Extract the .gguf model path from the running llama-server process."""
        try:
            result = subprocess.run(
                ["pgrep", "-a", "llama-server"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return None
            
            for line in result.stdout.strip().splitlines():
                # Parse "-m /path/to/model.gguf" from commandline
                match = re.search(r'-m\s+(\S+\.gguf)', line)
                if match:
                    return match.group(1)
            return None
        except Exception:
            return None

    def _identify_model_by_path(self, gguf_path: str) -> Optional[str]:
        """Reverse-lookup: find model name by its .gguf path."""
        for name, cfg in self.models.items():
            if cfg.get("path") == gguf_path:
                return name
        return None

    async def startup_check(self):
        """Run on Guardian startup: verify backend or force correct model.
        
        Called from server.py lifespan. If the backend runs the wrong model,
        this triggers a forced switch to the pinned/default model.
        """
        target = self.resolve_reload_target(self._pinned_model or self.current_model)
        logger.info(f"🔍 Startup check: expecting model '{target}'")

        # If the running backend's persisted signature doesn't match current models.yaml,
        # force a reload even if the backend process looks alive (settings changed).
        startup_drift = self._config_drifted(target, enable_vision=self.current_vision_enabled)
        if startup_drift:
            logger.info(
                f"🔄 Startup config drift for '{target}' — forcing reload to apply new models.yaml settings"
            )

        verified = await self.verify_backend_model()
        if verified and not startup_drift:
            logger.info(f"✅ Startup check passed — backend matches '{self.current_model}'")
            return

        # Backend mismatch detected — force switch
        actual_gguf = self._get_backend_model_path()
        actual_name = self._identify_model_by_path(actual_gguf) if actual_gguf else "NONE"
        logger.warning(
            f"🔄 Startup mismatch: forcing switch from actual '{actual_name}' to target '{target}'"
        )

        if not self._pinned_model and actual_name in self.models and not startup_drift:
            logger.warning(
                "🔄 Startup mismatch has a known live backend and no model pin; "
                "adopting '%s' instead of replacing it with stale args state",
                actual_name,
            )
            self.current_model = actual_name
            self.current_vision_enabled = self.current_runtime_uses_mmproj(actual_name)
            if await self.verify_backend_model():
                logger.info(f"✅ Startup adopted live backend '{actual_name}'")
                return

        self.current_model = MISMATCH_MODEL_NAME  # Force switch_model to not skip

        try:
            await self.switch_model(target)
            logger.info(f"✅ Startup forced switch to '{target}' succeeded")
        except Exception as e:
            self.current_model = target
            self.current_vision_enabled = self.current_runtime_uses_mmproj(target)
            self.is_unloaded = True
            logger.error(f"❌ Startup forced switch FAILED: {e}")

    async def get_current_model(self) -> str:
        # We can implement a health check or store internal state
        return self.current_model

    async def backend_serves_model(self, model_name: str) -> bool:
        """F5: True when the running llama-server actually serves ``model_name``.

        Unlike ``verify_backend_model`` (which checks against
        ``self.current_model``), this compares the running backend's .gguf
        against the requested model's config path — so the gateway can
        distinguish "backend is up" from "backend is up AND serving the model
        a request needs" after a caretaker outage, without adopting a wrong
        loaded-state.
        """
        actual_gguf = self._get_backend_model_path()
        if not actual_gguf:
            return False
        expected = self.models.get(model_name, {}).get("path", "")
        if not expected:
            return False
        return Path(actual_gguf).resolve() == Path(expected).resolve()


    async def backend_health_ok(self) -> bool:
        """Return True when the managed llama-server backend accepts requests."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.server_url}/health")
            return resp.status_code == 200
        except httpx.ConnectError:
            return False
        except Exception as e:
            logger.debug(f"Backend health probe failed: {e}")
            return False

    async def switch_model(
        self,
        model_name: str,
        client_id: str = "_system",
        force: bool = False,
        enable_vision: Optional[bool] = None,
        context_hint: Optional[int] = None,
    ):
        # Re-read models.yaml so config edits take effect without Guardian restart
        self._refresh_model_registry()
        if model_name not in self.models:
            raise ValueError(f"Model {model_name} not found in configuration")

        # SECURITY: Pinned model protection
        # Allowlisted clients can override the pin (they're trusted)
        client_can_override = self.is_switch_allowed(client_id)
        if self._pinned_model and model_name != self._pinned_model and not force and not client_can_override:
            logger.warning(
                f"🔒 BLOCKED: Client '{client_id}' tried to switch to '{model_name}' "
                f"but model is pinned to '{self._pinned_model}'. Use force=True or unpin first."
            )
            raise ValueError(
                f"Model switch blocked: '{self._pinned_model}' is pinned. "
                f"Remove guardian.pinned_model from models.yaml to allow switches."
            )
        if self._pinned_model and model_name != self._pinned_model and client_can_override:
            logger.info(
                f"🔓 Allowlisted client '{client_id}' overriding pin "
                f"('{self._pinned_model}' → '{model_name}')"
            )

        desired_vision = self._resolve_runtime_vision_flag(model_name, enable_vision)
        current_vision = self.current_vision_enabled if model_name == self.current_model else self.current_runtime_uses_mmproj(model_name)

        drifted = self._config_drifted(model_name, enable_vision=desired_vision, context_hint=context_hint)
        if model_name == self.current_model and desired_vision == current_vision and not drifted:
            logger.info(f"Model {model_name} is already active")
            return
        if drifted:
            logger.info(
                f"🔄 Config drift detected for '{model_name}' "
                "(settings changed in models.yaml) — reloading to apply new settings"
            )

        logger.info(
            "Switching from %s [%s] to %s [%s]",
            self.current_model,
            "vision" if self.current_vision_enabled else "text",
            model_name,
            "vision" if desired_vision else "text",
        )
        
        # 1. Auto-save current context
        await self._save_context(f"auto_save_{self.current_model}")

        # 2. Stop llama-server
        await self._stop_server()

        # 3. Write new model args + binary selection
        target_config = self.build_runtime_config(
            model_name, enable_vision=desired_vision, context_hint=context_hint
        )
        logger.info(
            "Runtime config for %s [%s]: context=%s ngl=%s split=%s mmproj=%s",
            model_name,
            "vision" if desired_vision else "text",
            target_config.get("context"),
            target_config.get("ngl"),
            target_config.get("tensor_split") or "auto",
            target_config.get("mmproj") or "none",
        )
        self._write_server_args(target_config)
        
        # 4. Free GPU memory (kill non-Frigate processes)
        await self._free_gpu_memory()

        # 5. Start llama-server
        await self._start_server()
        
        # 6. Wait for health with crash detection
        healthy = await self._wait_for_health(model_name)
        
        if not healthy:
            # Server crashed or failed to start — record and raise
            crash = await self._detect_crash(
                model_name,
                config_snapshot=self._build_crash_config_snapshot(
                    model_name,
                    runtime_config=target_config,
                    vision_enabled=desired_vision,
                ),
            )
            raise ModelLoadError(
                f"Model '{model_name}' failed to load: {crash.error_message}",
                crash_record=crash,
            )
        
        self.current_model = model_name
        self.current_vision_enabled = desired_vision
        # Persist the launch signature so future same-model requests can detect
        # config drift and reload instead of skipping.
        launch_sig = self._compute_launch_signature(
            model_name, enable_vision=desired_vision, context_hint=context_hint
        )
        if launch_sig is not None:
            self._write_persisted_signature(launch_sig)
        self.reset_vision_validation(model_name)
        logger.info(f"✅ Model '{model_name}' loaded successfully")

        # SECURITY: Post-switch verification — confirm backend actually loaded right model
        if not await self.verify_backend_model():
            logger.error(f"🚨 POST-SWITCH VERIFICATION FAILED for '{model_name}'!")
        
        # 7. Restore context if exists
        try:
             await self._load_context(f"auto_save_{model_name}")
        except Exception:
             logger.info(f"No auto-save found for {model_name}, starting fresh.")

    @property
    def idle_unload_minutes(self) -> Optional[float]:
        """Return idle_unload_minutes from guardian config, or None if disabled."""
        try:
            with open(self.config_path, 'r') as f:
                raw = yaml.safe_load(f)
            return raw.get('guardian', {}).get('idle_unload_minutes', None)
        except Exception:
            return None

    async def unload(self) -> None:
        """Stop llama-server to free all VRAM. Guard against double-unload."""
        if self.is_unloaded:
            logger.info("⚡ Already unloaded — nothing to do")
            return
        logger.info(f"🔌 Unloading model '{self.current_model}' to free VRAM...")
        await self._stop_server()
        self.is_unloaded = True
        logger.info("✅ llama-server stopped — VRAM is free")

    def mark_unloaded_by_caretaker(self) -> None:
        """F5: the caretaker daemon confirmed an unload it performed itself.

        The gateway's own ``unload()`` used to be the only way the flag flipped;
        with the remote lifecycle split the caretaker can unload on its own (its
        own idle-unload, a direct operator call).  This method brings the
        gateway-local manager into the same end-state ``unload()`` would have
        produced — the model is no longer served (``is_unloaded``), its backend
        is no longer verified (``_model_verified``), so the hotpath auto-reload
        fires on the next request and a stale health/verification run does not
        treat the caretaker-killed process as a crash and respawn it.
        """
        self.is_unloaded = True
        self._model_verified = False
        self._last_verification_at = None
        self._last_backend_model = None

    def snapshot_unload_state(self) -> dict:
        """Capture the pre-unload state so an optimistic
        mark_unloaded_by_caretaker() can be fully rolled back."""
        return {
            "is_unloaded": self.is_unloaded,
            "model_verified": self._model_verified,
            "last_verification_at": self._last_verification_at,
            "last_backend_model": self._last_backend_model,
        }

    def rollback_unload_state(
        self,
        *,
        is_unloaded: bool,
        model_verified: bool,
        last_verification_at: str | None,
        last_backend_model: str | None,
    ) -> None:
        """F5: undo an optimistic mark_unloaded_by_caretaker() after the
        caretaker refused the unload (the backend was never stopped).  Restore
        the exact pre-mark state so nothing looks unloaded/unknown when nothing
        actually changed — otherwise a follow-up request or health check would
        trigger an avoidable reload or mismatch on a still-running model."""
        self.is_unloaded = is_unloaded
        self._model_verified = model_verified
        self._last_verification_at = last_verification_at
        self._last_backend_model = last_backend_model

    def rollback_unload_if_unchanged(self, prev_state: dict) -> bool:
        """F5: guarded rollback after a caretaker refusal.

        Only restore the pre-mark snapshot when the state still looks exactly
        like the optimistic mark set it (is_unloaded True, verification
        cleared).  A request that arrived during the round-trip may have
        started a reload which flips is_unloaded False / _model_verified True —
        in that case the snapshot is stale and must NOT clobber the fresh
        state.  Returns True when the rollback happened.
        """
        if (
            self.is_unloaded is True
            and self._model_verified is False
            and self._last_verification_at is None
            and self._last_backend_model is None
        ):
            self.rollback_unload_state(**prev_state)
            return True
        return False

    def mark_loaded_by_caretaker(
        self,
        model_name: str,
        enable_vision: bool | None = None,
        context_hint: int | None = None,
    ) -> None:
        """F5: the caretaker daemon confirmed (``POST /ensure`` 200) that
        ``model_name`` is loaded and healthy on the backend.

        The remote lifecycle split means the gateway no longer spawns
        llama-server itself on the happy path; this mirror brings the
        gateway-local manager into the same end-state ``load()`` /
        ``switch_model()`` would have produced — current model + vision flag
        updated, unloaded flag cleared, backend marked verified (the caretaker
        verified at ensure time), launch signature persisted so same-model
        context-hint caching and the drift check keep working, and the
        vision-validation cache reset.
        """
        self._refresh_model_registry()
        if model_name not in self.models:
            raise ValueError(f"Model '{model_name}' not found in configuration")
        # Same semantics as load()/switch_model(): resolve the vision flag with
        # the OLD active model still in place — enable_vision=None keeps the
        # current flag only when the target IS the already-active model, and
        # otherwise falls back to the target model's own default (False when it
        # has no mmproj). Copying current_vision_enabled blindly would stamp a
        # stale flag + launch signature onto a different model (e.g. the
        # connect-error recovery path switching to another model).
        desired_vision = self._resolve_runtime_vision_flag(model_name, enable_vision)
        self.current_model = model_name
        # Always set the vision flag: resolve already kept the previous flag
        # for the same model and fell back to the model default otherwise
        # (mirrors load()/switch_model()).
        self.current_vision_enabled = desired_vision
        self.is_unloaded = False
        self._model_verified = True
        self._last_backend_model = model_name
        launch_sig = self._compute_launch_signature(
            model_name, enable_vision=desired_vision, context_hint=context_hint
        )
        if launch_sig is not None:
            self._write_persisted_signature(launch_sig)
        self.reset_vision_validation(model_name)
        logger.info("F5: caretaker confirmed model '%s' loaded", model_name)

    async def save_current_context(self) -> None:
        """F5: persist the currently loaded model's session context (auto-save).

        The remote ``/ensure`` switch path no longer runs the local
        ``switch_model`` body (which owned the context auto-save); the gateway
        calls this before handing a switch to the caretaker so long-running
        sessions survive a remote model swap the same way they survive a local
        one. Tolerates a missing/corrupt save file (mirrors load()'s restore).
        """
        if not self.current_model or self.is_unloaded:
            return
        try:
            await self._save_context(f"auto_save_{self.current_model}")
        except Exception:  # noqa: BLE001 — save must never block a model switch
            logger.info("No auto-save found for %s, starting fresh.", self.current_model)

    async def _free_gpu_memory(self) -> None:
        """Ask coexisting GPU services to release VRAM before loading a model.

        Instead of killing processes, this asks services politely via their APIs:
        - ComfyUI: POST /free {"unload_models": true, "free_memory": true}
        - Frigate: NEVER touched (cameras are sacred)

        Any unknown GPU processes are logged but left alone.
        """
        logger.info("🧹 Requesting GPU memory release from coexisting services...")

        # Ask ComfyUI to unload models and free VRAM
        await self._request_comfyui_free()

        # Log remaining GPU consumers for visibility
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    logger.info(f"📊 GPU process: {line.strip()}")
        except Exception:
            pass

    def _get_comfyui_url(self) -> str:
        """Read ComfyUI URL from global.settings.yaml, fallback to default."""
        try:
            settings_path = global_settings_file()
            with open(settings_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("services", {}).get("comfyui_url", "http://127.0.0.1:8188")
        except Exception:
            return "http://127.0.0.1:8188"

    async def _request_comfyui_free(self) -> None:
        """Ask ComfyUI to unload all models and free GPU memory via its API."""
        comfyui_url = self._get_comfyui_url()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{comfyui_url}/free",
                    json={"unload_models": True, "free_memory": True},
                )
                if resp.status_code == 200:
                    logger.info("✅ ComfyUI released GPU memory (models unloaded)")
                    # Give CUDA a moment to actually release the memory
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"⚠️ ComfyUI /free returned HTTP {resp.status_code}")
        except httpx.ConnectError:
            logger.info("ℹ️ ComfyUI not running — no memory to free")
        except Exception as e:
            logger.warning(f"⚠️ Failed to request ComfyUI memory free: {e}")

    async def load(
        self,
        model_name: Optional[str] = None,
        enable_vision: Optional[bool] = None,
        runtime_overrides: Optional[Dict[str, Any]] = None,
        context_hint: Optional[int] = None,
    ) -> None:
        """Reload llama-server with current (or specified) model."""
        # Re-read models.yaml so config edits take effect without Guardian restart
        self._refresh_model_registry()
        target = model_name or self.current_model
        if target not in self.models:
            if model_name is None:
                target = self.resolve_reload_target()
            else:
                raise ValueError(f"Model '{target}' not found in configuration")
        if target not in self.models:
            raise ValueError(f"Model '{target}' not found in configuration")
        desired_vision = self._resolve_runtime_vision_flag(target, enable_vision)
        logger.info(f"🔄 Loading model '{target}' in {'vision' if desired_vision else 'text'} mode...")
        target_config = self.build_runtime_config(
            target, enable_vision=desired_vision, context_hint=context_hint
        )
        if runtime_overrides is not None:
            if not isinstance(runtime_overrides, dict):
                raise ValueError(
                    f"runtime_overrides must be an object/dict, got {type(runtime_overrides).__name__!r}"
                )
            runtime_total_layers: int | None = None
            total_layers_value = self._resolve_runtime_value(target_config, "total_layers", enable_vision=desired_vision)
            if total_layers_value not in (None, ""):
                try:
                    runtime_total_layers = int(total_layers_value)
                except (TypeError, ValueError):
                    runtime_total_layers = None
            allowed_keys = {"context", "ngl", "tensor_split", "kv_type"}
            allowed_kv_types = {
                "f16",
                "bf16",
                "q8_0",
                "q4_0",
                "q4_1",
                "iq4_nl",
                "q5_0",
                "q5_1",
                # TurboQuant KV-cache types supported by the cuda128-laguna-tq-full
                # fork-binary (GGML_TYPE_TURBO2_0/3_0/4_0); CLI token via ggml_type_name().
                # Added STAP7 so runtime_overrides.kv_type can select turbo4 (e.g. STAP8
                # test #3 Qwen3.6+turbo4); base-config kv_type is read unvalidated anyway.
                "turbo2",
                "turbo3",
                "turbo4",
            }
            unknown_keys = set(runtime_overrides) - allowed_keys
            if unknown_keys:
                unknown_keys_list = ", ".join(sorted(repr(key) for key in unknown_keys))
                raise ValueError(
                    "runtime_overrides contains unsupported keys: "
                    f"{unknown_keys_list}. Allowed keys: context, ngl, tensor_split, kv_type"
                )
            for key in ("context", "ngl", "tensor_split", "kv_type"):
                if key not in runtime_overrides:
                    continue
                value = runtime_overrides[key]
                if key in {"context", "ngl"}:
                    if not isinstance(value, (int, str)) or isinstance(value, bool):
                        raise ValueError(
                            f"runtime_overrides.{key} must be an integer, got {type(value).__name__!r}"
                        )
                    if isinstance(value, str):
                        stripped_value = value.strip()
                        if not re.fullmatch(r"[0-9]+", stripped_value):
                            raise ValueError(
                                f"runtime_overrides.{key} string values must contain only digits, got {value!r}"
                            )
                        try:
                            int_value = int(stripped_value)
                        except ValueError:
                            raise ValueError(
                                f"runtime_overrides.{key} string values must contain only digits, got {value!r}"
                            ) from None
                    else:
                        int_value = int(value)
                    if key == "context" and int_value <= 0:
                        raise ValueError(
                            f"runtime_overrides.context must be a positive integer, got {int_value}"
                        )
                    if key == "ngl" and int_value < 0:
                        raise ValueError(
                            f"runtime_overrides.ngl must be a non-negative integer, got {int_value}"
                        )
                    if key == "ngl" and runtime_total_layers is not None and int_value > runtime_total_layers:
                        raise ValueError(
                            "runtime_overrides.ngl must not exceed the configured total_layers "
                            f"({runtime_total_layers}), got {int_value}"
                        )
                    target_config[key] = int_value
                elif key == "tensor_split" and value in (None, ""):
                    target_config.pop("tensor_split", None)
                elif key == "tensor_split":
                    tensor_split = str(value)
                    raw_split_parts = tensor_split.split(",")
                    if len(raw_split_parts) != 2:
                        raise ValueError(
                            "runtime_overrides.tensor_split must contain exactly two comma-separated values, "
                            f"got {len(raw_split_parts)} parts: {tensor_split}"
                        )
                    split_parts = [part.strip() for part in raw_split_parts]
                    if any(not part for part in split_parts):
                        raise ValueError(
                            "runtime_overrides.tensor_split must contain two non-empty comma-separated values"
                        )
                    parsed_split_parts = []
                    for raw_part in split_parts:
                        try:
                            parsed_part = float(raw_part)
                        except ValueError as exc:
                            raise ValueError("runtime_overrides.tensor_split must contain numeric values") from exc
                        if not math.isfinite(parsed_part):
                            raise ValueError(
                                "runtime_overrides.tensor_split values must be finite numbers, "
                                f"got {raw_part!r}"
                            )
                        parsed_split_parts.append(parsed_part)
                    if any(part < 0 for part in parsed_split_parts):
                        raise ValueError(
                            "runtime_overrides.tensor_split values must be non-negative"
                        )
                    split_total = sum(parsed_split_parts)
                    if split_total <= 0:
                        raise ValueError("runtime_overrides.tensor_split must have a positive total")
                    target_config["tensor_split"] = ",".join(split_parts)
                elif key == "kv_type":
                    if not isinstance(value, str):
                        raise ValueError(
                            f"runtime_overrides.kv_type must be a string, got {type(value).__name__!r}"
                        )
                    kv_type = value.strip().lower()
                    if kv_type not in allowed_kv_types:
                        allowed_values = ", ".join(sorted(allowed_kv_types))
                        raise ValueError(
                            f"runtime_overrides.kv_type must be one of: {allowed_values}; got {value!r}"
                        )
                    target_config["kv_type"] = kv_type
        logger.info(
            "Runtime config for %s [%s]: context=%s ngl=%s kv=%s split=%s mmproj=%s",
            target,
            "vision" if desired_vision else "text",
            target_config.get("context"),
            target_config.get("ngl"),
            target_config.get("kv_type"),
            target_config.get("tensor_split") or "auto",
            target_config.get("mmproj") or "none",
        )
        self._write_server_args(target_config)
        await self._stop_server()
        await self._free_gpu_memory()
        await self._start_server()
        healthy = await self._wait_for_health(target)
        if not healthy:
            crash = await self._detect_crash(
                target,
                config_snapshot=self._build_crash_config_snapshot(
                    target,
                    runtime_config=target_config,
                    vision_enabled=desired_vision,
                ),
            )
            raise ModelLoadError(
                f"Model '{target}' failed to load: {crash.error_message}",
                crash_record=crash,
            )
        self.current_model = target
        self.current_vision_enabled = desired_vision
        self.reset_vision_validation(target)
        self.is_unloaded = False
        self.last_request_time = time.time()
        logger.info(f"✅ Model '{target}' loaded and ready")

    async def _save_context(self, filename: str):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.server_url}/slots/0?action=save",
                    json={"filename": filename},
                    timeout=30.0
                )
                if resp.status_code == 200:
                    logger.info(f"Auto-saved context to {filename}")
        except Exception as e:
            logger.warning(f"Failed to auto-save context: {e}")

    async def _load_context(self, filename: str):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.server_url}/slots/0?action=restore",
                json={"filename": filename},
                timeout=60.0
            )
            if resp.status_code == 200:
                logger.info(f"Auto-restored context from {filename}")
            else:
                raise Exception("Restore failed")

    def _build_args_string(self, config: Dict[str, Any]) -> tuple[str, dict[str, str]]:
        """Build the llama-server CLI args string + env vars from a runtime config.
        MUST be byte-identical to what _write_server_args writes to current_model.args
        and current_model.env. Single source of truth for both launch and signature.
        """
        path = config["path"]
        ctx = config.get("context", 4096)
        ngl = config.get("ngl", 99)
        kv_type = config.get("kv_type", "q4_0")
        tensor_split = config.get("tensor_split", "")
        mmproj = config.get("mmproj", "")
        extra_args = config.get("extra_args", "")
        cuda_visible_devices = config.get("cuda_visible_devices", "")
        # DFlash / speculative-decoding draft model (llama-server b2111+)
        draft_model_path = str(config.get("draft_model_path", "")).strip()
        spec_type = str(config.get("spec_type", "draft-dflash")).strip()
        spec_draft_n_max = config.get("spec_draft_n_max", 8)
        spec_draft_n_min = config.get("spec_draft_n_min", 1)
        draft_cache_type_k = str(config.get("draft_cache_type_k", "f16")).strip()
        draft_cache_type_v = str(config.get("draft_cache_type_v", "f16")).strip()

        logger.info(f"Using official llama.cpp binary: {OFFICIAL_LLAMA_SERVER_BIN}")

        # Build args string
        args_content = (
            f"-m {path} -c {ctx} -ngl {ngl} -ctk {kv_type} -ctv {kv_type} "
            f"--host 127.0.0.1 --port 11440 --slot-save-path {LLAMA_SLOTS_DIR} --load-mode none"
        )

        # Multi-GPU weight distribution (e.g. "0.55,0.45" for 2 GPUs)
        if tensor_split:
            args_content += f" --tensor-split {tensor_split}"
            logger.info(f"Tensor split: {tensor_split}")

        # Vision-language projector (required for VL/multimodal models)
        if mmproj:
            mmproj_path = Path(mmproj)
            if not mmproj_path.exists():
                logger.error(f"❌ mmproj file not found: {mmproj} — vision input will NOT work!")
            else:
                args_content += f" --mmproj {mmproj}"
                logger.info(f"🖼️  mmproj: {mmproj}")

        # DFlash / speculative-decoding draft model (llama.cpp b2111+).
        # If draft_model_path is set and exists, llama-server will use --spec-type
        # with --model-draft to draft N tokens at a time before main-model verification.
        if draft_model_path:
            draft_path = Path(draft_model_path)
            if not draft_path.exists():
                logger.warning(
                    f"⚠️  draft_model_path set but file missing: {draft_model_path} — "
                    "speculative decoding will be DISABLED"
                )
            else:
                args_content += (
                    f" --spec-type {spec_type}"
                    f" --model-draft {draft_model_path}"
                    f" --spec-draft-n-max {spec_draft_n_max}"
                    f" --spec-draft-n-min {spec_draft_n_min}"
                    f" --cache-type-k-draft {draft_cache_type_k}"
                    f" --cache-type-v-draft {draft_cache_type_v}"
                )
                logger.info(
                    f"⚡ Speculative decoding enabled: spec_type={spec_type}, "
                    f"draft={draft_path.name}, n_max={spec_draft_n_max}, n_min={spec_draft_n_min}"
                )
        elif spec_type not in ("", "none", "draft-dflash"):
            # Speculative decoding WITHOUT an external draft model: either native
            # MTP layers (draft-mtp — requires model-architecture MTP heads) or
            # n-gram lookup (ngram-simple/ngram-map-k/ngram-mod/ngram-cache — works
            # on any model, uses prompt-context lookup tables). Emit only --spec-type.
            args_content += f" --spec-type {spec_type}"
            logger.info(f"⚡ Speculative decoding enabled (no draft): spec_type={spec_type}")
        elif spec_type == "draft-dflash":
            # draft-dflash requires an external draft model; without one it cannot
            # launch — treat as a config error and emit nothing. Note: the spec_type
            # default IS "draft-dflash", so only warn when it was explicitly set
            # (an absent field just means "no speculation configured" — silent).
            if "spec_type" in config:
                logger.warning(
                    "⚠️  spec_type=draft-dflash without draft_model_path — "
                    "speculative decoding DISABLED (draft-dflash needs --model-draft)"
                )

        # Optional per-model parallel slot count (--parallel). Higher slot counts
        # pair naturally with client-hinted smaller contexts: more concurrent
        # requests fit in the freed KV VRAM. Part of args_content, so a n_slots
        # change also triggers launch-signature drift (correct: launch-param change).
        n_slots = config.get("n_slots")
        if n_slots and int(n_slots) > 1:
            args_content += f" --parallel {int(n_slots)}"
            logger.info(f"Parallel slots: {n_slots}")

        # Pass-through for any extra flags not covered above
        if extra_args:
            args_content += f" {extra_args}"
            logger.info(f"Extra args: {extra_args}")

        # Optional per-model GPU pinning for the systemd launch wrapper.
        # scripts/start_llama.sh sources current_model.env before launching llama-server.
        env_dict: dict[str, str] = {}
        cuda_visible_devices = str(cuda_visible_devices).strip()
        if cuda_visible_devices:
            env_dict["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices
        return args_content, env_dict

    def _write_server_args(self, config: Dict):
        """Build llama-server CLI arguments from model config and write to args file.

        Supported config keys (from models.yaml):
            path, context, ngl, kv_type, tensor_split, mmproj, extra_args,
            cuda_visible_devices, draft_model_path, spec_type,
            spec_draft_n_max, spec_draft_n_min, draft_cache_type_k, draft_cache_type_v
        """
        args_file = CURRENT_MODEL_ARGS_FILE
        env_file = CURRENT_MODEL_ENV_FILE

        args_content, env_dict = self._build_args_string(config)

        with open(args_file, "w") as f:
            f.write(args_content)

        # Optional per-model GPU pinning for the systemd launch wrapper.
        # scripts/start_llama.sh sources current_model.env before launching llama-server.
        if env_dict:
            with open(env_file, "w") as f:
                f.write(f"export CUDA_VISIBLE_DEVICES={env_dict['CUDA_VISIBLE_DEVICES']}\n")
            logger.info(f"CUDA_VISIBLE_DEVICES={env_dict['CUDA_VISIBLE_DEVICES']}")
        elif env_file.exists():
            env_file.unlink()
            logger.info("Cleared model environment file (no CUDA_VISIBLE_DEVICES override)")

    def _compute_launch_signature(
        self,
        model_name: str,
        *,
        enable_vision: Optional[bool],
        context_hint: Optional[int] = None,
    ) -> Optional[dict]:
        """Compute the launch signature for a model+vision-mode from CURRENT models.yaml.
        Returns None if the model is unknown. Uses build_runtime_config (so vision/text
        overrides and the client context hint resolve correctly)."""
        if model_name not in self.models:
            return None
        runtime_config = self.build_runtime_config(
            model_name, enable_vision=enable_vision, context_hint=context_hint
        )
        args_str, env_dict = self._build_args_string(runtime_config)
        return {
            "model": model_name,
            "vision": bool(self._resolve_runtime_vision_flag(model_name, enable_vision)),
            "args_sha256": hashlib.sha256(args_str.encode("utf-8")).hexdigest(),
            "env_sha256": hashlib.sha256(json.dumps(env_dict, sort_keys=True).encode("utf-8")).hexdigest(),
        }

    def _read_persisted_signature(self) -> Optional[dict]:
        try:
            text = CURRENT_MODEL_SIG_FILE.read_text()
            return json.loads(text)
        except Exception:
            return None

    def _write_persisted_signature(self, sig: dict) -> None:
        try:
            CURRENT_MODEL_SIG_FILE.write_text(json.dumps(sig, sort_keys=True))
        except Exception as e:
            logger.warning(f"Failed to persist launch signature: {e}")

    def _config_drifted(
        self,
        model_name: str,
        *,
        enable_vision: Optional[bool],
        context_hint: Optional[int] = None,
    ) -> bool:
        """True if the model must be reloaded to apply current models.yaml settings.
        Drift = persisted sig missing, OR model/vision differ, OR args/env hash differ.
        context_hint is folded into the computed signature, so a client-hinted
        context different from the persisted one counts as drift (triggers reload)."""
        persisted = self._read_persisted_signature()
        if not persisted:
            return True
        current = self._compute_launch_signature(
            model_name, enable_vision=enable_vision, context_hint=context_hint
        )
        if not current:
            return True
        return persisted != current

    async def _stop_server(self):
        # Use create_subprocess_exec (no shell) — the command is static, but
        # avoiding shell=True closes the injection surface if this ever gains
        # parameters sourced from config/user input.
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "stop", "llama-server",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def _start_server(self):
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "start", "llama-server",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def _wait_for_health(self, model_name: str = "") -> bool:
        """Poll llama-server health endpoint. Returns True if healthy, False if crashed.
        
        Detects crashes by monitoring systemd restart counter (NRestarts).
        If NRestarts increases, the service is crash-looping.
        """
        initial_restarts = await self._get_restart_count()
        max_crash_restarts = 3  # If service restarts 3+ times, it's definitely broken

        for i in range(120):  # 120 seconds timeout for large models
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{self.server_url}/health", timeout=5.0)
                    if resp.status_code == 200:
                        logger.info(f"✅ Server healthy after {i}s (model: {model_name})")
                        return True
            except Exception:
                pass

            # Every 5 seconds, check if the service is crash-looping
            if i > 3 and i % 5 == 0:
                current_restarts = await self._get_restart_count()
                restart_delta = current_restarts - initial_restarts
                if restart_delta >= max_crash_restarts:
                    logger.error(
                        f"❌ llama-server crash-looping ({restart_delta} restarts) "
                        f"while loading '{model_name}'"
                    )
                    return False

                # Also check if service entered failed state (Restart=on-failure with limit)
                if await self._is_service_failed():
                    logger.error(f"❌ llama-server service failed while loading '{model_name}'")
                    return False

            await asyncio.sleep(1)

        logger.error(f"❌ Server health timeout after 120s for '{model_name}'")
        return False

    async def _get_restart_count(self) -> int:
        """Get the NRestarts counter from systemd for llama-server."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "show", "llama-server", "--property=NRestarts", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Output like: NRestarts=16
            val = stdout.decode().strip().split("=")[-1]
            return int(val)
        except Exception:
            return 0

    async def _is_service_failed(self) -> bool:
        """Check if the llama-server systemd service is in a failed state."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-failed", "llama-server",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode().strip() == "failed"
        except Exception:
            return False

    async def _detect_crash(self, model_name: str, config_snapshot: Optional[Dict[str, Any]] = None) -> CrashRecord:
        """Extract error details from journalctl and record the crash."""
        error_msg = await self._get_crash_error()
        config_snap = copy.deepcopy(config_snapshot) if config_snapshot is not None else self.models.get(model_name, {}).copy()

        crash = CrashRecord(
            timestamp=datetime.now().isoformat(),
            model=model_name,
            error_message=error_msg,
            exit_code=await self._get_service_exit_code(),
            config_snapshot=config_snap,
        )

        self.last_crash = crash
        self.crash_history.append(crash)
        if len(self.crash_history) > MAX_CRASH_HISTORY:
            self.crash_history = self.crash_history[-MAX_CRASH_HISTORY:]

        runtime_mode = config_snap.get("runtime_mode") if isinstance(config_snap, dict) else None
        effective = config_snap.get("effective_runtime_config") if isinstance(config_snap, dict) else None
        logger.error(
            "💥 Crash recorded: model=%s runtime_mode=%s effective_runtime=%s error=%s",
            model_name,
            runtime_mode or "unknown",
            effective or {},
            error_msg,
        )

        # Stop the service to prevent restart loops
        await self._stop_server()

        return crash

    async def _get_crash_error(self) -> str:
        """Extract the relevant error lines from journalctl for the last llama-server run."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", "llama-server", "-n", "120", "--no-pager", "-o", "cat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().splitlines()
            return self._extract_crash_error_from_lines(lines)
        except Exception as e:
            return f"Failed to read crash logs: {e}"

    @staticmethod
    def _extract_crash_error_from_lines(lines: List[str]) -> str:
        """Summarize the most relevant llama-server crash lines from recent logs."""
        error_keywords = [
            "cudamalloc failed",
            "cuda error",
            "out of memory",
            "failed to load model",
            "failed to allocate",
            "failed to fit params to free device memory",
            "cannot meet free memory targets",
            "failed to initialize the context",
            "failed to allocate compute pp buffers",
            "error loading model",
            "unknown model architecture",
            "alloc_tensor_range: failed",
            "graph_reserve: failed",
            "segmentation fault",
            "core dumped",
            "exiting due to",
        ]

        error_lines: List[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            lower = line.lower()
            if any(keyword in lower for keyword in error_keywords):
                if not error_lines or error_lines[-1] != line:
                    error_lines.append(line)

        if error_lines:
            return " | ".join(error_lines[-6:])
        return "Unknown error (no recognizable error pattern in logs)"

    async def _get_service_exit_code(self) -> Optional[int]:
        """Get the exit code of the last llama-server run."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "show", "llama-server", "--property=ExecMainStatus", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Output like: ExecMainStatus=1
            val = stdout.decode().strip().split("=")[-1]
            return int(val)
        except Exception:
            return None

    def get_crash_history(self) -> List[Dict]:
        """Return crash history as a list of dicts (for API responses)."""
        return [c.to_dict() for c in self.crash_history]

manager = ModelManager()
