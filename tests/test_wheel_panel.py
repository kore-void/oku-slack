import json, random
from oku_slack.wheel import config, engine, panel, render, store, slack_adapter as SA

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
    def files_upload_v2(self, **kw): self._rec("upload", kw); return {"file": {"id": "F0CARD"}}
    def names(self): return [n for n, _ in self.calls]

def mk(card=None, **kw):
    clk = Clock(); st = store.Store(); e = engine.Engine(config.load(), st, clock=clk, rng=random.Random(9))
    sl = FakeSlack(**kw); return e, st, clk, sl, panel.Panel(e, sl, "C0C6W8E6NP9", st, clock=clk, card_renderer=card)

def T(bl): return json.dumps(bl, ensure_ascii=False)
def img(call): return [b for b in call[1]["blocks"] if b["type"] == "image"][0]

def test_panel_posted_once_then_updated_and_persisted():
    e, st, clk, sl, p = mk(); assert p.flush() and sl.names() == ["post"]
    assert st.kv_get("panel_ts") == "100.1"
    p.request(); clk.adv(1); assert p.flush() and sl.names() == ["post", "update"]
    p2 = panel.Panel(e, sl, "C0C6W8E6NP9", st, clock=clk); clk.adv(1); p2.request(); p2.flush()
    assert sl.calls[-1][1]["ts"] == "100.1"

def test_spin_choreography_drums_gif_almost_card():
    e, st, clk, sl, p = mk(card=lambda ev: b"PNG"); p.flush()
    ev = e.spin("kore", force="tiskovka"); p.request(); clk.adv(0.1); clk.adv(1); p.flush()   # t=0.1..1.1 -> drums
    seen = []
    for _ in range(40):
        clk.adv(0.25); p.flush()
        if sl.names()[-1] == "update": seen.append(img(sl.calls[-1])["title"]["text"])
    seen = list(dict.fromkeys(seen))
    assert seen[:4] == ["🥁 Bubny…", "🎡 Točí se…", "A je to…", "Tisková konference"], seen
    first = [c for c in sl.calls if c[0] == "update"][0]; assert img(first)["title"]["text"] == "🥁 Bubny…"
    gif = [c for c in sl.calls if c[0] == "update" and "image_url" in img(c) and ".gif" in img(c)["image_url"]][0]
    assert img(gif)["image_url"].endswith(f"kolo-spin-tiskovka.gif?v={ev['id']}")
    assert img(sl.calls[-1])["slack_file"] == {"id": "F0CARD"} and sl.names().count("upload") == 1
    ups = [i for i, (n, _) in enumerate(sl.calls) if n == "update"]
    # >= 1 s between updates
    # (clock values are not recorded by the fake; the throttle is asserted in test_throttle)

def test_throttle_1s():
    e, st, clk, sl, p = mk(); p.flush()
    for _ in range(8): p.request(); clk.adv(0.25); p.flush()
    assert sl.names().count("update") == 2

def test_card_fallback_on_invalid_blocks():
    e, st, clk, sl, p = mk(card=lambda ev: b"PNG"); p.flush(); ev = e.spin("kore", force="porada"); clk.adv(7)
    sl.fail["update"] = "invalid_blocks"; p.request(); assert not p.flush()
    sl.fail.clear(); clk.adv(2); assert p.flush() and img(sl.calls[-1])["image_url"].endswith(f"card-porada.png?v={ev['id']}")

def test_blocks_show_layout():
    e, st, clk, sl, p = mk(); ev = e.spin("kore", force="socky", lead_s=600)
    e.confirm_code(ev["id"], "kore", ev["code"]); e.use_command("icik"); clk.adv(7); e.tick()
    bl, ph = panel.blocks(e, clk()); t = T(bl)
    assert ph == "result" and [b["type"] for b in bl][:3] == ["header", "context", "image"]
    assert bl[0]["text"]["text"] == "🎡 OKÚ KOLO ŠTĚSTÍ" and "_" in bl[1]["elements"][0]["text"]
    assert t.count(ev["title"]) == 2  # image title + alt only, never repeated in text
    f = [b for b in bl if b.get("fields")][0]["fields"]
    assert [x["text"].split("\n")[0] for x in f] == ["*Start*", "*Potvrzení*", "*Nabité příkazy*", "*Sekvence*"]
    assert "✅ <@U0C6XAN3EG3>" in f[1]["text"] and "⏳ <@U0C75FSEK2M>" in f[1]["text"] and "{ago}" in f[0]["text"]
    assert sum(1 for b in bl if b["type"] == "divider") == 1 and ev["code"] not in t
    for bad in ("127.0.0.1", "localhost", "podržením", "roomce", "http://"): assert bad not in t
    act = [b for b in bl if b["type"] == "actions"][0]["elements"]
    assert [a.get("action_id") for a in act] == ["kolo_spin", "kolo_confirm", "kolo_command", "kolo_more"]
    assert act[0]["style"] == "primary" and act[1]["style"] == "primary" and act[3]["type"] == "overflow"
    assert len(bl) <= 50

