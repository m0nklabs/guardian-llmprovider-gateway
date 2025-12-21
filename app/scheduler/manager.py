import asyncio
import logging
import datetime
import subprocess

logger = logging.getLogger("Scheduler")

class SchedulerManager:
    def __init__(self):
        # Configuration for idle window (04:00 - 11:00)
        self.start_hour = 4
        self.end_hour = 11
        self.services_to_manage = [
            # "caramba-backend", # Example services to stop
            # "agent-forge"
        ]
        self.active_mode = False

    def is_idle_window(self):
        now = datetime.datetime.now()
        # Check if weekday (0=Monday, 4=Friday)
        if now.weekday() > 4: # Saturday(5) or Sunday(6)
            return False # Or maybe run all day on weekends? User said "doordeweeks" (weekdays)
        
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
        try:
            logger.info(f"{action.capitalize()}ing service: {service_name}")
            # subprocess.run(["sudo", "systemctl", action, service_name], check=True)
            # For now, just log it to avoid accidental shutdowns during dev
            pass 
        except Exception as e:
            logger.error(f"Failed to {action} {service_name}: {e}")
