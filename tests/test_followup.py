import json
from oku_slack import followup as F
from oku_slack import bridge, core

class C:
    def __init__(s): s.posts, s.updates, s.n = [], [], 0
    def chat_postMessage(s, **k): s.n += 1; s.posts.append(k); return {"ts": f"9.{s.n}"}
    def chat_update(s, **k): s.updates.append(k)

A = 1000.0
def mk(tmp, t, c=None):
    clock = {"t": t}
    f = F.Followup(c or C(), {"channel": "CX", "user": "UI", "anchor_ts": str(A)}, tmp / "s.json", now=lambda: clock["t"])
    return f, clock

def test_status_posted_once_and_edited(tmp_path):
    f, k = mk(tmp_path, A + 10); f.tick(); f.tick()
    assert len(f.c.posts) == 1 and not f.c.updates
    k["t"] += 121; f.tick(); assert len(f.c.updates) == 1 and f.c.updates[0]["ts"] == "9.1"
    k["t"] += 121; f.tick(); assert f.c.updates[1]["text"] != f.c.updates[0]["text"]

def test_reminders_and_hard_stop(tmp_path):
    f, k = mk(tmp_path, A + 10); f.tick()
    k["t"] = A + 30 * 60; f.tick(); assert "<@UI> ICIKu" in f.c.posts[-1]["text"]
    k["t"] = A + 45 * 60; assert f.tick() is False
    assert "druhýho" in f.c.posts[-1]["text"] and len(f.c.posts) == 3
    assert f.c.updates[-1]["text"] == F.FINAL and f.state["done"] == "timeout"

def test_reply_cancels(tmp_path):
    f, k = mk(tmp_path, A + 10); f.tick()
    assert not f.on_message({"channel": "CX", "user": "UOTHER"})
    assert f.on_message({"channel": "CX", "user": "UI", "ts": "1"})
    assert f.c.updates[-1]["text"] == F.DONE
    k["t"] = A + 30 * 60; assert f.tick() is False and len(f.c.posts) == 1

def test_restart_no_resend(tmp_path):
    f, k = mk(tmp_path, A + 10); f.tick(); k["t"] = A + 30 * 60; f.tick()
    c2 = C(); g, _ = mk(tmp_path, A + 31 * 60, c2); g.tick()
    assert c2.posts == [] and g.state["status_ts"] == "9.1"

def test_past_reminders_skipped(tmp_path):
    f, k = mk(tmp_path, A + 40 * 60); f.tick()
    assert len(f.c.posts) == 1  # status only; +30 reminder is >grace past

def test_deepcuts_only_capak(tmp_path):
    cfg = core.load_config(); cfg["capak_followup"]["channel"] = "CCAP"
    b = bridge.Bridge(C(), cfg, "UB", "babis", gen=lambda *a: "x")
    assert "Deep cuts" in b.prompt_for("CCAP") and b.prompt_for("COTHER") == b.prompt