def test_idle_and_spin_button_always_visible():
    e = engine.Engine(config.load(), store.Store())
    bl, ph = panel.blocks(e); assert ph == "idle" and "kolo_spin" in T(bl)
    e.spin("kore", force="porada"); bl, ph = panel.blocks(e); assert ph == "drums" and "kolo_spin" in T(bl) and "kolo_confirm" not in T(bl)

def test_legend_mode_live_broadcast():
    e, st, clk, sl, p = mk(); ev = e.spin("kore", force="snemovna", lead_s=0)
    for q in e.players(): e.confirm_code(ev["id"], q, ev["code"])
    clk.adv(7); e.tick(); clk.t = e.active_event()["live_at"] + 112
    bl, ph = panel.blocks(e, clk()); t = T(bl)
    assert ph == "legend" and "titanic.png" in t and "PŘÍMÝ PŘENOS" in t and "> *Já letím!*" in t and "Petr Macinka" in t

def test_actions_overflow_modal_with_code_and_cooldown():
    e, st, clk, sl, p = mk(); posted = []
    def body(a, **x): return {"actions": [dict(action_id=a, **x)], "user": {"id": "U0C6XAN3EG3"}, "channel": {"id": "C0C6W8E6NP9"}, "trigger_id": "T1"}
    r = SA.handle_action(e, sl, body("kolo_spin"), post=lambda k, o: posted.append(k), pnl=p)
    assert r["spin"] and sl.names()[-1] == "ephemeral"
    SA.handle_action(e, sl, body("kolo_spin"), pnl=p); assert "Kolo je obsazené: čeká se na potvrzení" in sl.calls[-1][1]["text"]  # busy -> ephemeral
    SA.handle_action(e, sl, body("kolo_confirm"), pnl=p); v = sl.calls[-1][1]["view"]
    assert sl.names()[-1] == "views_open" and r["spin"]["code"] in T(v)
    SA.handle_action(e, sl, body("kolo_command"), post=lambda k, o: posted.append(k), pnl=p)
    SA.handle_action(e, sl, body("kolo_command"), pnl=p); assert "Nabíjí" in sl.calls[-1][1]["text"]
    SA.handle_action(e, sl, body("kolo_more", selected_option={"value": "stav"}), pnl=p); assert sl.names()[-1] == "ephemeral"

def test_poster_only_short_alarm_message():
    e, st, clk, sl, p = mk(); post = SA.make_poster(e, sl, "C1", None, p)
    ev = e.spin("kore", force="porada")
    for k in ("spin", "reveal", "live", "done", "expired", "nag", "seq_start", "beat", "confirm", "chat"): post(k, ev)
    assert sl.names() == []
    post("alarm", ev); assert sl.names() == ["post"]
    txt = sl.calls[-1][1]["text"]; assert "<@U0C6XAN3EG3>" in txt and "5 minut" in txt and len(txt) < 300
    for bad in ("127.0.0.1", "podržením", "roomce"): assert bad not in txt

def test_not_in_channel_logged_and_backoff():
    e, st, clk, sl, p = mk(fail={"post": "not_in_channel"})
    assert not p.flush() and p.last_error == "not_in_channel"
    clk.adv(10); assert not p.flush() and sl.names() == ["post"]
    sl.fail.clear(); clk.adv(120); assert p.flush() and st.kv_get("panel_ts")

def test_result_card_png():
    b = render.result_card("Mimořádná porada OKÚ", "Andrej Babiš", "Start 18:05", "#1d4f91", None)
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
