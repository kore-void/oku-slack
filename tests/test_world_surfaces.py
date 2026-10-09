"""World surfaces after P-001: the wheel panel and canvas carry no world line/section any more; `/kolo svet` (and the
panel overflow 'Stav světa') render the oku_world snapshot fetched over loopback, with a fallback when it is down."""
import json, random
from oku_slack.wheel import canvas, config, engine, panel, store, slack_adapter as SA
from oku_slack.world import view

class Clock:
    def __init__(self, t=1_800_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

class FakeSlack:
    def __init__(self): self.calls = []
    def chat_postMessage(self, **kw): self.calls.append(("post", kw)); return {"ts": "100.1", "channel": kw["channel"]}
    def chat_update(self, **kw): self.calls.append(("update", kw)); return {"ok": True}
    def chat_postEphemeral(self, **kw): self.calls.append(("ephemeral", kw)); return {"ok": True}
    def views_open(self, **kw): self.calls.append(("views_open", kw)); return {"ok": True}
    def files_upload_v2(self, **kw): self.calls.append(("upload", kw)); return {"file": {"id": "F0CARD"}}
    def names(self): return [n for n, _ in self.calls]

def mk(**s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3)), st, clk

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"
def T(x): return json.dumps(x, ensure_ascii=False)

SNAP = {"regime": "A_scarce", "resources": {"dotace": 5000, "kampan": 35, "hranolky": 80, "lajky": 1200},
        "blame": {"kalousek": 1, "kore": 1}, "metrics_7d": {"spun": 2, "live": 1, "bets": 0, "commands": 1, "human_actions": 4, "bridge_calls": 7},
        "porada": {"started": 1, "dry_run": 2}, "dry_run": True,
        "player": {"recent_acts": [{"ts": 1_800_000_000.0, "persona": "babis", "act": "missed:porada:we_0003"}]}}

def test_kolo_svet_renders_the_world_snapshot(monkeypatch):
    e, st, clk = mk(); asked = []
    monkeypatch.setattr(view, "fetch_world", lambda p=None, **k: asked.append(p) or SNAP)
    r = SA.handle_command(e, KORE, "svet"); t = r["text"]
    assert not r.get("panel") and not r["open_modal"] and asked == ["kore"]
    for must in ("🌍 *Stav světa OKÚ*", "režim A (vzácné koruny)", "💶 Dotace 5\u00a0000", "📣 Kampaň 35/100", "🍟 Hranolky 80",
                 "👍 Lajky 1\u00a0200", "🫵 Vina:", "Kalousek 1", "Za 7 dní: 2× točeno, 1× živě (50 %)", "7 odpovědí postav",
                 "Porady: 1× proběhla, 2× nanečisto", "Tvoje skutky, které si postavy pamatují: Babiš: propásl(a) jsi *Mimořádná porada OKÚ*",
                 "🪙 Tvůj zůstatek: 1\u00a0000 OKÚ korun"):
        assert must in t, must
    assert "*/kolo svet*" in SA.HELP

def test_kolo_svet_when_world_is_down(monkeypatch):
    e, st, clk = mk(); monkeypatch.setattr(view, "fetch_world", lambda p=None, **k: None)
    assert SA.handle_command(e, ICIK, "svět")["text"] == view.DOWN

def test_fetch_world_ignores_proxies_and_fails_closed(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.1:9"); monkeypatch.setenv("OKU_WORLD_URL", "http://127.0.0.1:9")  # nothing listens
    assert view.fetch_world("kore", timeout=0.5) is None

def test_panel_has_no_world_line_but_overflow_points_to_world(monkeypatch):
    e, st, clk = mk(); sl = FakeSlack(); p = panel.Panel(e, sl, "C0C6W8E6NP9", st, clock=clk)
    bl, ph = panel.blocks(e); t = T(bl)
    assert "🌍 Kampaň" not in t and "živých událostí" not in t and '"value": "svet"' in t and len(bl) <= 50
    monkeypatch.setattr(view, "fetch_world", lambda p=None, **k: SNAP)
    body = {"actions": [{"action_id": "kolo_more", "selected_option": {"value": "svet"}}], "user": {"id": ICIK}, "channel": {"id": "C0C6W8E6NP9"}}
    SA.handle_action(e, sl, body, pnl=p); assert sl.names()[-1] == "ephemeral" and "Stav světa OKÚ" in sl.calls[-1][1]["text"]
    assert [r["payload"] for r in e.wlog.events() if r["type"] == "ui.kolo"][-1] == {"sub": "svet", "via": "button"}

def test_canvas_has_no_world_section_and_vetoed_state():
    e, st, clk = mk(); md = canvas.markdown(e)
    assert "Stav světa" not in md and "Vina:" not in md
    assert canvas.STATE_CZ["vetoed"] and "vetoed" in panel.STATE_CZ and "vetoed" in SA.STATE_HUMAN
