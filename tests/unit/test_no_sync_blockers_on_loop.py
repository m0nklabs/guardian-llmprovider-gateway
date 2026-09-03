"""Structural guard: the asyncio event loop must NEVER block.

The gateway serves local AND cloud routes from one process. Any synchronous
blocking call (subprocess.run, requests.*, time.sleep, ...) that executes on
the event loop stalls EVERY in-flight request — visible as micro-gaps in
streaming responses. Design rule (operator, 2026-09-02): modular, gap-free
operation; a gap is a structural bug, not an accepted cost.

Enforcement: an AST scan of hot-path modules. A forbidden blocking call is
only legal when
  1. it lives in a function explicitly named ``*_sync`` (the convention for
     helpers destined for ``asyncio.to_thread``), or
  2. it is listed in ALLOWLIST with a documented reason (startup-only,
     provably off-loop, or sub-millisecond by contract).

New blocking code must either adopt the ``*_sync`` + ``to_thread`` pattern or
add an allowlist entry with a reason — silently reintroducing a loop block
fails CI.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Hot-path modules: request serving + engine operations that fire while
# requests are in flight. Startup-only modules stay out of scope.
MODULES = [
    "app/engine/manager.py",
    "app/gateway/routing.py",
    "app/gateway/streaming.py",
    "app/gateway/queue_helpers.py",
    "app/gateway/admin_api.py",
    "app/cloud_inference/forwarding.py",
    "app/cloud_inference/routing.py",
    "app/local_inference/ollama.py",
    "app/local_inference/models.py",
    "app/local_inference/model_registry.py",
    "app/proxy/server.py",
    "app/proxy/metrics.py",
    "app/proxy/usage.py",
    "app/proxy/auth.py",
    "app/proxy/process.py",
    "app/proxy/lifespan.py",
    "app/scheduler/manager.py",
    "app/capture/wal_writer.py",
    "app/capture/integration.py",
]

FORBIDDEN_ATTR_CALLS = {
    ("subprocess", "run"),
    ("subprocess", "check_output"),
    ("subprocess", "check_call"),
    ("subprocess", "call"),
    ("os", "system"),
    ("os", "fsync"),
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "request"),
    ("time", "sleep"),
    ("gzip", "GzipFile"),
}

# (module-relative key, function name, forbidden call) -> reason. Every entry
# needs a reason; keep it short but concrete.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    # pgrep in _get_backend_model_path/_backend_cmdline is legal: every caller
    # must go through asyncio.to_thread (enforced by review; the *_sync-style
    # name convention does not apply because these predate it).
    (
        "app/engine/manager.py",
        "_get_backend_model_path",
        "subprocess.run",
    ): "pgrep timeout=5 — all call sites offload via asyncio.to_thread",
    (
        "app/engine/manager.py",
        "_backend_cmdline",
        "subprocess.run",
    ): "pgrep timeout=5 — callers offload via asyncio.to_thread",
    (
        "app/engine/manager.py",
        "_log_gpu_processes_sync",
        "subprocess.run",
    ): "nvidia-smi logger helper — runs via asyncio.to_thread in _free_gpu_memory",
    (
        "app/local_inference/models.py",
        "get_gpu_metrics",
        "subprocess.check_output",
    ): "nvidia-smi metrics — every async call site offloads via asyncio.to_thread",
    (
        "app/proxy/metrics.py",
        "update_gpu_metrics",
        "subprocess.run",
    ): "nvidia-smi — async update_gpu_metrics_cached (5s TTL) offloads via to_thread",
    (
        "app/proxy/auth.py",
        "_resolve_local_process_for_port",
        "subprocess.run",
    ): "ss probe only on localhost 401; _log_unauthorized_attempt runs via to_thread",
    (
        "app/proxy/process.py",
        "describe_process",
        "subprocess.run",
    ): "ps probe — every async call site offloads via asyncio.to_thread",
    (
        "app/proxy/process.py",
        "get_proxy_listener_info",
        "subprocess.run",
    ): "ss probe — every async call site offloads via asyncio.to_thread",
    (
        "app/scheduler/manager.py",
        "manage_service",
        "subprocess.run",
    ): "sudo systemctl — async enter/exit_maintenance_mode offload via to_thread",
}


def _iter_files():
    for rel in MODULES:
        p = REPO_ROOT / rel
        if p.exists():
            yield rel, p


def _forbidden_calls_in_function(func: ast.AST) -> list[str]:
    """Return the forbidden ``module.attr`` names called inside *func*."""
    found: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and (f.value.id, f.attr) in FORBIDDEN_ATTR_CALLS
        ):
            found.append(f"{f.value.id}.{f.attr}")
    return found


def _blocking_functions(module_rel: str, path: Path):
    """Yield (function-name, [forbidden calls]) for all FunctionDefs."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = _forbidden_calls_in_function(node)
            if calls:
                yield node.name, calls


@pytest.mark.parametrize("module_rel", MODULES)
def test_no_new_blocking_calls_outside_convention(module_rel):
    """Every blocking call must sit in a *_sync helper or the ALLOWLIST."""
    path = REPO_ROOT / module_rel
    if not path.exists():
        pytest.skip(f"{module_rel} does not exist")
    offenders = []
    for func_name, calls in _blocking_functions(module_rel, path):
        for call in calls:
            if func_name.endswith("_sync"):
                continue  # convention: helper destined for asyncio.to_thread
            if (module_rel, func_name, call) in ALLOWLIST:
                continue
            offenders.append(f"{module_rel}:{func_name} calls {call}")
    assert not offenders, (
        "Blocking call(s) reachable from the event loop — fix with the "
        "*_sync + asyncio.to_thread pattern or add an ALLOWLIST entry with a "
        "documented reason:\n  " + "\n  ".join(offenders)
    )


def test_allowlist_entries_reference_real_code():
    """Allowlist entries must still match the code (no stale exemptions)."""
    stale = [
        key
        for key in ALLOWLIST
        if not (REPO_ROOT / key[0]).exists()
        or not any(func == key[1] for func, _ in _blocking_functions(key[0], REPO_ROOT / key[0]))
    ]
    assert not stale, f"Stale allowlist entries (code changed?): {stale}"
