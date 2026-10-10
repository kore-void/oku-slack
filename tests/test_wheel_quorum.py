"""P-001 D1: configurable confirmation quorum (default 1, join while ready) and B5 (double_bet consumed only by a
round the player bet in)."""
import json, random
import pytest
from oku_slack.wheel import config, engine, panel, store, slack_adapter as SA

class Clock:
    def __init__(self, t=6_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

def mk(**s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3)), st, clk

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"
def T(x): return json.dumps(x, ensure_ascii=False)

# ---------------- D1: confirmation quorum ----------------
def test_quorum_default_1_one_confirmation_goes_live_and_others_may_join():
    e, st, clk = mk()
    assert e.s["confirm_quorum"] == 1 and e.quorum() == 1
    ev = e.spin("kore", force="porada", lead_s=60)
    assert "stačí 1 z: Kore, ICIK" in e.busy_reason(e.active_event())
    e.confirm_code(ev["id"], "icik", ev["code"]); a = e.active_event()
    assert a["state"] == "ready" and e.quorum_met(a)
    e.confirm_code(ev["id"], "kore", ev["code"])  # joining while ready is allowed
    assert set(e.active_event()["confirmed"]) == {"kore", "icik"}
    bal = e.eco.balance("kore"); e.confirm_code(ev["id"], "kore", ev["code"])  # re-confirm: no-op, no double points
    assert e.eco.balance("kore") == bal
    clk.adv(60); out = [k for k, _ in e.tick()]
    assert "live" in out and e.active_event()["state"] == "live"
    with pytest.raises(engine.WheelError) as x: e.confirm_code(ev["id"], "kore", ev["code"])
    assert x.value.code == "not_confirmable"

def test_quorum_configurable_strict_and_clamped():
    e, st, clk = mk(confirm_quorum=2); ev = e.spin("kore", force="porada", lead_s=60)
    e.confirm_code(ev["id"], "kore", ev["code"]); assert e.active_event()["state"] == "pending"
    assert "čeká se na potvrzení (ICIK)" in e.busy_reason(e.active_event())
    e.confirm_code(ev["id"], "icik", ev["code"]); assert e.active_event()["state"] == "ready"
    for q, want in ((0, 2), (5, 2), ("x", 2), (1, 1)):
        e.s["confirm_quorum"] = q; assert e.quorum() == want
    assert e.snapshot()["settings"]["confirm_quorum"] == 1

def test_quorum_not_met_expires_with_missing_players():
    e, st, clk = mk(); ev = e.spin("kore", force="kantyna", lead_s=10)
    clk.adv(10 + 61); out = dict(e.tick()); assert out["expired"]["id"] == ev["id"]
    assert e.missing(out["expired"]) == ["kore", "icik"] and e.active_event() is None

def test_panel_and_status_with_quorum():
    e, st, clk = mk(); ev = e.spin("kore", force="socky", lead_s=600); e.confirm_code(ev["id"], "kore", ev["code"])
    clk.adv(7); e.tick(); bl, ph = panel.blocks(e, clk()); t = T(bl)
    assert ph == "result" and "kolo_confirm" in t and "✅ <@U0C6XAN3EG3>" in t and "⏳ <@U0C75FSEK2M>" in t
    assert "připraveno" in SA.status_text(e, "kore", e.active_event(), 0)

# ---------------- B5: double_bet only consumed by a round the player bet in ----------------
def test_double_bet_kept_when_player_did_not_bet_then_used():
    e, st, clk = mk(bet_window_s=30); st.kv_set("double:icik", "1")
    e.open_bets("kore"); e.bet("kore", "porada", 50); clk.adv(31); ev = dict(e.tick())["spin"]; clk.adv(7); e.tick()
    assert st.kv_get("double:icik") == "1"                                         # ICIK did not bet: double kept
    clk.t = ev["start_at"] + 61; e.tick(); assert e.active_event() is None         # expired, wheel free
    e.open_bets("icik")
    for x in e.cfg["events"]: e.bet("icik", x["key"], 10)                           # exactly one segment wins
    clk.adv(31); ev2 = dict(e.tick())["spin"]; clk.adv(7); e.tick()
    won = [b for b in e.store.bets() if b["player"] == "icik" and b["state"] == "won"][0]
    assert won["key"] == ev2["key"] and won["payout"] == int(round(10 * won["odds"] * 2)) and not st.kv_get("double:icik")
