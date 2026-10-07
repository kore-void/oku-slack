import json, random
from oku_slack.wheel import config, engine, panel, store, slack_adapter as SA

class Clock:
    def __init__(self, t=4_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

class Err(Exception):
    def __init__(self, code): super().__init__(code); self.response = {"error": code}

class FakeSlack:
    def __init__(self, fail=None): self.calls, self.fail = [], dict(fail or {})
    def _rec(self, n, kw):
        self.calls.append((n, kw))
        if n in self.fail: raise Err(self.fail[n])
    def chat_postMessage(self, **kw): self._rec("post", kw); return {"ts": "100.1", "channel": kw["channel"]}
    def chat_update(self, **kw): self._rec("update", kw); return {"ok": True}
    def chat_postEphemeral(self, **kw): self._rec("ephemeral", kw); return {"ok": True}
    def views_open(self, **kw): self._rec("views_open", kw); return {"ok": True}
    def files_upload_v2(self, **kw): self._rec("upload", kw); return {"file": {"permalink": "https://x"}}
    def names(self): return [n for n, _ in self.calls]

def mk(**kw):
    clk = Clock(); st = store.Store(); e = engine.Engine(config.load(), st, clock=clk, rng=random.Random(9))
    sl = FakeSlack(**kw); return e, st, clk, sl, panel.Panel(e, sl, "C0C6W8E6NP9", st, clock=clk)

def all_text(bl): return json.dumps(bl, ensure_ascii=False)

def test_panel_posted_once_then_updated_and_persisted():
    e, st, clk, sl, p = mk(); assert p.flush() and sl.names() == ["post"]
    assert st.kv_get("panel_ts") == "100.1" and st.kv_get("panel_channel") == "C0C6W8E6NP9"
    p.request(); clk.adv(2); assert p.flush() and sl.names() == ["post", "update"]
    p2 = panel.Panel(e, sl, "C0C6W8E6NP9", st, clock=clk); clk.adv(2); p2.request(); p2.flush()
    assert sl.names()[-1] == "update" and sl.calls[-1][1]["ts"] == "100.1"

def test_throttle_and_spin_gif_then_result():
    e, st, clk, sl, p = mk(); p.flush()
    ev = e.spin("kore", force="tiskovka"); p.request(); clk.adv(2); p.flush()
    img = sl.calls[-1][1]["blocks"][1]["image_url"]; assert img.endswith(f"kolo-spin-tiskovka.gif?v={ev['id']}")
    for _ in range(3): p.request(); clk.adv(0.4); p.flush()
    assert sl.names().count("update") == 1          # throttled (2 s)
    clk.adv(1.5); p.flush()                          # >3.5 s after spin: switches to result by itself
    assert sl.calls[-1][1]["blocks"][1]["image_url"].endswith(f"kolo-tiskovka.png?v={ev['id']}")

def test_blocks_content_and_limits():
    e, st, clk, sl, p = mk(); ev = e.spin("kore", force="porada", lead_s=600)
    e.confirm_code(ev["id"], "kore", ev["code"]); e.use_command("icik"); clk.adv(7); e.tick()
    bl, kind = panel.blocks(e, clk()); t = all_text(bl)
    assert kind == "result" and len(bl) <= 50
    for must in ("Monika Babišová", "Mimořádná porada OKÚ", "čeká na potvrzení", "Kore ✅", "ICIK ⏳", "nabíjí se", "Kalousek za to může", "<!date^"):
        assert must in t, must
    assert ev["code"] not in t
    ids = [x["action_id"] for b in bl if b["type"] == "actions" for x in b["elements"]]
    assert ids == ["kolo_confirm", "kolo_command", "kolo_status"] and len(set(ids)) == len(ids)
    for b in bl:
        if b["type"] == "section": assert len(b["text"]["text"]) <= 3000
        if b["type"] == "header": assert len(b["text"]["text"]) <= 150
        if b["type"] == "image": assert b["image_url"].startswith("https://") and len(b["image_url"]) <= 3000
    bl0, k0 = panel.blocks(engine.Engine(config.load(), store.Store()))
    assert k0 == "idle" and "kolo_spin" in all_text(bl0)

def test_titanic_beat_in_panel():
    e, st, clk, sl, p = mk(); ev = e.spin("kore", force="snemovna", lead_s=0)
    for q in e.players(): e.confirm_code(ev["id"], q, ev["code"])
    clk.adv(5); e.tick(); clk.t = e.active_event()["live_at"] + 112
    bl, kind = panel.blocks(e, clk()); t = all_text(bl)
    assert kind == "poster" and "PŘÍMÝ PŘENOS · Já letím!" in t and "Petr Macinka" in t

def test_actions_ephemeral_modal_and_cooldown():
    e, st, clk, sl, p = mk(); posted = []
    body = lambda a: {"actions": [{"action_id": a}], "user": {"id": "U0C6XAN3EG3"}, "channel": {"id": "C0C6W8E6NP9"}, "trigger_id": "T1"}
    r = SA.handle_action(e, sl, body("kolo_spin"), post=lambda k, o: posted.append(k), pnl=p)
    assert r["spin"] and posted == ["spin"] and sl.names()[-1] == "ephemeral" and r["spin"]["code"] in sl.calls[-1][1]["text"]
    SA.handle_action(e, sl, body("kolo_confirm"), pnl=p); assert sl.names()[-1] == "views_open"
    SA.handle_action(e, sl, body("kolo_command"), post=lambda k, o: posted.append(k), pnl=p); assert posted[-1] == "seq_start"
    SA.handle_action(e, sl, body("kolo_command"), pnl=p); assert "Nabíjí" in sl.calls[-1][1]["text"]
    SA.handle_action(e, sl, body("kolo_status"), pnl=p); assert sl.names()[-1] == "ephemeral"
    nob = body("kolo_spin"); nob["user"]["id"] = "UNOBODY"; SA.handle_action(e, sl, nob, pnl=p)
    assert "Nejsi" in sl.calls[-1][1]["text"]

def test_not_in_channel_logged_and_backoff():
    e, st, clk, sl, p = mk(fail={"post": "not_in_channel"})
    assert not p.flush() and p.last_error == "not_in_channel" and st.kv_get("panel_ts") is None
    clk.adv(10); assert not p.flush() and sl.names() == ["post"]
    sl.fail.clear(); clk.adv(120); assert p.flush() and st.kv_get("panel_ts")
