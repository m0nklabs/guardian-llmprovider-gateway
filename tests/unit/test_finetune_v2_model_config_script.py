import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "finetune_v2_model_config.py"


def _load_script_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(SCRIPT_PATH.parent))
    spec = importlib.util.spec_from_file_location("finetune_v2_model_config_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("argv", "error_message"),
    [
        (["TestModel", "--context", "0"], "--context must be > 0"),
        (["TestModel", "--ngl", "-1"], "--ngl must be >= 0"),
        (["TestModel", "--ngl-step", "0"], "--ngl-step must be > 0"),
    ],
)
def test_validate_args_rejects_invalid_runtime_ranges(monkeypatch: pytest.MonkeyPatch, argv: list[str], error_message: str):
    module = _load_script_module(monkeypatch)
    args = module.parse_args(argv)
    with pytest.raises(SystemExit, match=error_message):
        module.validate_args(args)
