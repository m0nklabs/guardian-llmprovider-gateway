"""Unit tests for the speech-endpoint engine payload mapping."""

from app.gateway.tts import _build_engine_payload


def test_design_payload_unchanged_for_openai_clients():
    body = {"input": "hello", "voice": "nova", "seed": 7, "response_format": "wav"}
    payload = _build_engine_payload(body, {})
    assert payload["text"] == "hello"
    assert payload["instruct"]  # voice mapped into an instruct
    assert payload["seed"] == 7
    assert "ref_audio" not in payload and "language" not in payload


def test_clone_fields_pass_through_when_present():
    body = {
        "input": "hello there",
        "voice": "Trump",
        "ref_audio": "trump.wav",
        "ref_text": "Make voice cloning great again.",
        "language": "english",
        "seed": 3,
        "zero_shot": True,
    }
    payload = _build_engine_payload(body, {})
    assert payload["ref_audio"] == "trump.wav"
    assert payload["ref_text"] == "Make voice cloning great again."
    assert payload["language"] == "english"
    assert payload["zero_shot"] is True
    assert payload["instruct"] == "Trump"  # voice still maps to instruct for design fallback


def test_none_and_empty_values_are_dropped():
    payload = _build_engine_payload({"input": "x", "ref_audio": None, "language": ""}, {})
    assert "ref_audio" not in payload
    assert "language" not in payload


def test_zero_seed_survives_falsy_drop_rule():
    """seed=0 is a legitimate deterministic seed — the empty-string drop rule
    must not swallow falsy numbers."""
    payload = _build_engine_payload({"input": "x", "seed": 0, "ref_audio": ""}, {})
    assert payload["seed"] == 0
    assert "ref_audio" not in payload
