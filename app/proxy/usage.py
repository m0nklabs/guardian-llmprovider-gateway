"""Persistent API usage tracking for the Guardian dashboard."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import json
import logging
from pathlib import Path
from threading import Lock
import time
from typing import Any, Optional

from app.paths import DATA_DIR


logger = logging.getLogger("guardian-usage")


def _safe_int(value: object) -> int:
    """Convert an arbitrary value to a non-negative integer."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _category_for_endpoint(endpoint: str) -> str:
    """Map a request path to a coarse usage category."""
    if endpoint.startswith("/admin/"):
        return "admin"
    if endpoint.startswith("/api/session/"):
        return "session"
    if endpoint.startswith("/v1/") or endpoint in {"/api/chat", "/api/generate"}:
        return "inference"
    return "other"


class ApiUsageTracker:
    """Track authenticated API usage in memory and persist it for restarts."""

    def __init__(self, recent_limit: int = 1000, state_file: Optional[Path | str] = None):
        self._lock = Lock()
        self._recent_limit = recent_limit
        self._state_file = Path(state_file) if state_file is not None else DATA_DIR / "api_usage_state.json"
        with self._lock:
            self._clear_state_locked()
            self._load_locked()

    def _clear_state_locked(self) -> None:
        """Reset internal counters without touching persistence."""
        self.started_at = time.time()
        self.total_requests = 0
        self.total_errors = 0
        self.unauthenticated_requests = 0
        self.streaming_requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.total_request_bytes = 0
        self.total_response_bytes = 0
        self.total_duration_ms = 0.0
        self.requests_with_duration = 0
        self._endpoint_counts: Counter[str] = Counter()
        self._recent_requests: deque[dict[str, Any]] = deque(maxlen=self._recent_limit)
        self._clients: dict[str, dict[str, Any]] = defaultdict(self._new_client_bucket)

    def _new_client_bucket(self) -> dict[str, Any]:
        """Create an empty stats bucket for a client ID."""
        return {
            "requests": 0,
            "errors": 0,
            "streaming_requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "request_bytes": 0,
            "response_bytes": 0,
            "duration_total_ms": 0.0,
            "requests_with_duration": 0,
            "last_seen": None,
            "last_model": None,
            "last_endpoint": None,
            "last_key_prefix": None,
            "last_key_fingerprint": None,
            "last_auth_header": None,
            "last_source_ip": None,
            "last_forwarded_for": None,
            "last_host": None,
            "last_origin": None,
            "last_referer": None,
            "last_user_agent": None,
            "project_prefix": None,
            "metadata_client": None,
            "metadata_note": None,
            "categories": Counter(),
            "endpoints": Counter(),
            "methods": Counter(),
        }

    def _serialize_locked(self) -> dict[str, Any]:
        """Build a JSON-safe snapshot for persistence."""
        clients: dict[str, dict[str, Any]] = {}
        for client_id, bucket in self._clients.items():
            clients[client_id] = {
                "requests": int(bucket["requests"]),
                "errors": int(bucket["errors"]),
                "streaming_requests": int(bucket["streaming_requests"]),
                "prompt_tokens": int(bucket["prompt_tokens"]),
                "completion_tokens": int(bucket["completion_tokens"]),
                "total_tokens": int(bucket["total_tokens"]),
                "request_bytes": int(bucket["request_bytes"]),
                "response_bytes": int(bucket["response_bytes"]),
                "duration_total_ms": round(float(bucket["duration_total_ms"]), 3),
                "requests_with_duration": int(bucket["requests_with_duration"]),
                "last_seen": bucket["last_seen"],
                "last_model": bucket["last_model"],
                "last_endpoint": bucket["last_endpoint"],
                "last_key_prefix": bucket["last_key_prefix"],
                "last_key_fingerprint": bucket["last_key_fingerprint"],
                "last_auth_header": bucket["last_auth_header"],
                "last_source_ip": bucket["last_source_ip"],
                "last_forwarded_for": bucket["last_forwarded_for"],
                "last_host": bucket["last_host"],
                "last_origin": bucket["last_origin"],
                "last_referer": bucket["last_referer"],
                "last_user_agent": bucket["last_user_agent"],
                "project_prefix": bucket["project_prefix"],
                "metadata_client": bucket["metadata_client"],
                "metadata_note": bucket["metadata_note"],
                "categories": dict(bucket["categories"]),
                "endpoints": dict(bucket["endpoints"]),
                "methods": dict(bucket["methods"]),
            }

        return {
            "schema_version": 2,
            "started_at": self.started_at,
            "total_requests": int(self.total_requests),
            "total_errors": int(self.total_errors),
            "unauthenticated_requests": int(self.unauthenticated_requests),
            "streaming_requests": int(self.streaming_requests),
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": int(self.total_tokens),
            "total_request_bytes": int(self.total_request_bytes),
            "total_response_bytes": int(self.total_response_bytes),
            "total_duration_ms": round(float(self.total_duration_ms), 3),
            "requests_with_duration": int(self.requests_with_duration),
            "endpoint_counts": dict(self._endpoint_counts),
            "recent_requests": list(self._recent_requests),
            "clients": clients,
        }

    def _save_locked(self) -> None:
        """Persist current usage state atomically to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_file.with_name(f"{self._state_file.name}.tmp")
            tmp_path.write_text(json.dumps(self._serialize_locked(), ensure_ascii=True), encoding="utf-8")
            tmp_path.replace(self._state_file)
        except Exception as exc:
            logger.warning("Failed to persist API usage state to %s: %s", self._state_file, exc)

    def _load_locked(self) -> None:
        """Restore persisted usage state if one exists."""
        if not self._state_file.exists():
            return

        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load API usage state from %s: %s", self._state_file, exc)
            return

        if not isinstance(raw, dict):
            logger.warning("Ignoring malformed API usage state in %s: root is not an object", self._state_file)
            return

        self.started_at = float(raw.get("started_at") or time.time())
        self.total_requests = _safe_int(raw.get("total_requests", 0))
        self.total_errors = _safe_int(raw.get("total_errors", 0))
        self.unauthenticated_requests = _safe_int(raw.get("unauthenticated_requests", 0))
        self.streaming_requests = _safe_int(raw.get("streaming_requests", 0))
        self.prompt_tokens = _safe_int(raw.get("prompt_tokens", 0))
        self.completion_tokens = _safe_int(raw.get("completion_tokens", 0))
        self.total_tokens = _safe_int(raw.get("total_tokens", 0))
        self.total_request_bytes = _safe_int(raw.get("total_request_bytes", 0))
        self.total_response_bytes = _safe_int(raw.get("total_response_bytes", 0))
        self.total_duration_ms = max(float(raw.get("total_duration_ms", 0.0) or 0.0), 0.0)
        self.requests_with_duration = _safe_int(raw.get("requests_with_duration", 0))
        self._endpoint_counts = Counter(raw.get("endpoint_counts", {}))
        recent = raw.get("recent_requests", [])
        self._recent_requests = deque(recent[-self._recent_limit :], maxlen=self._recent_limit)
        self._clients = defaultdict(self._new_client_bucket)

        clients = raw.get("clients", {})
        if not isinstance(clients, dict):
            return

        scalar_fields = (
            "requests",
            "errors",
            "streaming_requests",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "request_bytes",
            "response_bytes",
            "duration_total_ms",
            "requests_with_duration",
            "last_seen",
            "last_model",
            "last_endpoint",
            "last_key_prefix",
            "last_key_fingerprint",
            "last_auth_header",
            "last_source_ip",
            "last_forwarded_for",
            "last_host",
            "last_origin",
            "last_referer",
            "last_user_agent",
            "project_prefix",
            "metadata_client",
            "metadata_note",
        )
        for client_id, client_payload in clients.items():
            if not isinstance(client_payload, dict):
                continue
            bucket = self._new_client_bucket()
            for field in scalar_fields:
                if field in client_payload:
                    bucket[field] = client_payload[field]
            bucket["categories"] = Counter(client_payload.get("categories", {}))
            bucket["endpoints"] = Counter(client_payload.get("endpoints", {}))
            bucket["methods"] = Counter(client_payload.get("methods", {}))
            self._clients[str(client_id)] = bucket

    def reset(self) -> None:
        """Clear all tracked usage and restart the local counters."""
        with self._lock:
            self._clear_state_locked()
            self._save_locked()

    def _apply_attribution(self, bucket: dict[str, Any], attribution: Optional[dict[str, Any]]) -> None:
        """Merge request attribution into the per-client usage bucket."""
        if not isinstance(attribution, dict):
            return

        field_map = {
            "key_prefix": "last_key_prefix",
            "key_fingerprint": "last_key_fingerprint",
            "header_name": "last_auth_header",
            "source_ip": "last_source_ip",
            "forwarded_for": "last_forwarded_for",
            "host": "last_host",
            "origin": "last_origin",
            "referer": "last_referer",
            "user_agent": "last_user_agent",
            "project_prefix": "project_prefix",
            "metadata_client": "metadata_client",
            "metadata_note": "metadata_note",
        }
        for source_field, bucket_field in field_map.items():
            value = attribution.get(source_field)
            if value not in (None, ""):
                bucket[bucket_field] = value

    def record_request(
        self,
        *,
        client_id: Optional[str],
        endpoint: str,
        method: str,
        status_code: int,
        model: Optional[str] = None,
        duration_ms: Optional[float] = None,
        request_bytes: object = 0,
        response_bytes: object = 0,
        streamed: bool = False,
        attribution: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a completed API request."""
        now = time.time()
        normalized_client = client_id.strip() if isinstance(client_id, str) and client_id.strip() else None
        category = _category_for_endpoint(endpoint)
        request_byte_count = _safe_int(request_bytes)
        response_byte_count = _safe_int(response_bytes)
        duration_value = round(float(duration_ms), 1) if duration_ms is not None else None
        request_row: dict[str, Any] = {
            "timestamp": now,
            "client_id": normalized_client or "unauthenticated",
            "endpoint": endpoint,
            "method": method,
            "status_code": int(status_code),
            "model": model,
            "streamed": bool(streamed),
            "duration_ms": duration_value,
            "request_bytes": request_byte_count,
            "response_bytes": response_byte_count,
            "category": category,
        }
        if isinstance(attribution, dict):
            for field in (
                "project_prefix",
                "key_prefix",
                "key_fingerprint",
                "header_name",
                "source_ip",
                "forwarded_for",
                "host",
                "origin",
                "referer",
                "user_agent",
                "metadata_client",
                "metadata_note",
            ):
                value = attribution.get(field)
                if value not in (None, ""):
                    request_row[field] = value

        with self._lock:
            self.total_requests += 1
            self._endpoint_counts[endpoint] += 1
            if status_code >= 400:
                self.total_errors += 1
            if streamed:
                self.streaming_requests += 1
            if normalized_client is None:
                self.unauthenticated_requests += 1
            self.total_request_bytes += request_byte_count
            self.total_response_bytes += response_byte_count
            if duration_value is not None:
                self.total_duration_ms += duration_value
                self.requests_with_duration += 1
            self._recent_requests.append(request_row)

            if normalized_client is None:
                self._save_locked()
                return

            bucket = self._clients[normalized_client]
            bucket["requests"] += 1
            bucket["errors"] += int(status_code >= 400)
            bucket["streaming_requests"] += int(streamed)
            bucket["request_bytes"] += request_byte_count
            bucket["response_bytes"] += response_byte_count
            if duration_value is not None:
                bucket["duration_total_ms"] += duration_value
                bucket["requests_with_duration"] += 1
            bucket["last_seen"] = now
            bucket["last_endpoint"] = endpoint
            bucket["last_model"] = model or bucket["last_model"]
            bucket["categories"][category] += 1
            bucket["endpoints"][endpoint] += 1
            bucket["methods"][method] += 1
            self._apply_attribution(bucket, attribution)
            self._save_locked()

    def record_tokens(
        self,
        *,
        client_id: Optional[str],
        endpoint: str,
        model: Optional[str],
        prompt_tokens: object = 0,
        completion_tokens: object = 0,
    ) -> None:
        """Record token usage for a request when the backend reports it."""
        prompt_count = _safe_int(prompt_tokens)
        completion_count = _safe_int(completion_tokens)
        total_count = prompt_count + completion_count
        normalized_client = client_id.strip() if isinstance(client_id, str) and client_id.strip() else None

        if total_count == 0 and normalized_client is None and model is None:
            return

        with self._lock:
            self.prompt_tokens += prompt_count
            self.completion_tokens += completion_count
            self.total_tokens += total_count

            if normalized_client is None:
                self._save_locked()
                return

            bucket = self._clients[normalized_client]
            bucket["prompt_tokens"] += prompt_count
            bucket["completion_tokens"] += completion_count
            bucket["total_tokens"] += total_count
            bucket["last_seen"] = time.time()
            bucket["last_endpoint"] = endpoint
            bucket["last_model"] = model or bucket["last_model"]
            self._save_locked()

    def snapshot(self, top_n: int = 10, recent_n: int = 20, endpoint_n: int = 10) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for UI polling."""
        now = time.time()
        with self._lock:
            uptime_seconds = max(now - self.started_at, 0.0)
            recent_rows = list(self._recent_requests)
            top_clients = []
            for client_id, bucket in self._clients.items():
                requests = int(bucket["requests"])
                errors = int(bucket["errors"])
                top_endpoint = None
                if bucket["endpoints"]:
                    top_endpoint = bucket["endpoints"].most_common(1)[0][0]
                avg_duration_ms = 0.0
                if int(bucket["requests_with_duration"]):
                    avg_duration_ms = round(
                        float(bucket["duration_total_ms"]) / int(bucket["requests_with_duration"]),
                        1,
                    )
                top_clients.append(
                    {
                        "client_id": client_id,
                        "requests": requests,
                        "errors": errors,
                        "error_rate_pct": round((errors / requests) * 100, 1) if requests else 0.0,
                        "streaming_requests": int(bucket["streaming_requests"]),
                        "prompt_tokens": int(bucket["prompt_tokens"]),
                        "completion_tokens": int(bucket["completion_tokens"]),
                        "total_tokens": int(bucket["total_tokens"]),
                        "request_bytes": int(bucket["request_bytes"]),
                        "response_bytes": int(bucket["response_bytes"]),
                        "avg_duration_ms": avg_duration_ms,
                        "last_seen": bucket["last_seen"],
                        "last_model": bucket["last_model"],
                        "last_endpoint": bucket["last_endpoint"],
                        "top_endpoint": top_endpoint,
                        "last_key_prefix": bucket["last_key_prefix"],
                        "last_key_fingerprint": bucket["last_key_fingerprint"],
                        "last_auth_header": bucket["last_auth_header"],
                        "last_source_ip": bucket["last_source_ip"],
                        "last_forwarded_for": bucket["last_forwarded_for"],
                        "last_host": bucket["last_host"],
                        "last_origin": bucket["last_origin"],
                        "last_referer": bucket["last_referer"],
                        "last_user_agent": bucket["last_user_agent"],
                        "project_prefix": bucket["project_prefix"],
                        "metadata_client": bucket["metadata_client"],
                        "metadata_note": bucket["metadata_note"],
                        "categories": dict(bucket["categories"]),
                    }
                )

            top_clients.sort(key=lambda row: (row["requests"], row["total_tokens"]), reverse=True)
            endpoints = [
                {"endpoint": endpoint, "requests": count}
                for endpoint, count in self._endpoint_counts.most_common(endpoint_n)
            ]
            requests_last_5m = sum(1 for row in recent_rows if now - float(row["timestamp"]) <= 300)
            requests_last_hour = sum(1 for row in recent_rows if now - float(row["timestamp"]) <= 3600)
            average_duration_ms = 0.0
            if self.requests_with_duration:
                average_duration_ms = round(self.total_duration_ms / self.requests_with_duration, 1)

            return {
                "summary": {
                    "started_at": self.started_at,
                    "uptime_seconds": round(uptime_seconds, 1),
                    "total_requests": int(self.total_requests),
                    "total_errors": int(self.total_errors),
                    "error_rate_pct": round((self.total_errors / self.total_requests) * 100, 1)
                    if self.total_requests
                    else 0.0,
                    "unauthenticated_requests": int(self.unauthenticated_requests),
                    "streaming_requests": int(self.streaming_requests),
                    "unique_clients": len(self._clients),
                    "prompt_tokens": int(self.prompt_tokens),
                    "completion_tokens": int(self.completion_tokens),
                    "total_tokens": int(self.total_tokens),
                    "total_request_bytes": int(self.total_request_bytes),
                    "total_response_bytes": int(self.total_response_bytes),
                    "average_duration_ms": average_duration_ms,
                    "requests_last_5m": requests_last_5m,
                    "requests_last_hour": requests_last_hour,
                    "requests_per_minute": round(self.total_requests / max(uptime_seconds / 60.0, 1 / 60.0), 2)
                    if self.total_requests
                    else 0.0,
                },
                "top_clients": top_clients[:top_n],
                "top_endpoints": endpoints,
                "recent_requests": list(reversed(recent_rows[-recent_n:])),
            }
