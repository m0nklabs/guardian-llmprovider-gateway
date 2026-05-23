"""Opt-in live smoke checks for the finetune v2 contract layer."""

import os
from urllib.parse import urlparse

import httpx
import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.finetune_v2_live,
    pytest.mark.skipif(
        os.environ.get("FINETUNE_V2_LIVE") != "1",
        reason="set FINETUNE_V2_LIVE=1 to run live finetune v2 smoke checks",
    ),
]


def test_live_guardian_status_for_finetune_v2_smoke():
    guardian_url = os.environ.get("GUARDIAN_URL", "http://127.0.0.1:11434")
    parsed_url = urlparse(guardian_url)
    if parsed_url.scheme not in {"http", "https"}:
        pytest.fail("GUARDIAN_URL must use http or https")
    api_key = os.environ.get("GUARDIAN_TEST_KEY")
    if not api_key:
        pytest.skip("set GUARDIAN_TEST_KEY for live finetune v2 smoke checks")

    try:
        response = httpx.get(
            f"{guardian_url}/api/status",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"Failed to reach Guardian at {guardian_url}: {exc}")

    assert response.status_code == 200, response.text
    assert isinstance(response.json(), dict)
