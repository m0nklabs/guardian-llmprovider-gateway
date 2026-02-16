import asyncio
import logging
import datetime
import subprocess

import yaml
from pathlib import Path

logger = logging.getLogger("Scheduler")


class SchedulerManager:
    def __init__(self):
        config = self._load_config()
        schedule = config.get("benchmark", {}).get("schedule", {})
        self.start_hour = schedule.get("start_hour", 4)
        self.end_hour = schedule.get("end_hour", 11)
        self.allowed_days = schedule.get("days", ["mon", "tue", "wed", "thu", "fri"])
        self.services_to_manage = config.get("services_to_stop", [])
        self.active_mode = False
        logger.info(f"Scheduler config: hours={self.start_hour}-{self.end_hour}, days={self.allowed_days}, services={self.services_to_manage}")

    def _load_config(self) -> dict:
        """Load scheduler configuration from settings.yaml."""
        config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        try:
            if config_path.exists():
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load scheduler config: {e}")
        return {}

    def is_idle_window(self):
        now = datetime.datetime.now()
        # Check if today's day name is in allowed days
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today = day_names[now.weekday()]
        if today not in self.allowed_days:
            return False
        
        return self.start_hour <= now.hour < self.end_hour

    async def run_loop(self, benchmark_suite):
        logger.info("Scheduler loop started.")
        while True:
            if self.is_idle_window():
                if not self.active_mode:
                    await self.enter_maintenance_mode()
                
                # Trigger benchmark if not running
                if not benchmark_suite.is_running:
                    logger.info("Idle window active. Triggering benchmark suite.")
                    await benchmark_suite.run_suite()
            else:
                if self.active_mode:
                    await self.exit_maintenance_mode()
                    if benchmark_suite.is_running:
                        logger.info("Idle window ended. Stopping benchmark suite.")
                        benchmark_suite.stop()

            await asyncio.sleep(60) # Check every minute

    async def enter_maintenance_mode(self):
        logger.info("Entering Maintenance Mode (Idle Window)")
        self.active_mode = True
        for service in self.services_to_manage:
            self.manage_service(service, "stop")

    async def exit_maintenance_mode(self):
        logger.info("Exiting Maintenance Mode")
        self.active_mode = False
        for service in self.services_to_manage:
            self.manage_service(service, "start")

    def manage_service(self, service_name, action):
        """Start or stop a systemd service during maintenance mode."""
        try:
            logger.info(f"{action.capitalize()}ing service: {service_name}")
            subprocess.run(
                ["sudo", "systemctl", action, service_name],
                check=True,
                capture_output=True,
                timeout=30
            )
            logger.info(f"Successfully {action}ed {service_name}")
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout {action}ing {service_name}")
        except Exception as e:
            logger.error(f"Failed to {action} {service_name}: {e}")
