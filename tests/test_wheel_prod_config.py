"""P-001 production config: allow_force off + hidden room button (D10), confirm_quorum 1 (D1), Čapák follow-up off (D12)."""
import pathlib, random, tomllib
import pytest
from oku_slack.wheel import config, engine, store, slack_adapter as SA

ROOT = pathlib.Path(__file__).resolve().parents[1]

class Clock:
    def __init__(self, t=6_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

def mk(**s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3)), st, clk

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"

# ---------------- production config: D10 allow_force, D12 follow-up ----------------
def test_production_config_force_off_quorum_1_followup_off():
    raw = tomllib.loads((ROOT / "players.toml").read_text(encoding="utf-8"))["settings"]
    assert raw["allow_force"] is False and raw["confirm_quorum"] == 1
    assert tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))["capak_followup"]["enabled"] is False
    html = (ROOT / "oku_slack" / "wheel" / "static" / "room.html").read_text(encoding="utf-8")
    assert 'id="force" title="test: allow_force" style="display:none"' in html and "S.settings.allow_force" in html

def test_force_disabled_when_not_allowed_and_snapshot_flag():
    e, st, clk = mk(allow_force=False)
    assert e.snapshot()["settings"]["allow_force"] is False
    with pytest.raises(engine.WheelError) as x: e.spin("kore", force="snemovna")
    assert x.value.code == "force_disabled"
    assert SA.handle_command(e, KORE, "toc snemovna")["text"] == "✋ Vynucení je vypnuté."
