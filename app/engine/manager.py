import asyncio
import logging
import subprocess
import yaml
import time
import re
import httpx
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("model-manager")

# Binary paths for dual-backend support
# official = primary (ggml-org/llama.cpp), ik_fork = fallback if needed for specific optimizations
BACKEND_BINARIES = {
    "official": "/home/flip/llama_cpp_official/build/bin/llama-server",
    "ik_fork": "/home/flip/ik_llama_cpp_build/build/bin/llama-server",
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


class ModelManager:
    def __init__(self, config_path: str = "/home/flip/llama_cpp_guardian/config/models.yaml"):
        self.config_path = Path(config_path)
        self.models = self._load_config()
        self.server_process: Optional[int] = None # Systemd manages main process, but we might control it via systemctl
        self.server_url = "http://127.0.0.1:11440"
        self.crash_history: List[CrashRecord] = []
        self.last_crash: Optional[CrashRecord] = None

        # === SECURITY: Model pinning & switch protection ===
        self._pinned_model: Optional[str] = self._load_pinned_model()
        self._switch_allowlist: Set[str] = self._load_switch_allowlist()
        self._model_verified = False  # True after startup verification passes

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

    @property
    def pinned_model(self) -> Optional[str]:
        return self._pinned_model

    def _detect_initial_model(self) -> str:
        """Detect which model the backend is running by reading current_model.args.
        Falls back to first model in config if detection fails.
        """
        try:
            args_file = Path("/home/flip/llama_cpp_guardian/config/current_model.args")
            if args_file.exists():
                args = args_file.read_text().strip()
                # Extract -m /path/to/model.gguf from args
                for model_name, config in self.models.items():
                    if config.get("path") and config["path"] in args:
                        logger.info(f"🔍 Detected running model from args file: {model_name}")
                        return model_name
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
                return True
            else:
                # MISMATCH — find which model is actually loaded
                actual_model_name = self._identify_model_by_path(actual_gguf)
                logger.error(
                    f"🚨 MODEL MISMATCH! Guardian thinks: {self.current_model} "
                    f"but backend runs: {actual_model_name or 'UNKNOWN'} ({Path(actual_gguf).name})"
                )
                self._model_verified = False
                return False
        except Exception as e:
            logger.error(f"❌ Backend verification failed: {e}")
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

        if target != self.current_model:
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

    async def switch_model(self, model_name: str, client_id: str = "_system", force: bool = False):
        # Re-read models.yaml so config edits take effect without Guardian restart
        self.models = self._load_config()
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

    async def _request_comfyui_free(self) -> None:
        """Ask ComfyUI to unload all models and free GPU memory via its API."""
        comfyui_url = "http://127.0.0.1:8188"
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
        target = model_name or self.current_model
        if target not in self.models:
            raise ValueError(f"Model '{target}' not found in configuration")
        logger.info(f"🔄 Loading model '{target}'...")
        self._write_server_args(self.models[target])
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
                "journalctl", "-u", "llama-server", "-n", "30", "--no-pager", "-o", "cat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().splitlines()

            # Find the most relevant error lines
            error_lines = []
            error_keywords = [
                "cudaMalloc failed",
                "out of memory",
                "failed to load model",
                "failed to allocate",
                "error loading model",
                "unknown model architecture",
                "CUDA error",
                "exiting due to",
                "alloc_tensor_range: failed",
            ]
            for line in lines:
                lower = line.lower()
                if any(kw.lower() in lower for kw in error_keywords):
                    error_lines.append(line.strip())

            if error_lines:
                return " | ".join(error_lines[-5:])  # Last 5 relevant error lines
            return "Unknown error (no recognizable error pattern in logs)"
        except Exception as e:
            return f"Failed to read crash logs: {e}"

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
