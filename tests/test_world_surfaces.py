"""P-001 world surfaces (no channel posts): /kolo svet (ephemeral), panel overflow + world line, canvas section."""
import json, random
from oku_slack.wheel import canvas, config, engine, panel, store, slack_adapter as SA

class Clock:
    def __init__(self, t=6_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

class Err(Exception):
    def __init__(self, code, msgs=None):
        super().__init__(code); self.response = {"error": code, "response_metadata": {"messages": msgs or []}}

class FakeSlack:
    def __init__(self): self.calls, self.fail = [], {}
    def _rec(self, n, kw):
        self.calls.append((n, kw))
        if n in self.fail: raise self.fail[n]
    def chat_postMessage(self, **kw): self._rec("post", kw); return {"ts": "100.1", "channel": kw["channel"]}
    def chat_update(self, **kw): self._rec("update", kw); return {"ok": True}
    def chat_postEphemeral(self, **kw): self._rec("ephemeral", kw); return {"ok": True}
    def views_open(self, **kw): self._rec("views_open", kw); return {"ok": True}
    def files_upload_v2(self, **kw): self._rec("upload", kw); return {"file": {"id": "F0CARD"}}
    def names(self): return [n for n, _ in self.calls]

def mk(jsonl=None, **s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3), world_jsonl=jsonl), st, clk

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"
def T(x): return json.dumps(x, ensure_ascii=False)

def _history(e, clk):
    ev = e.spin("kore", force="porada", lead_s=10); clk.adv(71); e.tick()          # expired, both missed
    e.use_command("icik")                                                           # steal: Kalousek takes the blame
    ev2 = e.spin("icik", force="kantyna", lead_s=0); e.confirm_code(ev2["id"], "icik", ev2["code"]); clk.adv(7); e.tick()
    clk.t = e.active_event()["end_at"]; e.tick()                                    # done
    return ev, ev2

# ---------------- surfaces: /kolo svet, panel line, canvas ----------------
def test_kolo_svet_ephemeral_summary():
    e, st, clk = mk(); _history(e, clk)
    r = SA.handle_command(e, KORE, "svet"); t = r["text"]
    assert not r.get("panel") and not r["open_modal"] and not r.get("changed")
    for must in ("🌍 *Stav světa OKÚ*", "režim A (vzácné koruny)", "💶 Dotace 5\u00a0000", "📣 Kampaň 35/100", "🍟 Hranolky 80",
                 "👍 Lajky 1\u00a0200", "🫵 Vina:", "Kalousek 1", "Za 7 dní: 2× točeno, 1× živě (50 %)",
                 "Tvoje skutky, které si postavy pamatují: Babiš: propásl(a) jsi *Mimořádná porada OKÚ*"):
        assert must in t, must
    assert "Stav světa OKÚ" in SA.handle_command(e, ICIK, "svět")["text"]
    assert "*/kolo svet*" in SA.HELP

def test_svet_from_panel_overflow_and_panel_world_line():
    e, st, clk = mk(); sl = FakeSlack(); p = panel.Panel(e, sl, "C0C6W8E6NP9", st, clock=clk)
    bl, ph = panel.blocks(e); t = T(bl)
    assert '"value": "svet"' in t and "🌍 Kampaň 35 · Hranolky 80 · Dotace 5\u00a0000 · živých událostí 0/0 · /kolo svet" in t and len(bl) <= 50
    body = {"actions": [{"action_id": "kolo_more", "selected_option": {"value": "svet"}}], "user": {"id": ICIK}, "channel": {"id": "C0C6W8E6NP9"}}
    SA.handle_action(e, sl, body, pnl=p); assert sl.names()[-1] == "ephemeral" and "Stav světa OKÚ" in sl.calls[-1][1]["text"]
    assert [r["payload"] for r in e.wlog.events() if r["type"] == "ui.kolo"][-1] == {"sub": "svet", "via": "button"}

def test_canvas_world_section_and_vetoed_state():
    e, st, clk = mk(); _history(e, clk); md = canvas.markdown(e)
    assert "## 🌍 Stav světa" in md and "**5\u00a0000**" in md and "Vina: " in md and "Alenka otevírá kantýnu proběhla" in md
    assert canvas.STATE_CZ["vetoed"] and "vetoed" in panel.STATE_CZ and "vetoed" in SA.STATE_HUMAN
