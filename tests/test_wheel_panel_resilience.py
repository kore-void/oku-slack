"""P-001 B2/B3: panel exponential backoff with cap, Slack response_metadata logging, text-only degrade,
result-card (slack_file) race. Fake Slack client; no network."""
import json, logging, random
from oku_slack.wheel import config, engine, panel, store

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

def mk(**s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3)), st, clk

def T(x): return json.dumps(x, ensure_ascii=False)
def img(call): return [b for b in call[1]["blocks"] if b["type"] == "image"]

# ---------------- B2/B3: panel backoff, Slack metadata logging, text-only degrade, card race ----------------
def test_panel_exponential_backoff_capped_and_logs_slack_messages(caplog):
    e, st, clk = mk(); sl = FakeSlack(); p = panel.Panel(e, sl, "C1", st, clock=clk)
    assert p.flush() and sl.names() == ["post"]
    sl.fail["update"] = Err("invalid_blocks", ["[ERROR] downloading image failed [json-pointer:/blocks/2/image_url]"])
    caplog.set_level(logging.WARNING, logger="oku_wheel.panel")
    for _ in range(4 * 3600): clk.adv(1); p.request(); p.flush()   # 4 h of a broken panel, 1 flush/s
    n = sl.names().count("update")
    assert 20 <= n <= 70, n                                        # was ~14 400 (one per second) before P-001
    assert p.blocked_until - clk() <= p.backoff_cap_s and p.err_streak == n
    logs = caplog.text
    assert "downloading image failed" in logs and "x3 in a row" in logs and "retry in 300s" in logs
    sl.fail.clear(); clk.adv(301); p.request(); assert p.flush() and p.err_streak == 0 and p.last_error is None

def step(p, clk): clk.adv(max(0.0, p.blocked_until - clk()) + 1.01); p.request(); return p.flush()

def test_backoff_resets_on_new_code_and_soft_errors_keep_retry_s():
    e, st, clk = mk(); sl = FakeSlack(); p = panel.Panel(e, sl, "C1", st, clock=clk); p.flush()
    sl.fail["update"] = Err("invalid_blocks")
    for _ in range(3): step(p, clk)
    assert p.err_streak == 3 and round(p.blocked_until - clk()) == 4
    sl.fail["update"] = Err("ratelimited"); clk.adv(5); p.request(); p.flush()
    assert p.err_streak == 1 and round(p.blocked_until - clk()) == 120

def test_text_only_degrade_after_repeated_invalid_blocks():
    e, st, clk = mk(); sl = FakeSlack(); p = panel.Panel(e, sl, "C1", st, clock=clk); p.flush()
    assert img(sl.calls[-1])
    sl.fail["update"] = Err("invalid_blocks")
    for _ in range(2): step(p, clk)
    assert not p.degraded(); step(p, clk); assert p.degraded()
    sl.fail.clear(); clk.adv(10); p.request(); assert p.flush()
    last = sl.calls[-1][1]["blocks"]; assert not [b for b in last if b["type"] == "image"] and "🖼️ Kolo štěstí OKÚ" in T(last)
    clk.adv(601); p.request(); assert p.flush() and img(sl.calls[-1])  # images come back after degrade_s

def test_result_card_static_first_then_slack_file_when_ready():
    e, st, clk = mk(); sl = FakeSlack(); p = panel.Panel(e, sl, "C1", st, clock=clk, card_renderer=lambda ev: b"PNG"); p.flush()
    ev = e.spin("kore", force="porada"); clk.adv(7); p.request(); assert p.flush()
    assert sl.names()[-2:] == ["upload", "update"] and img(sl.calls[-1])[0]["image_url"].endswith(f"card-porada.png?v={ev['id']}")
    clk.adv(1); assert not p.flush()                     # not ready yet, nothing to change
    clk.adv(2.5); assert p.flush() and img(sl.calls[-1])[0]["slack_file"] == {"id": "F0CARD"}
    assert sl.names().count("upload") == 1
    sl.fail["update"] = Err("invalid_blocks"); clk.adv(2); p.request(); p.flush()   # slack_file refused -> static card
    sl.fail.clear(); clk.adv(5); p.request(); assert p.flush() and "image_url" in img(sl.calls[-1])[0]
