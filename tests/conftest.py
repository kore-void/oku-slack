
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
    """Tests never write the real logs/usage.jsonl (usage.usage_log_path honours OKU_USAGE_LOG), the real
    wheel->bridge hand-off files under logs/outbox (oku_slack.handoff honours OKU_MEETING_OUTBOX) nor the world diary."""
    monkeypatch.setenv("OKU_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OKU_MEETING_OUTBOX", str(tmp_path / "outbox"))
    # oku_world: never the real diary/source logs, never a running world service on :8798
    monkeypatch.setenv("OKU_WORLD_LOGS", str(tmp_path / "world-logs"))
    monkeypatch.setenv("OKU_WORLD_SOURCE_LOGS", str(tmp_path / "world-src"))
    monkeypatch.setenv("OKU_WORLD_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("OKU_WORLD_DRY_RUN", raising=False); monkeypatch.delenv("OKU_WORLD_KILL", raising=False)
    monkeypatch.delenv("OKU_WORLD_INBOX", raising=False); monkeypatch.delenv("OKU_WORLD_CONSEQUENCES", raising=False)
    # podnet pollers: never a real X token (env or Windows registry) and never the real pplx CLI
    from oku_slack.world import pplx as _pplx, xsource as _x, view as _view
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(_x, "bearer_token", lambda env=None: (env or {}).get("X_BEARER_TOKEN"))
    def _no_cli(*a, **k): raise AssertionError("tests must not run the real pplx CLI")
    monkeypatch.setattr(_pplx, "run_cli", _no_cli)
    def _no_http(*a, **k): raise AssertionError("tests must not call the real X API")
    monkeypatch.setattr(_x, "http_get", _no_http)
    getattr(_view, "_BRIEFS", {}).clear()
