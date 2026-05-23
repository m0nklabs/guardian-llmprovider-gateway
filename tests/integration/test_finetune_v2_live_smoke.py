"""Opt-in live smoke checks for the Guardian-backed finetune v2 path."""

import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from app.tweaker.finetune_v2_runner import FinetuneV2Runner, GuardianV2ProbeRunner


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


def test_live_guardian_finetune_v2_fixed_shape_dry_run(tmp_path: Path):
    guardian_url = os.environ.get("GUARDIAN_URL", "http://127.0.0.1:11434")
    api_key = os.environ.get("GUARDIAN_TEST_KEY")
    model = os.environ.get("FINETUNE_V2_LIVE_MODEL")
    context = os.environ.get("FINETUNE_V2_LIVE_CONTEXT")
    ngl = os.environ.get("FINETUNE_V2_LIVE_NGL")
    split = os.environ.get("FINETUNE_V2_LIVE_SPLIT", "0.50,0.50")
    models_config = Path(os.environ.get("FINETUNE_V2_LIVE_MODELS_CONFIG", "config/models.yaml"))
    if not api_key:
        pytest.skip("set GUARDIAN_TEST_KEY for live finetune v2 smoke checks")
    if not model or not context or not ngl:
        pytest.skip("set FINETUNE_V2_LIVE_MODEL, FINETUNE_V2_LIVE_CONTEXT, and FINETUNE_V2_LIVE_NGL")
    before = models_config.read_bytes()
    probe_runner = GuardianV2ProbeRunner(
        guardian_url=guardian_url,
        api_key=api_key,
        smoke_prompt=os.environ.get("FINETUNE_V2_LIVE_SMOKE_PROMPT", "Reply with exactly: FIT OK"),
        smoke_max_tokens=int(os.environ.get("FINETUNE_V2_LIVE_SMOKE_MAX_TOKENS", "8")),
        smoke_image_url=os.environ.get("FINETUNE_V2_LIVE_SMOKE_IMAGE_URL"),
    )
    runner = FinetuneV2Runner(
        models_config_path=models_config,
        results_file=tmp_path / "model_finetune_v2_results.json",
        probe_runner=probe_runner,
        runtime_mode=os.environ.get("FINETUNE_V2_LIVE_RUNTIME_MODE", "auto"),
    )

    try:
        result = runner.tune_model(
            model,
            optimization=os.environ.get("FINETUNE_V2_LIVE_OPTIMIZATION", "speed"),
            fixed_context=int(context),
            fixed_ngl=int(ngl),
            split_candidates=[split],
            apply=False,
        )
    finally:
        probe_runner.close()

    assert models_config.read_bytes() == before
    assert result.applied is False
    assert result.winner.success is True
    assert result.winner_explanation["winner_reason"]["code"].endswith("_winner")
