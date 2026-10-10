"""P0-a bridge side: wheel -> bridge hand-off over logs/outbox (conftest points OKU_MEETING_OUTBOX at tmp).
A wheel porada becomes a REAL meeting.Meeting in the skit thread; no double porada per channel/thread."""
import json, threading, time
from oku_slack import core, handoff, meeting as M
from oku_slack.bridge import start_all
from test_meeting import setup, UIDS, CFG

CH = "C0C6W8E6NP9"

def test_outbox_is_tmp_in_tests(tmp_path):
    assert str(handoff.outbox()).startswith(str(tmp_path)) and "logs" not in str(handoff.outbox()).replace(str(tmp_path), "")

def test_request_poll_ack_once_and_survives_restart():
    got = []; inbox = handoff.Inbox(lambda r: got.append(r) or "started")
    r = handoff.request_meeting(CH, "300.1", event_id="E1", topic="Mimořádná porada")
    assert handoff.ack_for(r["id"]) is None
    assert inbox.poll() == [(r["id"], "started")] and got[0]["thread_ts"] == "300.1" and got[0]["topic"] == "Mimořádná porada"
    assert handoff.ack_for(r["id"])["status"] == "started"
    assert inbox.poll() == [] and handoff.Inbox(lambda r: got.append(r) or "started").poll() == []   # acked: never twice
    assert len(got) == 1

def test_stale_request_never_starts_and_half_line_waits():
    got = []; clk = [1000.0]
    r = handoff.request_meeting(CH, "300.1", clock=lambda: 900.0)                 # 100 s old (bridge was down)
    inbox = handoff.Inbox(lambda r: got.append(r) or "started", clock=lambda: clk[0])
    assert inbox.poll() == [(r["id"], "stale")] and got == [] and handoff.ack_for(r["id"])["status"] == "stale"
    with open(handoff.outbox() / handoff.REQUESTS, "a", encoding="utf-8") as f: f.write('{"id": "half", "at": 1000.0, "chan')
    assert inbox.poll() == []
    with open(handoff.outbox() / handoff.REQUESTS, "a", encoding="utf-8") as f: f.write('nel": "C", "thread_ts": "1.1"}\n')
    assert inbox.poll() == [("half", "started")]

def test_handler_error_is_acked_not_retried():
    def boom(r): raise RuntimeError("x")
    r = handoff.request_meeting(CH, "1.1"); inbox = handoff.Inbox(boom)
    assert inbox.poll() == [(r["id"], "error")] and inbox.poll() == []

def test_live_threads_roundtrip_and_expiry():
    now = time.time()
    handoff.write_live_threads({"1.1": {"channel": CH, "until": now + 60, "skit_running": True},
                                "2.2": {"channel": CH, "until": now - 1}})
    lt = handoff.read_live_threads(); assert list(lt) == ["1.1"] and lt["1.1"]["skit_running"]
    assert handoff.read_live_threads(clock=lambda: now + 120) == {}

def test_start_external_real_meeting_and_no_double_porada():
    th = [{"user": UIDS["babis"], "bot_id": "BB", "ts": "300.1", "text": "Porada! Hned! Chci vidět čísla!"}]
    c, cl = setup(th); started = []
    c.start = lambda m, **kw: started.append(m)
    assert c.start_external({"channel": CH, "thread_ts": "300.1", "event_id": "E1", "topic": "Mimořádná porada OKÚ"}) == "started"
    m = started[0]; assert (m.ch, m.ts, m.source) == (CH, "300.1", "wheel") and "kalousek" not in m.participants
    assert c.start_external({"channel": CH, "thread_ts": "300.1"}) == "dup"                 # same thread
    assert c.start_external({"channel": CH, "thread_ts": "999.9"}) == "dup"                 # same channel, other thread
    assert c.start_external({"channel": "COTHER", "thread_ts": "300.1"}) == "started"       # other channel is fine
    assert c.start_external({"channel": CH}) == "rejected" and len(started) == 2
    c.active.pop((CH, "300.1")); assert c.start_external({"channel": CH, "thread_ts": "300.1"}) == "started"  # after it ended

def test_human_trigger_in_thread_with_wheel_meeting_is_dup_and_wakes():
    th = [{"user": UIDS["babis"], "bot_id": "BB", "ts": "300.1", "text": "Porada!"}]
    c, _ = setup(th); c.start = lambda m, **kw: None
    c.start_external({"channel": CH, "thread_ts": "300.1"}); m = c.active[(CH, "300.1")]
    assert not m.wake.is_set()
    assert c.claim({"channel": CH, "ts": "301.0", "thread_ts": "300.1", "user": "UKORE", "text": f"porada <@{UIDS['babis']}> <@{UIDS['peta']}>"}) == "dup"
    assert m.wake.is_set() and len(c.active) == 1

def test_wheel_meeting_runs_in_skit_thread_without_repeating_the_opener():
    th = [{"user": UIDS["babis"], "bot_id": "BB", "ts": "300.1", "text": "Porada! Hned! Chci vidět čísla!"}]; seen = []
    gen = lambda s, h: seen.append(h[0]["content"]) or "Dobře, @Alenka, kolik to vydělalo?"
    c, cl = setup(th, gen); c.start = lambda m, **kw: None
    c.start_external({"channel": CH, "thread_ts": "300.1", "topic": "Mimořádná porada OKÚ"}); m = c.active[(CH, "300.1")]
    assert m.run(turns=3, sleep=lambda s: None) == 3
    posts = [p for k in cl for p in cl[k].posts]
    assert len(posts) == 3 and all(p["thread_ts"] == "300.1" and p["channel"] == CH for p in posts)
    assert "Neopakuj úvod" in seen[0] and "Mimořádná porada OKÚ" in seen[0] and "Andrej Babiš: Porada! Hned!" in seen[0]

def test_pause_is_cut_short_by_a_human_reply():
    c, _ = setup([]); m = M.Meeting(c, CH, "1.1", ["babis"])
    t0 = time.monotonic(); threading.Timer(0.05, m.wake.set).start(); m.pause(5, grace=0.01)
    assert time.monotonic() - t0 < 1 and not m.wake.is_set()

def test_start_all_runs_meeting_inbox(monkeypatch):
    class App:
        def __init__(self, token):
            class Cl:
                def auth_test(s): return {"user_id": "U_" + token}
            self.client = Cl()
        def event(self, name): return lambda f: f
    class H:
        def __init__(self, app, tok): pass
        def connect(self): pass
    ran = []; monkeypatch.setattr(handoff.Inbox, "run", lambda self, period=1.0: ran.append(self))
    started = start_all(CFG, {"SLACK_OKU_BOT_TOKEN": "b0", "SLACK_OKU_APP_TOKEN": "a0"}, App, H)
    inbox = started["babis"].coord.inbox
    assert ran == [inbox] and inbox.on_request == started["babis"].coord.start_external
