"""Proxy runtime state container.

Extracted from ``app.proxy.server`` as part of Phase 5 (Structural Separation).
Holds the per-process mutable state (generations, model stats, usage tracker,
VRAM scheduler, optimizer, scaler) that is injected into the gateway/cloud/
local modules at startup.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict

from app.local_inference.models import VramScheduler
from app.proxy.optimizer import RequestOptimizer
from app.proxy.scaler import DynamicScaler
from app.proxy.usage import ApiUsageTracker


class State:
    def __init__(self, vram_limit_mb: int):
        self.active_generations: Dict[str, int] = {}  # request_id -> vram_usage
        self.model_stats: Dict[str, int] = {}
        self.last_used: Dict[str, float] = defaultdict(float)
        self.api_usage = ApiUsageTracker()
        # VRAM Scheduler
        self.scheduler = VramScheduler(vram_limit_mb)
        # Optimizer
        self.optimizer = RequestOptimizer()
        # Dynamic scaler — adaptive reasoning budget & max_tokens
        self.scaler = DynamicScaler()


state: State = None  # type: ignore[assignment]  # bound by server at startup
