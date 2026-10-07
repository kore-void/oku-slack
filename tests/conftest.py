
import pytest as _pytest

@_pytest.fixture(autouse=True)
def _wheel_no_local_secrets(monkeypatch, tmp_path):
    """Wheel tests use committed placeholder room_keys, never the untracked players.local.toml."""
    monkeypatch.setenv("OKU_WHEEL_LOCAL", str(tmp_path / "absent.players.local.toml"))
