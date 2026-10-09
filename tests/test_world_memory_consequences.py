"""P-003 persona memory briefs (<= 600 chars, injected into chatter/porada hand-offs, GET /api/world/brief, bridge
replies failure-isolated) and P-006 rule-based consequences (consequences.toml -> consequence.applied rows with
causal parents; the projection replays them; Kampaň no longer starts in crisis)."""
import json, pytest
from oku_slack import bridge, handoff, meeting
from oku_slack.world import chatter, consequences, memory, state as wstate, view
from test_world_porada import mk, P, requests
from test_world_service import serve, call

MON15 = P(2026, 10, 12, 15)

def ev(seq, t, ts=MON15, actor=None, subject=None, **pl):
    return {"seq": seq, "id": f"we_{seq:04d}", "type": t, "ts": ts, "actor": actor, "subject": subject, "payload": pl, "source": "x"}

def test_rules_file_loads_and_fixes_kampan_start():
    c = consequences.load()
    assert c["seeds"]["kampan"] == 60 and c["thresholds"]["kampan_low"] == 40 and c["include_dry_run"] is True
    ids = {r["id"] for r in c["rules"]}
    assert {"wheel_expired_kampan", "podnet_reacted_lajky", "porada_held_dotace", "chatter_blame_kalousek"} <= ids
    assert wstate.initial()["resources"]["kampan"] == 60
    _, have = chatter.facts({"resources": wstate.initial()["resources"]}); assert "kampan_low" not in have
    _, have = chatter.facts({"resources": {"kampan": 39, "hranolky": 59}}); assert {"kampan_low", "hranolky_low"} <= have

def test_bad_rules_rejected(tmp_path):
    for body in ('[[rule]]\nid="a"\non=["x.y"]\n', '[[rule]]\nid="a"\non=["x.y"]\nresource="gold"\ndelta=1\n',
                 '[[rule]]\nid="a"\non=["x.y"]\nresource="kampan"\ndelta=1\n[[rule]]\nid="a"\non=["x.y"]\nresource="kampan"\ndelta=1\n'):
        f = tmp_path / "c.toml"; f.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError): consequences.load(f)

def test_derive_effects_and_selectors():
    c = consequences.load()
    out = consequences.derive(ev(5, "wheel.expired", key="porada", host="babis", missing=["kore", "icik"]), c)
    assert [o["payload"]["rule"] for o in out] == ["wheel_expired_kampan"]
    o = out[0]; assert o["causal_parents"] == ["we_0005"] and o["dedupe_key"] == "consequence:wheel_expired_kampan:we_0005"
    assert o["payload"]["effects"] == [{"resource": "kampan", "delta": -5}, {"relation": ["babis", "kore"], "delta": -1},
                                       {"relation": ["babis", "icik"], "delta": -1}]
    ch = consequences.derive(ev(6, "chatter.dry_run", storylet="KALOUSEK_VINA", participants=["kalousek", "babis", "alenka"]), c)
    rules = {o["payload"]["rule"]: o["payload"] for o in ch}
    assert set(rules) == {"chatter_rapport", "chatter_blame_kalousek"} and rules["chatter_rapport"]["dry_run"] is True
    assert len(rules["chatter_rapport"]["effects"]) == 6 and {"blame": "kalousek", "n": 1} in rules["chatter_blame_kalousek"]["effects"]
    assert {"relation": ["kalousek", "babis"], "delta": -2} in rules["chatter_blame_kalousek"]["effects"]
    assert consequences.derive(ev(7, "chatter.dry_run", storylet="X", participants=["a"]), dict(c, include_dry_run=False)) == []
    pod = consequences.derive(ev(8, "podnet.dry_run", kind="x_video", participants=["marty"]), c)
    assert {o["payload"]["rule"] for o in pod} == {"podnet_reacted_lajky", "podnet_reacted_kampan"}

def test_engine_writes_rows_once_and_projection_replays(tmp_path):
    svc, clk = mk(tmp_path, {"porada_enabled": True, "chatter_enabled": False}); clk.t = P(2026, 10, 12, 10, 1)
    d = svc.diary
    d.ingest({"type": "wheel.expired", "source": "wheel", "actor": None, "subject": "wheel:event:e1", "ts": clk.t - 50,
              "payload": {"key": "porada", "host": "babis", "missing": ["kore"]}, "dedupe_key": "wheel:e1"})
    before = dict(svc.world.state()["resources"])
    svc.tick(); svc.tick()
    applied = d.events(types=["consequence.applied"])
    assert sorted(a["payload"]["rule"] for a in applied) == ["porada_catering_hranolky", "porada_held_dotace", "wheel_expired_kampan"]
    assert all(a["source"] == "consequence" and a["causal_parents"] for a in applied)
    s = svc.world.state()
    assert s["resources"]["kampan"] == before["kampan"] - 5 and s["resources"]["dotace"] == before["dotace"] + 250
    assert s["resources"]["hranolky"] == before["hranolky"] - 10 and s["relations"]["babis"]["kore"] == -1
    assert s["consequences"]["applied"] == 3 and svc.health()["consequences"]["applied"] == 3
    assert json.loads(json.dumps(s)) == json.loads(json.dumps(svc.world.rebuild())) == json.loads(json.dumps(wstate.project(d.events(), svc.ctx)))
    svc.diary.kv_set("consequence:seq", 0); svc.tick(); assert len(d.events(types=["consequence.applied"])) == 3   # dedupe: idempotent

