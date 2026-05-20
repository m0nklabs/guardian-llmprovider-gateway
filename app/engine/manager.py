import asyncio
import logging
import subprocess
import yaml
import time
import re
import shlex
import httpx
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger("model-manager")

# Binary paths for backend support
# Additional backends (forks, custom builds) can be registered here.
# Models select their backend via the 'backend' key in models.yaml (default: official).
BACKEND_BINARIES = {
    "official": "/home/flip/llama_cpp_official/build/bin/llama-server",
}
DEFAULT_BACKEND = "official"

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


@dataclass
class VisionCapability:
    """Runtime multimodal capability state for a configured model."""
    configured: bool
    mmproj: Optional[str]
    mmproj_exists: bool
    backend: str
    status: str
    signature: Tuple[str, str, str]
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "mmproj": self.mmproj,
            "mmproj_exists": self.mmproj_exists,
            "backend": self.backend,
            "status": self.status,
            "validated": self.status in {"supported", "unsupported", "loading", "load_failed", "misconfigured"},
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
        }


class ModelManager:
    def __init__(self, config_path: str = "/home/flip/llama_cpp_guardian/config/models.yaml"):
        self.config_path = Path(config_path)
        self.models = self._load_config()
        self._vision_capabilities: Dict[str, VisionCapability] = {}
        self._sync_vision_capabilities()
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

        # Initial model: use pinned model if set, otherwise fallback
        self.current_model = self._pinned_model or self._detect_initial_model()
        logger.info(f"📌 Initial model set to: {self.current_model}")

        # === VRAM management: unload state and idle tracking ===
        self.is_unloaded: bool = False  # True when llama-server stopped to free VRAM
        self.last_request_time: float = time.time()  # Used for idle-unload timeout
        self.active_requests: int = 0  # Counter for in-flight requests (prevents idle-unload during streaming)

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
        # 1. Exact match
        if name in self.models:
            return name

        # 2. Alias lookup
        aliases = self._load_aliases()
        if name in aliases:
            target = aliases[name]
            if target in self.models:
                logger.info(f"🏷️  Resolved alias '{name}' → '{target}'")
                return target
            logger.warning(f"⚠️ Alias '{name}' points to '{target}' which is not in models config")

        # 3. Case-insensitive fallback
        name_lower = name.lower()
        for model_name in self.models:
            if model_name.lower() == name_lower:
                logger.info(f"🏷️  Resolved case-insensitive '{name}' → '{model_name}'")
                return model_name

        raise ValueError(f"Model '{name}' not found in configuration (no alias match)")

    def _uses_reasoning(self, config: Dict) -> bool:
        extra_args = str(config.get("extra_args", ""))
        return "--reasoning on" in extra_args

    def _is_tool_friendly_config(self, config: Dict) -> bool:
        extra_args = str(config.get("extra_args", ""))
        return (
            "chat-template-file" in extra_args
            or "--reasoning-budget 0" in extra_args
            or not self._uses_reasoning(config)
        )

    def _matching_model_candidates(self, model_name: str) -> List[str]:
        config = self.models.get(model_name, {})
        if not config:
            return []

        path = config.get("path")
        backend = config.get("backend", DEFAULT_BACKEND)
        mmproj = config.get("mmproj")
        candidates: List[str] = []
        for candidate_name, candidate_cfg in self.models.items():
            if candidate_name == model_name:
                continue
            if candidate_cfg.get("path") != path:
                continue
            if candidate_cfg.get("backend", DEFAULT_BACKEND) != backend:
                continue
            if candidate_cfg.get("mmproj") != mmproj:
                continue
            candidates.append(candidate_name)
        return candidates

    def _sort_preferred_candidates(self, model_names: List[str]) -> List[str]:
        def sort_key(name: str):
            cfg = self.models.get(name, {})
            extra_args = str(cfg.get("extra_args", ""))
            context = self.get_runtime_context_window(name) or 0
            return (
                0 if "Agent" in name else 1,
                0 if self._is_tool_friendly_config(cfg) else 1,
                0 if "chat-template-file" in extra_args else 1,
                -context,
                name,
            )

        return sorted(model_names, key=sort_key)

    def get_preferred_tool_model(self, model_name: Optional[str] = None) -> Optional[str]:
        """Return a tool-friendly sibling profile for a model family when available."""
        target = model_name or self.current_model
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
        target = model_name or self.current_model
        config = self.models.get(target)
        if not config:
            return None
        if self._uses_reasoning(config):
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
        """Return the configured runtime context for a model, if set."""
        config = self.models.get(model_name, {})
        configured_context = config.get("context", config.get("ctx"))
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
        public_models: Dict[str, str] = {name: name for name in self.models}

        for alias, target in self._load_aliases().items():
            if alias in public_models:
                continue
            if target not in self.models:
                logger.warning(f"⚠️ Skipping alias '{alias}' in public model list; target '{target}' not found")
                continue
            public_models[alias] = target

        return public_models

    def _vision_signature(self, config: Dict) -> Tuple[str, str, str]:
        return (
            str(config.get("path", "")).strip(),
            str(config.get("mmproj", "")).strip(),
            str(config.get("backend", DEFAULT_BACKEND)).strip() or DEFAULT_BACKEND,
        )

    def _sync_vision_capabilities(self) -> None:
        """Refresh cached multimodal capability state from the current config."""
        previous = getattr(self, "_vision_capabilities", {})
        refreshed: Dict[str, VisionCapability] = {}

        for model_name, config in self.models.items():
            mmproj = str(config.get("mmproj", "")).strip() or None
            backend = str(config.get("backend", DEFAULT_BACKEND)).strip() or DEFAULT_BACKEND
            signature = self._vision_signature(config)
            existing = previous.get(model_name)

            if not mmproj:
                refreshed[model_name] = VisionCapability(
                    configured=False,
                    mmproj=None,
                    mmproj_exists=False,
                    backend=backend,
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
                    backend=backend,
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
                    backend=backend,
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
                backend=backend,
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
                backend=DEFAULT_BACKEND,
                status="unknown",
                signature=("", "", DEFAULT_BACKEND),
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

    @property
    def pinned_model(self) -> Optional[str]:
        return self._pinned_model

    def _detect_initial_model(self) -> str:
        """Detect which model the backend is running by reading current_model.args.
        Falls back to first model in config if detection fails.
        """
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
        target = self._pinned_model or self.current_model
        logger.info(f"🔍 Startup check: expecting model '{target}'")

        verified = await self.verify_backend_model()
        if verified:
            logger.info(f"✅ Startup check passed — backend matches '{self.current_model}'")
            return

        # Backend mismatch detected — force switch
        actual_gguf = self._get_backend_model_path()
        actual_name = self._identify_model_by_path(actual_gguf) if actual_gguf else "NONE"
        logger.warning(
            f"🔄 Startup mismatch: forcing switch from actual '{actual_name}' to target '{target}'"
        )

        self.current_model = "__MISMATCH__"  # Force switch_model to not skip

        try:
            await self.switch_model(target)
            logger.info(f"✅ Startup forced switch to '{target}' succeeded")
        except Exception as e:
            logger.error(f"❌ Startup forced switch FAILED: {e}")

    def _load_config(self) -> Dict:
        if not self.config_path.exists():
            logger.warning(f"Config not found at {self.config_path}")
            return {}
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f).get("models", {})

    async def get_current_model(self) -> str:
        # We can implement a health check or store internal state
        return self.current_model

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

    async def switch_model(self, model_name: str, client_id: str = "_system", force: bool = False):
        # Re-read models.yaml so config edits take effect without Guardian restart
        self.models = self._load_config()
        self._sync_vision_capabilities()
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

        if model_name == self.current_model:
            logger.info(f"Model {model_name} is already active")
            return

        logger.info(f"Switching from {self.current_model} to {model_name}")
        
        # 1. Auto-save current context
        await self._save_context(f"auto_save_{self.current_model}")

        # 2. Stop llama-server
        await self._stop_server()

        # 3. Write new model args + binary selection
        target_config = self.models[model_name]
        self._write_server_args(target_config)
        
        # 4. Free GPU memory (kill non-Frigate processes)
        await self._free_gpu_memory()

        # 5. Start llama-server
        await self._start_server()
        
        # 6. Wait for health with crash detection
        healthy = await self._wait_for_health(model_name)
        
        if not healthy:
            # Server crashed or failed to start — record and raise
            crash = await self._detect_crash(model_name)
            raise ModelLoadError(
                f"Model '{model_name}' failed to load: {crash.error_message}",
                crash_record=crash,
            )
        
        self.current_model = model_name
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
        """Read ComfyUI URL from settings.yaml, fallback to default."""
        try:
            settings_path = self.config_path.parent / "settings.yaml"
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

    async def load(self, model_name: Optional[str] = None) -> None:
        """Reload llama-server with current (or specified) model."""
        # Re-read models.yaml so config edits take effect without Guardian restart
        self.models = self._load_config()
        self._sync_vision_capabilities()
        target = model_name or self.current_model
        if target not in self.models:
            raise ValueError(f"Model '{target}' not found in configuration")
        logger.info(f"🔄 Loading model '{target}'...")
        self._write_server_args(self.models[target])
        await self._stop_server()
        await self._free_gpu_memory()
        await self._start_server()
        healthy = await self._wait_for_health(target)
        if not healthy:
            crash = await self._detect_crash(target)
            raise ModelLoadError(
                f"Model '{target}' failed to load: {crash.error_message}",
                crash_record=crash,
            )
        self.current_model = target
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

    def _write_server_args(self, config: Dict):
        """Build llama-server CLI arguments from model config and write to args file.

        Supported config keys (from models.yaml):
            path, context, ngl, kv_type, backend, tensor_split, mmproj, extra_args
        """
        args_file = Path("/home/flip/llama_cpp_guardian/config/current_model.args")
        path = config["path"]
        ctx = config.get("context", 4096)
        ngl = config.get("ngl", 99)
        kv_type = config.get("kv_type", "q4_0")
        backend = config.get("backend", DEFAULT_BACKEND)
        tensor_split = config.get("tensor_split", "")
        mmproj = config.get("mmproj", "")
        extra_args = config.get("extra_args", "")

        # Resolve binary path and write to separate file for start_llama.sh
        binary_path = BACKEND_BINARIES.get(backend, BACKEND_BINARIES[DEFAULT_BACKEND])
        binary_file = Path("/home/flip/llama_cpp_guardian/config/current_model.binary")
        with open(binary_file, "w") as f:
            f.write(binary_path)
        logger.info(f"Backend: {backend} -> {binary_path}")

        # Build args string
        args_content = f"-m {path} -c {ctx} -ngl {ngl} -ctk {kv_type} -ctv {kv_type} --host 127.0.0.1 --port 11440 --slot-save-path /home/flip/llama_slots --no-mmap"

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

        # Pass-through for any extra flags not covered above
        if extra_args:
            args_content += f" {extra_args}"
            logger.info(f"Extra args: {extra_args}")

        with open(args_file, "w") as f:
            f.write(args_content)

    async def _stop_server(self):
        # Use simple os.system or subprocess to handle sudo if needed
        proc = await asyncio.create_subprocess_shell(
            "sudo systemctl stop llama-server",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def _start_server(self):
        proc = await asyncio.create_subprocess_shell(
            "sudo systemctl start llama-server",
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

    async def _detect_crash(self, model_name: str) -> CrashRecord:
        """Extract error details from journalctl and record the crash."""
        error_msg = await self._get_crash_error()
        config_snap = self.models.get(model_name, {}).copy()

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

        logger.error(f"💥 Crash recorded: model={model_name} error={error_msg}")

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
