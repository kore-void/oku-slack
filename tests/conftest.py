
import pytest as _pytest

@_pytest.fixture(autouse=True)
def _wheel_no_local_secrets(monkeypatch, tmp_path):
    """Wheel tests use committed placeholder room_keys, never the untracked players.local.toml."""
    monkeypatch.setenv("OKU_WHEEL_LOCAL", str(tmp_path / "absent.players.local.toml"))

@_pytest.fixture(autouse=True)
def _wheel_tests_allow_force(monkeypatch):
    """Production players.toml has allow_force = false (D10); tests force segments to be deterministic.
    test_wheel_prod_config.py checks the committed file itself."""
    from oku_slack.wheel import config as _cfg
    orig = _cfg.load
    def load(*a, **k):
        c = orig(*a, **k); c["settings"]["allow_force"] = True; return c
    monkeypatch.setattr(_cfg, "load", load)

@_pytest.fixture(autouse=True)
def _no_real_logs(monkeypatch, tmp_path):
    """Tests never write the real logs/usage.jsonl (usage.usage_log_path honours OKU_USAGE_LOG) nor the real
    wheel->bridge hand-off files under logs/outbox (oku_slack.handoff honours OKU_MEETING_OUTBOX)."""
    monkeypatch.setenv("OKU_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OKU_MEETING_OUTBOX", str(tmp_path / "outbox"))
