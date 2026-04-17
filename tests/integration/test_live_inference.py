"""
Integration tests for Guardian — hits the LIVE proxy on localhost:11434.

These tests require:
  1. Guardian running (systemctl --user status llama-guardian)
  2. A valid API key in config/api_keys.json
  3. At least one model configured in config/models.yaml

Run:
    pytest tests/integration/ -v --timeout=300

Skip from normal unit test runs:
    pytest tests/unit/              # fast, no LLM needed
    pytest tests/ -m "not integration"  # same effect
"""

import json
import os
import time
from typing import Generator

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GUARDIAN_URL = os.environ.get("GUARDIAN_URL", "http://127.0.0.1:11434")
API_KEY = os.environ.get(
    "GUARDIAN_TEST_KEY",
    # Auto-detect: read first key from config/api_keys.json
    None,
)

# How long to wait for model load + inference (first request may cold-start)
REQUEST_TIMEOUT = 300.0
# Short timeout for endpoints that should never block (status, models)
FAST_TIMEOUT = 15.0


def _resolve_api_key() -> str:
    """Resolve API key from env or config file."""
    if API_KEY:
        return API_KEY
    keys_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "api_keys.json"
    )
    keys_path = os.path.normpath(keys_path)
    if os.path.exists(keys_path):
        with open(keys_path) as f:
            keys = json.load(f)
        if keys:
            return next(iter(keys))
    pytest.skip("No API key available — set GUARDIAN_TEST_KEY or add to config/api_keys.json")


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_resolve_api_key()}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def auth_headers() -> dict:
    return _auth_headers()


@pytest.fixture(scope="module")
def client() -> Generator[httpx.Client, None, None]:
    with httpx.Client(
        base_url=GUARDIAN_URL,
        headers=_auth_headers(),
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
    ) as c:
        yield c


@pytest.fixture(scope="module")
def fast_client() -> Generator[httpx.Client, None, None]:
    """Client with short timeout for non-inference endpoints."""
    with httpx.Client(
        base_url=GUARDIAN_URL,
        headers=_auth_headers(),
        timeout=httpx.Timeout(FAST_TIMEOUT, connect=5.0),
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Precondition check
# ---------------------------------------------------------------------------

def _guardian_reachable() -> bool:
    try:
        resp = httpx.get(
            f"{GUARDIAN_URL}/api/status",
            headers=_auth_headers(),
            timeout=5.0,
        )
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _guardian_reachable(),
        reason=f"Guardian not reachable at {GUARDIAN_URL}",
    ),
]


# ===================================================================
# Status & Metadata (no inference, always fast)
# ===================================================================

