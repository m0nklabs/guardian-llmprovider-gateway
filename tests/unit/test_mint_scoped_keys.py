"""The scoped-key minting script: dry run, idempotency, and never leaking tokens.

The script exists because the 2026-11-11 scope deadline turns every legacy key
into a plain ``client`` key. Its failure modes are the expensive kind — minting
into a store it should not have touched, or printing a token into a log — so
each one is pinned here.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import mint_scoped_keys as mint  # noqa: E402


def _store(tmp_path: Path, entries: dict | None = None) -> Path:
    path = tmp_path / "guardian.keys.yaml"
    if entries is not None:
        path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_a_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    path = _store(tmp_path)
    assert mint.main(["--store", str(path)]) == 0
    assert not path.exists(), "a dry run must not create the store"
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "flip_" not in out, "a dry run must never print a token"


def test_apply_mints_the_four_keys_with_their_scopes(tmp_path: Path, capsys) -> None:
    path = _store(tmp_path)
    assert mint.main(["--store", str(path), "--apply"]) == 0
    store = _read(path)

    by_name = {entry["name"]: entry for entry in store.values()}
    assert set(by_name) == {"dashboard", "operator-tools", "capture-export", "lifecycle"}
    assert by_name["dashboard"]["metadata"]["scopes"] == ["admin"]
    assert by_name["operator-tools"]["metadata"]["scopes"] == ["admin"]
    assert by_name["capture-export"]["metadata"]["scopes"] == ["capture-export"]
    assert by_name["lifecycle"]["metadata"]["scopes"] == ["operator"]

    out = capsys.readouterr().out
    for token in store:
        assert token in out, "each minted token is printed once, on apply"


def test_a_second_run_is_a_no_op(tmp_path: Path, capsys) -> None:
    path = _store(tmp_path)
    mint.main(["--store", str(path), "--apply"])
    before = _read(path)
    capsys.readouterr()

    assert mint.main(["--store", str(path), "--apply"]) == 0
    assert _read(path) == before, "an idempotent run must not mint duplicates"
    assert "Nothing to mint" in capsys.readouterr().out


def test_force_replaces_a_key_and_retires_the_old_token(tmp_path: Path) -> None:
    path = _store(tmp_path)
    mint.main(["--store", str(path), "--apply"])
    old = {t for t, e in _read(path).items() if e["name"] == "dashboard"}

    mint.main(["--store", str(path), "--apply", "--force"])
    store = _read(path)
    new = {t for t, e in store.items() if e["name"] == "dashboard"}

    assert len(new) == 1
    assert new != old
    assert not (old & set(store)), "the replaced token must be gone, not left valid"


def test_an_existing_key_with_other_scopes_is_left_alone(tmp_path: Path, capsys) -> None:
    """Without --force, a hand-made key of the same name is not overwritten."""
    path = _store(tmp_path, {"flip_existing": {
        "name": "dashboard", "created_at": 1.0,
        "metadata": {"scopes": ["client"]}, "cloud_gateway_access": True,
    }})
    mint.main(["--store", str(path), "--apply"])
    store = _read(path)
    assert store["flip_existing"]["metadata"]["scopes"] == ["client"]
    assert "skip" in capsys.readouterr().out


def test_a_malformed_store_is_refused_and_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "guardian.keys.yaml"
    path.write_text("this: [is not, a mapping\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        mint.main(["--store", str(path), "--apply"])
    assert "refusing to touch" in str(exc.value)
    assert path.read_text(encoding="utf-8") == before


def test_a_non_mapping_store_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "guardian.keys.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mint.main(["--store", str(path), "--apply"])


def test_json_output_never_contains_a_token(tmp_path: Path, capsys) -> None:
    path = _store(tmp_path)
    mint.main(["--store", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in payload["plan"]] == [
        "dashboard", "operator-tools", "capture-export", "lifecycle",
    ]
    assert "flip_" not in json.dumps(payload)


def test_the_store_mode_is_preserved(tmp_path: Path) -> None:
    path = _store(tmp_path, {})
    path.chmod(0o600)
    mint.main(["--store", str(path), "--apply"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_minted_tokens_are_unique(tmp_path: Path) -> None:
    path = _store(tmp_path)
    mint.main(["--store", str(path), "--apply"])
    tokens = list(_read(path))
    assert len(tokens) == len(set(tokens)) == 4