def test_brief_compact_and_meaningful():
    evs = [ev(1, "porada.dry_run", actor="babis", topic="Kampaň stojí na 30/100.", chair="babis"),
           ev(2, "chatter.dry_run", actor="kalousek", storylet="KALOUSEK_VINA", participants=["kalousek", "babis"], channel_name="vina", topic="Kdo může za to?"),
           ev(3, "consequence.applied", rule="r", effects=[{"blame": "kalousek", "n": 2}, {"relation": ["kalousek", "babis"], "delta": -2},
                                                           {"relation": ["babis", "kore"], "delta": -1}]),
           ev(4, "podnet.dry_run", actor="babis", kind="x_video", participants=["babis"], channel_name="dotace")]
    for i in range(40): evs.append(ev(10 + i, "bridge.reply", actor="babis", kind="solo"))
    st = wstate.project(evs)
    b = memory.brief(st, "babis", {"kore": "Kore"})
    assert len(b) <= 600 and b.startswith("Nedávno: ") and "sdílený odkaz (x_video)" in b and "debata v #vina s Kalousek" in b
    assert "Hráči: Kore −1" in b and "Křivdu na tebe má: Kalousek −2" in b and "Odpovědí na Slacku celkem: 40" in b
    assert "bridge.reply" not in json.dumps(st["memory"]) and memory.brief(st, "nobody") == ""
    k = memory.brief(st, "kalousek"); assert "Vina: ty 2" in k and "Vztahy: Babiš −2" in k
    long = wstate.project([ev(i, "chatter.dry_run", actor="marty", storylet="S", participants=["marty", "peta"], topic=f"téma {i} " + "x" * 70) for i in range(1, 30)])
    assert len(memory.brief(long, "marty")) <= 600

def test_brief_endpoint_and_handoff_briefs(tmp_path):
    svc, clk = mk(tmp_path, {"porada_enabled": False, "chatter_enabled": True, "dry_run": False}); clk.t = P(2026, 10, 12, 11, 31)
    svc.diary.ingest({"type": "porada.dry_run", "source": "schedule", "actor": "babis", "payload": {"topic": "Hranolky", "chair": "babis",
                      "participants": ["babis", "alenka", "bourak", "marty", "peta", "kalousek"]}, "ts": clk.t - 60})
    srv = serve(svc)
    try:
        st, j = call(srv, "GET", "/api/world/brief?persona=marty"); assert st == 200 and j["persona"] == "marty" and 0 < j["chars"] <= 600
        assert call(srv, "GET", "/api/world/brief?persona=hacker")[0] == 404
        st, j = call(srv, "GET", "/api/world/brief"); assert st == 200 and set(j["briefs"]) <= set(wstate.PERSONAS)
        assert call(srv, "GET", "/api/world/brief?persona=babis", headers={"X-Forwarded-For": "1.2.3.4"})[0] == 403
    finally: srv.shutdown(); srv.server_close()
    svc.tick(); req = [r for r in requests(svc) if r.get("kind") == "chatter"][-1]
    assert set(req["briefs"]) == set(req["personas"]) and all(len(v) <= 600 for v in req["briefs"].values())
    r = handoff.request_world_meeting("C", "t", "o", outbox_dir=svc.outbox_dir, briefs={"babis": "x" * 900, "alenka": ""})
    assert r["briefs"] == {"babis": "x" * 600}

def test_meeting_turn_prompt_carries_brief():
    from test_handoff_world import FakeBridge
    cfg = {"personas": {k: {"name": k.title(), "aliases": [k]} for k in ("babis", "alenka")}}
    seen = []; c = meeting.Coordinator(cfg, gen=lambda s, h: seen.append(s) or "Chci čísla.")
    for k in cfg["personas"]: c.add(k, FakeBridge(k))
    m = c.active[("C", "1.1")] = meeting.Meeting(c, "C", "1.1", ["babis", "alenka"], source="world", topic="t", briefs={"babis": "Nedávno: porada."})
    c.say("babis", "C", "1.1", [], []); c.say("alenka", "C", "1.1", [], [])
    assert "Nedávno: porada." in seen[0] and "PAMĚŤ" not in seen[1]

def test_bridge_reply_uses_brief_and_survives_world_down(monkeypatch):
    from test_oku import FakeClient, CFG
    seen = []
    b = bridge.Bridge(FakeClient(), CFG, "K", "kalousek", gen=lambda s, h: seen.append(s) or "ok")
    b.handle({"channel": "C", "ts": "3", "text": "<@K> ahoj"})                                   # world down (conftest port 9)
    monkeypatch.setattr(view, "fetch_brief", lambda p, **k: "Vina: ty 3.")
    b.handle({"channel": "C", "ts": "4", "text": "<@K> ahoj"})
    def boom(p, **k): raise RuntimeError("x")
    monkeypatch.setattr(view, "fetch_brief", boom); b.handle({"channel": "C", "ts": "5", "text": "<@K> ahoj"})
    assert seen[0] == seen[2] == b.prompt and seen[1].endswith("Vina: ty 3.") and "PAMĚŤ" in seen[1]
    b.cfg = dict(CFG, world={"brief_in_replies": False}); monkeypatch.setattr(view, "fetch_brief", lambda p, **k: "Vina: ty 3.")
    b.handle({"channel": "C", "ts": "6", "text": "<@K> ahoj"}); assert seen[3] == b.prompt

def test_fetch_brief_down_is_fast_and_empty():
    import time
    t0 = time.monotonic(); assert view.fetch_brief("babis", url="http://127.0.0.1:9") == "" and time.monotonic() - t0 < 2
