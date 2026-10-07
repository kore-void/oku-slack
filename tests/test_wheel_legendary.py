import random, collections, pytest
from oku_slack.wheel import config, engine, render, store, slack_adapter as SA

class Clock:
    def __init__(self, t=2_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

def mk(seed=3, **settings):
    cfg = config.load(); cfg["settings"].update(settings); clk = Clock()
    return engine.Engine(cfg, store.Store(), clock=clk, rng=random.Random(seed)), clk

def go_live(e, clk, force="snemovna"):
    ev = e.spin("kore", lead_s=0, force=force)
    for p in e.players(): e.confirm_code(ev["id"], p, ev["code"])
    clk.adv(0.01); kinds = [k for k, _ in e.tick()]
    assert "live" in kinds
    return ev

def test_legendary_is_rare_by_weight():
    e, _ = mk(seed=11)
    n = 20000; c = collections.Counter(e.cfg["events"][e.pick()]["key"] for _ in range(n))
    tot = sum(x["weight"] for x in e.cfg["events"]); exp = 0.15 / tot
    assert c["snemovna"] / n == pytest.approx(exp, abs=0.01) and c["snemovna"] < c["porada"] / 5

def test_weight_tunable_zero_never():
    e, _ = mk()
    next(x for x in e.cfg["events"] if x["key"] == "snemovna")["weight"] = 0
    assert all(e.cfg["events"][e.pick()]["key"] != "snemovna" for _ in range(3000))

def test_forced_trigger_and_guard():
    e, _ = mk(); ev = e.spin("kore", force="snemovna")
    assert ev["legendary"] and ev["script"] == "titanic" and ev["forced"] and ev["host_say"]["kind"] == "legendary"
    e2, _ = mk(allow_force=False)
    with pytest.raises(engine.WheelError) as x: e2.spin("kore", force="snemovna")
    assert x.value.code == "force_disabled"
    with pytest.raises(engine.WheelError): mk()[0].spin("kore", force="nope")

def test_script_shape_and_timing():
    sc = config.load()["scripts"]["titanic"]; beats = sc["beats"]
    assert 8 <= len(beats) <= 12
    ats = [b["at"] for b in beats]; assert ats == sorted(ats) and ats[0] == 0 and ats[-1] < sc["duration_s"]
    for b in beats: assert b["visual"] and b["music"] and b["caption"] and b["direction"] and b["lines"]
    speakers = {sp for b in beats for sp, _ in b["lines"]}
    assert {"monika", "macinka", "turek", "marty"} <= speakers <= set(sc["cast"])
    text = " ".join(t for b in beats for _, t in b["lines"])
    for must in ("Věříš mi", "letím", "Nakresli mě", "Ledovec", "víko", "Nikdy tě nepustím"): assert must in text
    assert sc["credits"] == "Produkce: Marty Prchal, marketingový génius" and beats[-1]["visual"] == "credits"
    assert config.load()["events"][-1]["duration_s"] == sc["duration_s"]

def test_cinematic_beats_follow_server_clock():
    e, clk = mk(); ev = go_live(e, clk); sc = e.cfg["scripts"]["titanic"]
    assert e.cinematic(e.active_event())["beat"] == 0
    seen = [0]
    for b in sc["beats"][1:]:
        clk.t = e.active_event()["live_at"] + b["at"] - 0.5; e.tick()
        assert e.cinematic(e.active_event())["beat"] == seen[-1]
        clk.t += 0.5; out = e.tick(); i = sc["beats"].index(b)
        assert ("beat" in [k for k, _ in out]) and e.active_event()["beat"] == i; seen.append(i)
    snap = e.snapshot()["event"]
    assert snap["cinematic"]["active"] and snap["cinematic"]["data"]["visual"] == "credits" and snap["script_data"]["beats"]
    clk.t = e.active_event()["live_at"] + sc["duration_s"]; assert "done" in [k for k, _ in e.tick()]

def test_cinematic_inactive_for_normal_or_pending():
    e, clk = mk(); ev = e.spin("kore", force="porada")
    assert e.cinematic(ev)["active"] is False and "script_data" not in e.snapshot()["event"]
    e2, _ = mk(); ev2 = e2.spin("kore", force="snemovna"); assert e2.cinematic(ev2)["active"] is False

def test_host_lines_all_kinds():
    e, clk = mk(); host = e.cfg["host"]; assert host["name"] == "Monika Babišová"
    for k in ("spin", "result", "alarm", "nag", "expiry", "legendary"): assert len(host["lines"][k]) >= 2
    ev = e.spin("kore", force="porada"); assert ev["host_say"]["text"] in [l.format(who="Kore", title="", time="", missing="") for l in host["lines"]["spin"]]
    e.confirm_code(ev["id"], "kore", ev["code"])
    clk.adv(6.1); out = dict(e.tick()); assert "reveal" in out and out["reveal"]["host_say"]["kind"] == "result"
    assert ev["title"] in out["reveal"]["host_say"]["text"] or "{title}" not in " ".join(host["lines"]["result"])
    clk.t = ev["start_at"] - 300; assert dict(e.tick())["alarm"]["host_say"]["kind"] == "alarm"
    clk.t = ev["start_at"] - 120; n = dict(e.tick())["nag"]
    assert n["host_say"]["kind"] == "nag" and "ICIK" in n["host_say"]["text"]
    assert "nag" not in dict(e.tick())  # once
    clk.t = ev["start_at"] + 1800; x = dict(e.tick())["expired"]; assert x["host_say"]["kind"] == "expiry"

def test_no_nag_when_all_confirmed():
    e, clk = mk(); ev = e.spin("kore", force="porada")
    for p in e.players(): e.confirm_code(ev["id"], p, ev["code"])
    clk.t = ev["start_at"] - 100; assert "nag" not in dict(e.tick())

def test_slack_host_messages_and_force():
    e, clk = mk(); r = SA.handle_command(e, "U0C6XAN3EG3", "toc snemovna"); ev = r["spin"]
    assert ev["key"] == "snemovna"
    t = SA.notification(e, "spin", ev); assert "Monika Babišová" in t and "LEGENDÁRNÍ" in t and ev["code"] not in t
    clk.adv(7); rv = dict(e.tick())["reveal"]; assert "Monika" in SA.notification(e, "reveal", rv)
    for p in e.players(): e.confirm_code(ev["id"], p, ev["code"])
    clk.t = ev["start_at"]; live = dict(e.tick())["live"]; assert "PŘÍMÝ PŘENOS" in SA.notification(e, "live", live)

def test_poster_png():
    b = render.titanic_poster(config.load()["scripts"]["titanic"]); assert b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) > 20000