class TestStatus:
    """Tests that hit metadata endpoints — no LLM inference needed."""

    def test_api_status(self, fast_client: httpx.Client):
        """GET /api/status returns model info and VRAM data."""
        resp = fast_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_model" in data
        assert "vram" in data
        assert "backend_url" in data

    def test_api_status_vram_fields(self, fast_client: httpx.Client):
        """VRAM data contains used/free/total."""
        data = fast_client.get("/api/status").json()
        vram = data["vram"]
        assert "used" in vram
        assert "free" in vram
        assert "total" in vram
        assert vram["total"] > 0

    def test_v1_models(self, fast_client: httpx.Client):
        """GET /v1/models returns OpenAI-compatible model list."""
        resp = fast_client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert len(data["data"]) > 0
        # Each model entry should have an id
        for model in data["data"]:
            assert "id" in model

    def test_api_tags(self, fast_client: httpx.Client):
        """GET /api/tags returns Ollama-compatible model list."""
        resp = fast_client.get("/api/tags")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert len(data["models"]) > 0

    def test_queue_status(self, fast_client: httpx.Client):
        """GET /v1/queue/status returns queue state."""
        resp = fast_client.get("/v1/queue/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue_length" in data
        assert "active_count" in data
        assert "max_concurrent" in data
        assert "queue_timeout_s" in data
        assert "stats" in data

    def test_metrics_endpoint(self, fast_client: httpx.Client):
        """GET /metrics returns Prometheus metrics."""
        resp = fast_client.get("/metrics")
        assert resp.status_code == 200
        assert "guardian_" in resp.text
        assert "HELP" in resp.text


# ===================================================================
# Authentication
# ===================================================================

class TestAuth:
    """Test auth enforcement on live server."""

    def test_missing_auth_rejected(self):
        """Requests without auth header get 401."""
        resp = httpx.get(f"{GUARDIAN_URL}/api/status", timeout=5.0)
        assert resp.status_code in (401, 403)

    def test_invalid_key_rejected(self):
        """Requests with bad key get 401."""
        resp = httpx.get(
            f"{GUARDIAN_URL}/api/status",
            headers={"Authorization": "Bearer fake_00000000000000000000000000000000"},
            timeout=5.0,
        )
        assert resp.status_code in (401, 403)

    def test_valid_key_accepted(self, fast_client: httpx.Client):
        """Requests with valid key succeed."""
        resp = fast_client.get("/api/status")
        assert resp.status_code == 200


# ===================================================================
# Inference — Blocking (requires model load, may be slow)
# ===================================================================

class TestInferenceBlocking:
    """Tests that do actual LLM inference via blocking requests."""

    def test_chat_completions_basic(self, client: httpx.Client):
        """POST /v1/chat/completions returns a valid response."""
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
                "max_tokens": 10,
                "temperature": 0.0,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        data = resp.json()
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert "message" in data["choices"][0]
        content = data["choices"][0]["message"]["content"]
        assert len(content) > 0, "Model returned empty content"

    def test_chat_completions_has_usage(self, client: httpx.Client):
        """Response includes token usage stats."""
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Say hello"}],
                "max_tokens": 5,
                "temperature": 0.0,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        usage = data.get("usage", {})
        assert usage.get("prompt_tokens", 0) > 0, "Missing prompt_tokens"
        assert usage.get("completion_tokens", 0) > 0, "Missing completion_tokens"

    def test_response_has_queue_headers(self, client: httpx.Client):
        """Response includes X-Request-Id and X-Queue-Wait-Ms headers."""
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 3,
                "temperature": 0.0,
            },
        )
        assert resp.status_code == 200
        assert "X-Request-Id" in resp.headers, f"Missing X-Request-Id. Headers: {dict(resp.headers)}"
        assert "X-Queue-Wait-Ms" in resp.headers, f"Missing X-Queue-Wait-Ms. Headers: {dict(resp.headers)}"
        # Queue wait should be a non-negative number
        wait_ms = int(resp.headers["X-Queue-Wait-Ms"])
        assert wait_ms >= 0

    def test_completions_endpoint(self, client: httpx.Client):
        """POST /v1/completions (non-chat) returns text."""
        resp = client.post(
            "/v1/completions",
            json={
                "model": "auto",
                "prompt": "The capital of France is",
                "max_tokens": 10,
                "temperature": 0.0,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        assert len(data["choices"]) > 0
        text = data["choices"][0].get("text", "")
        assert len(text) > 0, "Model returned empty text"

    def test_ollama_chat_endpoint(self, client: httpx.Client):
        """POST /api/chat (Ollama format) returns a response."""
        resp = client.post(
            "/api/chat",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Say OK"}],
                "stream": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Ollama format has message.content at top level
        assert "message" in data
        assert "content" in data["message"]
        assert len(data["message"]["content"]) > 0


# ===================================================================
# Inference — Streaming
# ===================================================================

class TestInferenceStreaming:
    """Tests that verify SSE streaming works end-to-end."""

    def test_streaming_produces_chunks(self, auth_headers: dict):
        """Streaming response yields multiple SSE data lines."""
        chunks = []
        with httpx.stream(
            "POST",
            f"{GUARDIAN_URL}/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Count from 1 to 5"}],
                "max_tokens": 30,
                "temperature": 0.0,
                "stream": True,
            },
            headers=auth_headers,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
        ) as resp:
            assert resp.status_code == 200

            # Queue headers should be on the initial response
            assert "X-Request-Id" in resp.headers

            for line in resp.iter_lines():
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    chunks.append(json.loads(payload))

        assert len(chunks) > 1, f"Expected multiple chunks, got {len(chunks)}"

        # At least some chunks should have content deltas
        deltas = [
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta")
        ]
        combined = "".join(deltas)
        assert len(combined) > 0, "Stream produced no content"

    def test_streaming_has_queue_headers(self, auth_headers: dict):
        """Streaming responses include queue wait header on initial response."""
        with httpx.stream(
            "POST",
            f"{GUARDIAN_URL}/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 3,
                "stream": True,
            },
            headers=auth_headers,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
        ) as resp:
            assert resp.status_code == 200
            assert "X-Queue-Wait-Ms" in resp.headers
            # Consume stream to avoid connection issues
            for line in resp.iter_lines():
                if line.startswith("data: [DONE]"):
                    break


# ===================================================================
# Queue behavior under load
# ===================================================================

class TestQueueBehavior:
    """Verify queue status reflects reality during inference."""

    def test_status_shows_active_during_inference(self, auth_headers: dict):
        """While inference is running, queue status should show active request."""
        import threading

        status_snapshots = []
        stop_event = threading.Event()

        def poll_status():
            """Background thread polling queue status."""
            poll_client = httpx.Client(
                base_url=GUARDIAN_URL,
                headers=auth_headers,
                timeout=5.0,
            )
            while not stop_event.is_set():
                try:
                    resp = poll_client.get("/v1/queue/status")
                    if resp.status_code == 200:
                        status_snapshots.append(resp.json())
                except (httpx.RequestError, httpx.TimeoutException):
                    pass
                time.sleep(0.5)
            poll_client.close()

        # Start polling
        poller = threading.Thread(target=poll_status, daemon=True)
        poller.start()

        try:
            # Send a slow-ish inference request
            resp = httpx.post(
                f"{GUARDIAN_URL}/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Write a 3-sentence story about a cat."}],
                    "max_tokens": 80,
                    "temperature": 0.5,
                },
                headers=auth_headers,
                timeout=REQUEST_TIMEOUT,
            )
            assert resp.status_code == 200
        finally:
            stop_event.set()
            poller.join(timeout=3.0)

        # At least one snapshot should show active_count > 0
        active_snapshots = [s for s in status_snapshots if s.get("active_count", 0) > 0]
        assert len(active_snapshots) > 0, (
            f"Queue never showed active request across {len(status_snapshots)} polls. "
            f"Snapshots: {status_snapshots[:3]}"
        )
