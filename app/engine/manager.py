import asyncio
import logging
import subprocess
import yaml
import time
import httpx
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("model-manager")

class ModelManager:
    def __init__(self, config_path: str = "/home/flip/llama_cpp_guardian/config/models.yaml"):
        self.config_path = Path(config_path)
        self.models = self._load_config()
        self.current_model = "glm-4"  # Default startup model
        self.server_process: Optional[int] = None # Systemd manages main process, but we might control it via systemctl
        self.server_url = "http://127.0.0.1:11440"

    def _load_config(self) -> Dict:
        if not self.config_path.exists():
            logger.warning(f"Config not found at {self.config_path}")
            return {}
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f).get("models", {})

    async def get_current_model(self) -> str:
        # We can implement a health check or store internal state
        return self.current_model

    async def switch_model(self, model_name: str):
        if model_name not in self.models:
            raise ValueError(f"Model {model_name} not found in configuration")

        if model_name == self.current_model:
            logger.info(f"Model {model_name} is already active")
            return

        logger.info(f"Switching from {self.current_model} to {model_name}")
        
        # 1. Save current context implicitly? 
        # (Maybe explicit save is safer, but "on the fly" implies auto-handling)
        # Let's auto-save 'auto_save_{model_name}' just in case
        await self._save_context(f"auto_save_{self.current_model}")

        # 2. Stop llama-server
        await self._stop_server()

        # 3. Update Service Command (or restart with new args)
        # Since we use systemd, we can't easily change the ExecStart via text file edit reliably and fast.
        # BETTER APPROACH: Run llama-server as a subprocess directly from here, OR use a wrapper script that reads a target file.
        # Let's use the wrapper script approach. 
        # We will write the new model args to a file, then restart the service.
        
        target_config = self.models[model_name]
        self._write_server_args(target_config)
        
        # 4. Start llama-server
        await self._start_server()
        
        self.current_model = model_name
        
        # 5. Restore context if exists
        # Wait for health
        await self._wait_for_health()
        try:
             await self._load_context(f"auto_save_{model_name}")
        except Exception:
             logger.info(f"No auto-save found for {model_name}, starting fresh.")

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
        # We'll create a env file or arguments file that the service reads
        args_file = Path("/home/flip/llama_cpp_guardian/config/current_model.args")
        path = config["path"]
        ctx = config.get("context", 4096)
        ngl = config.get("ngl", 99)
        
        # Format: --model /path --ctx 4096 ...
        args_content = f"-m {path} -c {ctx} -ngl {ngl} -ctk q4_0 -ctv q4_0 --host 127.0.0.1 --port 11440 --slot-save-path /home/flip/llama_slots"
        
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

    async def _wait_for_health(self):
        for _ in range(30): # 30 seconds timeout
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{self.server_url}/health")
                    if resp.status_code == 200:
                        return
            except:
                pass
            await asyncio.sleep(1)
        logger.error("Server failed to come online")

manager = ModelManager()
