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
    # Reported as a mismatch, not as "ready": the migration is incomplete.
    assert "mismatch" in capsys.readouterr().out


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


# ── the five review findings ────────────────────────────────────────────


def test_json_and_apply_are_rejected_together(tmp_path: Path) -> None:
    """The JSON document is token-free; --apply prints tokens. Never both."""
    path = _store(tmp_path)
    with pytest.raises(SystemExit):
        mint.main(["--store", str(path), "--json", "--apply"])
    assert not path.exists(), "the combination must be rejected before any write"


def test_a_yaml_error_does_not_reproduce_the_store_contents(tmp_path: Path) -> None:
    """A parser message quotes the offending line — which can be a token."""
    path = tmp_path / "guardian.keys.yaml"
    secret = "flip_deadbeefdeadbeefdeadbeefdeadbeef"
    path.write_text(f"{secret}: [unclosed\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        mint.main(["--store", str(path), "--apply"])
    message = str(exc.value)
    assert secret not in message, "the token must not appear in the error"
    assert "unparsable YAML" in message
    assert path.read_text(encoding="utf-8").startswith(secret), "the file is untouched"


def test_the_temporary_file_is_never_world_readable(tmp_path: Path, monkeypatch) -> None:
    """Tokens must not be readable by other local users, even mid-write."""
    path = _store(tmp_path)
    seen: list[int] = []
    real_mkstemp = mint.tempfile.mkstemp

    def _spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(stat.S_IMODE(Path(name).stat().st_mode))
        return fd, name

    monkeypatch.setattr(mint.tempfile, "mkstemp", _spy)
    mint.main(["--store", str(path), "--apply"])
    assert seen, "the store must be written through a temporary file"
    assert all(mode == 0o600 for mode in seen), f"temporary modes were {seen}"


def test_a_mismatched_key_is_reported_not_called_ready(tmp_path: Path, capsys) -> None:
    """An existing legacy key must not be presented as a completed migration."""
    path = _store(tmp_path, {"flip_legacy": {
        "name": "dashboard", "created_at": 1.0,
        "metadata": {}, "cloud_gateway_access": True,
    }})
    mint.main(["--store", str(path)])
    out = capsys.readouterr().out
    assert "mismatch" in out
    assert "--force" in out
    assert "every planned key already exists with the right scopes" not in out


def test_the_store_lock_serializes_read_modify_write(tmp_path: Path) -> None:
    """Every concurrent writer's key must survive, not just the last one's.

    Asserting "four correctly named keys at the end" would accept the very
    failure this guards against: each writer could read the empty store, mint
    its own tokens, and the last save would win while the others' printed
    tokens no longer exist. So each writer adds a **distinct** key and every
    one of them must be present afterwards.
    """
    import threading
    import time as _time

    path = _store(tmp_path)
    errors: list[BaseException] = []

    def _writer(index: int) -> None:
        try:
            with mint.store_lock(path):
                store = mint.load_store(path)
                _time.sleep(0.05)  # widen the window the lock must cover
                store[f"flip_writer{index}"] = {
                    "name": f"writer{index}", "created_at": 1.0,
                    "metadata": {"scopes": ["client"]}, "cloud_gateway_access": True,
                }
                mint.save_store(path, store)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    store = _read(path)
    assert sorted(store) == [f"flip_writer{i}" for i in range(4)], (
        "a lost update: some writer's key was overwritten"
    )


def test_apply_saves_while_holding_the_lock(tmp_path: Path, monkeypatch) -> None:
    """The real path must hold the lock across the save, not just the test helper."""
    import fcntl

    path = _store(tmp_path)
    held: list[bool] = []
    real_save = mint.save_store

    def _spy(store_path, store):
        # A second, non-blocking lock attempt must fail while the save runs.
        with open(store_path.with_name(store_path.name + ".lock"), "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                held.append(True)
            else:
                held.append(False)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return real_save(store_path, store)

    monkeypatch.setattr(mint, "save_store", _spy)
    mint.main(["--store", str(path), "--apply"])
    assert held == [True], "the store was saved without holding the lock"
