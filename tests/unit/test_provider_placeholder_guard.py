"""Config guard: an enabled provider must not point at a placeholder host.

Issue #48: ``config/providers/windows-gpu-local.settings.yaml`` shipped
``enabled: true`` while all four of its URLs pointed at ``192.168.1.W`` — not an
IP literal, not resolvable (``gaierror -2``), 0 ids in ``/v1/models``, and every
routed request a ``503``. It is a per-deployment template that reads like a live
LAN provider; the moment somebody "fixes" the address by guessing, it becomes a
second provider carrying cleartext Bearer keys over four LAN hops (B-03 in
``docs/G0_TRUST_BOUNDARY_THREAT_MODEL.md``).

Enabling such a provider has to be a deliberate decision, so this guard refuses a
committed provider file that is enabled while any of its URLs carries a
placeholder-shaped host. Placeholder shape here means: a host-like token
(``PLACEHOLDER``, ``<host>``, ``{host}``, ``$HOST``), or an IPv4-shaped address
whose octets are not all numeric (``192.168.1.W``, ``192.168.1.x``).

``enabled`` defaults to true when the key is absent (``app/proxy/providers.py``
reads ``bool(cfg.get("enabled", True))``), so a file without the key is treated
as enabled — otherwise deleting the line would be a silent way around this guard.

Stdlib + PyYAML only: the committed files are read as text, nothing is imported
from ``app``, no process or network is touched.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_DIR = REPO_ROOT / "config" / "providers"

#: Tokens that only ever appear in a placeholder, never in a real host.
PLACEHOLDER_TOKENS = ("placeholder", "<", ">", "{", "}", "$")
PLACEHOLDER_HOSTS = frozenset({"host", "hostname", "host-or-ip", "your-host"})

FIX_HINT = (
    "An enabled provider whose URL cannot resolve is dead weight in discovery and "
    "health checks, and the day someone fills in a guessed address it becomes a live "
    "bypass nobody reviewed.\n"
    "  Fix: set ``enabled: false`` and keep the placeholder documented in the file "
    "header, or commit the real address through a deliberate, reviewed change. "
    "Never guess an address to make a guard green."
)


def url_host(value: str) -> str:
    """The host of ``value`` with its original casing (``urlsplit`` lowercases).

    Keeping the case matters for the failure message: the operator must see
    ``192.168.1.W`` in the report, because that is the token to search for.
    """
    netloc = urlsplit(value).netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if netloc.startswith("["):  # IPv6 literal
        return netloc.split("]", 1)[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def url_hosts(node, prefix: str = "") -> list[tuple[str, str]]:
    """Every ``(dotted key, host)`` pair of a provider document.

    A URL is any string value under a key ending in ``url`` (``base_url``,
    ``management_url``, ``tts_url``, ``stt_url``, …), including nested endpoint
    blocks. Relative values (``catalog_url: /models``) have no host and are
    skipped: they are appended to ``base_url`` and inherit its placeholder.
    """
    pairs: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                pairs.extend(url_hosts(value, dotted))
                continue
            if not isinstance(value, str) or not str(key).lower().endswith("url"):
                continue
            host = url_host(value)
            if host:
                pairs.append((dotted, host))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            pairs.extend(url_hosts(value, f"{prefix}[{index}]"))
    return pairs


def is_placeholder_host(host: str) -> bool:
    """True when ``host`` is visibly a per-deployment placeholder."""
    lowered = host.lower()
    if any(token in lowered for token in PLACEHOLDER_TOKENS):
        return True
    if lowered in PLACEHOLDER_HOSTS:
        return True
    octets = host.split(".")
    # An IPv4-shaped host with a non-numeric octet. Requiring three leading
    # numeric labels keeps ordinary hostnames out of it (no domain looks like
    # 192.168.1.<word>), which is exactly the shape #48 shipped.
    return len(octets) == 4 and all(octet.isdigit() for octet in octets[:3]) and not octets[3].isdigit()


def placeholder_violations(provider_file: Path, document=None) -> list[str]:
    """Placeholder URL reasons for one provider document, empty when clean."""
    if document is None:
        document = yaml.safe_load(provider_file.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        return []
    # Absent ``enabled`` means enabled, matching the loader.
    if not document.get("enabled", True):
        return []
    return [
        f"{key} → host {host!r}"
        for key, host in url_hosts(document)
        if is_placeholder_host(host)
    ]


# ── Tests ──────────────────────────────────────────────────────────────


def test_no_enabled_provider_points_at_a_placeholder_host():
    """A committed provider file may be enabled only with real addresses."""
    files = sorted(PROVIDERS_DIR.glob("*.yaml"))
    assert files, f"no provider files under {PROVIDERS_DIR} — this guard would be blind"

    violations: dict[str, list[str]] = {}
    for path in files:
        reasons = placeholder_violations(path)
        if reasons:
            violations[path.relative_to(REPO_ROOT).as_posix()] = reasons

    assert not violations, (
        "these committed provider files are enabled while pointing at a placeholder host:\n  "
        + "\n  ".join(f"{name}: " + "; ".join(reasons) for name, reasons in violations.items())
        + "\n"
        + FIX_HINT
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.168.1.W", True),
        ("192.168.1.x", True),
        ("10.0.0.ABC", True),
        ("PLACEHOLDER", True),
        ("placeholder.local", True),
        ("<host>", True),
        ("{host}", True),
        ("$WINDOWS_HOST", True),
        ("hostname", True),
        ("192.168.1.245", False),
        ("127.0.0.1", False),
        ("api.openai.com", False),
        ("generativelanguage.googleapis.com", False),
        ("inference.poolside.ai", False),
    ],
)
def test_placeholder_host_detection(host: str, expected: bool):
    """The shape classifier: IPv4-with-a-word and visible tokens are placeholders."""
    assert is_placeholder_host(host) is expected


def test_guard_fires_on_a_placeholder_provider_and_clears_a_real_one(tmp_path: Path):
    """The file-level guard itself: enabled+placeholder fails, disabled passes."""
    template = """
        local: false
        enabled: {enabled}
        base_url: http://{host}:11440/v1
        management_url: http://{host}:11441
        api_key: ${{WINDOWS_LAN_KEY}}
    """
    placeholder = tmp_path / "template.settings.yaml"
    placeholder.write_text(template.format(enabled="true", host="192.168.1.W"), encoding="utf-8")
    disabled = tmp_path / "template-disabled.settings.yaml"
    disabled.write_text(template.format(enabled="false", host="192.168.1.W"), encoding="utf-8")
    real = tmp_path / "real.settings.yaml"
    real.write_text(template.format(enabled="true", host="192.168.1.245"), encoding="utf-8")

    reasons = placeholder_violations(placeholder)
    assert len(reasons) == 2, reasons
    assert "base_url" in reasons[0] and "management_url" in reasons[1]
    assert placeholder_violations(disabled) == []
    assert placeholder_violations(real) == []


def test_missing_enabled_key_counts_as_enabled():
    """Deleting ``enabled`` must not be a way around the guard."""
    document = {"base_url": "http://192.168.1.W:11440/v1"}
    assert placeholder_violations(Path("synthetic.settings.yaml"), document) == [
        "base_url → host '192.168.1.W'"
    ]


def test_shipped_windows_provider_is_disabled_and_documents_the_placeholder():
    """The #48 file: disabled, and its header says why (no guessed address).

    The pin exists so the file cannot drift back to ``enabled: true`` without
    this test going red next to the config guard above.
    """
    path = PROVIDERS_DIR / "windows-gpu-local.settings.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["enabled"] is False
    header = " ".join(
        line.lstrip("# ").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("#")
    ).lower()
    assert "placeholder" in header, (
        "the header must say plainly that 192.168.1.W is a per-deployment "
        "placeholder; enabling it is a deliberate decision, not a config fix"
    )
