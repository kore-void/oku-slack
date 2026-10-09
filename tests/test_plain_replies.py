"""P0-b + P2: plain human replies (no @mention) in a live meeting / live wheel-skit thread reach the bots; bot messages
never do (loop guard); no second porada in a thread with a running skit or meeting. conftest: tmp outbox."""
import time
from oku_slack import bridge, handoff, meeting as M
from test_meeting import setup, UIDS

CH = "C0C6W8E6NP9"
def human(ts, text="a co dotace?", thread="300.1", user="UKORE"):
    return {"type": "message", "channel": CH, "channel_type": "channel", "user": user, "ts": ts, "thread_ts": thread, "text": text}

def live(thread="300.1", host="alenka", running=True, until=None):
    handoff.write_live_threads({thread: {"channel": CH, "event_id": "E1", "key": "kantyna", "host": host,
                                         "until": until or time.time() + 300, "skit_running": running}})

def meeting_coord():
    c, cl = setup([{"user": UIDS["babis"], "bot_id": "BB", "ts": "300.1", "text": "Porada!"}]); c.start = lambda m, **kw: None
    assert c.start_external({"channel": CH, "thread_ts": "300.1"}) == "started"; return c, cl

def test_plain_reply_in_meeting_thread_goes_to_meeting_and_wakes_it():
    c, _ = meeting_coord(); m = c.active[(CH, "300.1")]
    assert c.route_plain(human("301.0")) == "meeting" and m.wake.is_set()
    assert c.route_plain(human("301.0")) is None                                       # same message twice (two apps)
    assert c.claim(human("301.0")) == "dup"                                            # never also a solo reply

def test_meeting_loop_answers_the_plain_human_reply():
    th = [{"user": UIDS["babis"], "bot_id": "BB", "ts": "300.1", "text": "Porada!"}]; seen = []
    c, _ = setup(th, gen=lambda s, h: seen.append(h[0]["content"]) or "@Marty, čísla?"); c.start = lambda m, **kw: None
    c.start_external({"channel": CH, "thread_ts": "300.1"}); m = c.active[(CH, "300.1")]
    def sl(_):
        if len(th) == 2: th.append(human("305.0", "a kolik stojí hranolky?")); c.route_plain(th[-1])
    m.run(turns=3, sleep=sl)
    assert "Kore (člověk): a kolik stojí hranolky?" in seen[1] and "vstoupil" in seen[1]

def test_plain_reply_in_live_skit_thread_answered_by_host_persona():
    c, _ = setup([]); live(host="alenka")
    assert c.route_plain(human("301.0")) == "alenka"
    assert c.route_plain(human("301.0")) is None
    live(host="nobody"); assert c.route_plain(human("302.0")) == "babis"              # unknown host -> chair

def test_plain_reply_ignored_elsewhere_and_for_bots():
    c, _ = setup([]); live()
    assert c.route_plain(human("301.0", thread="999.9")) is None                       # not a live thread
    assert c.route_plain(dict(human("301.1"), thread_ts=None)) is None                 # top-level: unchanged
    assert c.route_plain(human("301.2", user=UIDS["peta"])) is None                    # persona bot user
    assert c.route_plain(dict(human("301.3"), bot_id="B0C7D597S8N")) is None           # wheel bot / any bot
    assert c.route_plain(dict(human("301.4"), subtype="message_changed")) is None
    assert c.route_plain(human("301.5", f"<@{UIDS['marty']}> ahoj")) is None           # @mention: app_mention path
    assert c.route_plain(dict(human("301.6"), channel="COTHER")) is None               # same ts, other channel
    live(until=time.time() - 1); assert c.route_plain(human("301.7")) is None          # event over

def _handlers(b):
    handlers = {}
    class App:
        def event(self, name):
            def deco(fn): handlers[name] = fn; return fn
            return deco
    bridge.register(App(), b); return handlers

def test_babis_message_handler_routes_plain_reply_to_host_bridge(monkeypatch):
    c, cl = setup([]); live(host="alenka"); handled = []
    monkeypatch.setattr(bridge.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: handled.append((target.__self__.persona, args[0]["ts"]))})())
    h = _handlers(c.bridges["babis"])
    h["message"](human("301.0"))
    h["message"](dict(human("301.1"), bot_id="B0C7D597S8N", user="U0C7D597S8N", text="porada"))   # wheel bot: ignored
    assert handled == [("alenka", "301.0")]

def test_p2_no_second_porada_in_thread_with_running_skit_or_meeting():
    c, _ = setup([]); started = []; c.start = lambda m, **kw: started.append(m)
    live(thread="400.1", running=True)                                                 # scripted skit still posting
    trig = dict(human("401.0", f"porada <@{UIDS['babis']}> <@{UIDS['peta']}>", thread="400.1"))
    assert c.claim(trig) is None and not c.active                                      # solo reply, not a meeting
    live(thread="400.1", running=False)                                                # skit done -> a porada may start
    m = c.claim(dict(trig, ts="402.0")); assert isinstance(m, M.Meeting)
    assert c.claim(dict(trig, ts="403.0")) == "dup"                                    # meeting running: no second one
    assert c.start_external({"channel": CH, "thread_ts": "400.1"}) == "dup"            # wheel cannot add one either
    assert c.start_external({"channel": CH, "thread_ts": "500.1"}) == "dup"            # nor elsewhere in that channel
    assert len(c.active) == 1
