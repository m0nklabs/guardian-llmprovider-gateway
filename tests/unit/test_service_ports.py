"""GuardianService listener ports: GUARDIAN_UI_PORT env override (B3).

The dashboard/UI listener used to be hardcoded to 11437, which made a test or
parallel instance fight the production port. Product requirement: installable
without port surgery.
"""

import pytest

from app.main import GuardianService


class TestUIPort:
    def test_default_ui_port_is_11437(self, monkeypatch):
        monkeypatch.delenv("GUARDIAN_UI_PORT", raising=False)
        assert GuardianService().ui_port == 11437

    def test_ui_port_env_override(self, monkeypatch):
        monkeypatch.setenv("GUARDIAN_UI_PORT", "12437")
        assert GuardianService().ui_port == 12437

    def test_ui_port_rejects_garbage(self, monkeypatch):
        monkeypatch.setenv("GUARDIAN_UI_PORT", "not-a-port")
        with pytest.raises(RuntimeError, match="GUARDIAN_UI_PORT"):
            GuardianService()

    def test_ui_port_rejects_out_of_range(self, monkeypatch):
        monkeypatch.setenv("GUARDIAN_UI_PORT", "70000")
        with pytest.raises(RuntimeError, match="GUARDIAN_UI_PORT"):
            GuardianService()
