"""Bridge side of P-005 autonomous chatter: kind=chatter hand-off requests, a bounded (<= 4 turns) persona exchange
driven by the bridge itself (opener top-level, turns in its thread, each generated with the thread so far), acks
started (thread_ts) + done (turns, llm_calls), satire guardrails, and the loop guard left intact."""
import threading, pytest
from oku_slack import bridge, chatter, handoff, meeting

CH = "C0C76ATGLAH"   # #oku-vina
CFG = {"personas": {k: {"name": n, "aliases": [k]} for k, n in
                    (("babis", "Andrej Babiš"), ("alenka", "Alenka Hranolka"), ("kalousek", "Kalousek"), ("marty", "Marty Prchal"))},
       "channel_defaults": {CH: "kalousek", "C0C7BF296P4": "alenka", "C0C6W8E6NP9": "babis"}}

class Slack:
    """One fake workspace shared by all persona clients: threads keep the real order of posts."""
    def __init__(self): self.posts, self.n = [], 0
    def post(self, user, **kw):
        self.n += 1; ts = f"{1790000000 + self.n}.000100"
        self.posts.append(dict(kw, user=user, bot_id="B" + user, ts=ts)); return {"ts": ts}
    def thread(self, ch, ts):
        return [p for p in self.posts if p["channel"] == ch and (p["ts"] == ts or p.get("thread_ts") == ts)]

class Client:
    def __init__(self, slack, uid): self.slack, self.uid = slack, uid
    def chat_postMessage(self, **kw): return self.slack.post(self.uid, **kw)
    def conversations_replies(self, channel, ts, limit=100): return {"messages": self.slack.thread(channel, ts)}

class FakeBridge:
    def __init__(self, slack, key): self.bot, self.client, self.prompt = f"U{key.upper()}", Client(slack, f"U{key.upper()}"), f"[persona {key}]"

def mk(gen=None, cfg=CFG):
    slack, seen = Slack(), []
    def g(system, hist):
        seen.append((system, hist[-1]["content"])); return gen(system, hist) if gen else f"Odpověď číslo {len(seen)}, a co ty?"
    c = meeting.Coordinator(cfg, gen=g)
    for k in cfg["personas"]: c.add(k, FakeBridge(slack, k))
    return c, slack, seen

def req(**kw):
    r = {"id": "r1", "kind": "chatter", "source": "world:chatter", "channel": CH, "thread_ts": None,
         "personas": ["kalousek", "babis"], "topic": "Kdo může za hranolky?", "opener": "Já za to nemůžu. <@UBABIS> <!channel>", "turns": 4,
         "storylet": "KALOUSEK_VINA", "slot": "2026-10-12 11:30"}
    r.update(kw); return r

def run(c, r):
    res = c.start_chatter(r, sleep=lambda d: None); c.chatter_thread.join(5); return res

def test_exchange_opener_top_level_then_bounded_turns_in_thread_with_context():
    c, slack, seen = mk()
    res = run(c, req())
    assert res[0] == "started" and res[1]["turns"] == 4 and res[1]["personas"] == ["kalousek", "babis"]
    ts = res[1]["thread_ts"]; th = slack.thread(CH, ts)
    assert th[0]["ts"] == ts and "thread_ts" not in th[0] and th[0]["user"] == "UKALOUSEK"        # opener: top-level, persona 1
    assert th[0]["text"] == "Já za to nemůžu." and "<" not in th[0]["text"]                       # mentions/broadcasts stripped
    assert [p["user"] for p in th] == ["UKALOUSEK", "UBABIS", "UKALOUSEK", "UBABIS"] and all(p["thread_ts"] == ts for p in th[1:])
    assert len(seen) == 3 and "Kdo může za hranolky?" in seen[0][1] and "Kalousek: Já za to nemůžu." in seen[0][1]
    assert "Odpověď číslo 1" in seen[1][1] and "Odpověď číslo 2" in seen[2][1]                   # each turn sees the thread so far
    assert chatter.CHATTER_RULES in seen[0][0] and seen[0][0].startswith("[persona babis]") and "bez otázky" in seen[2][1]
    assert c.chatters == {} and not c.busy(CH)
    done = handoff.acks_for("r1")
    assert done[-1]["status"] == "done" and done[-1]["turns"] == 4 and done[-1]["llm_calls"] == 3 and done[-1]["thread_ts"] == ts

def test_three_personas_round_robin_and_turn_cap():
    assert chatter.speaker_order(["a", "b", "c"], 4) == ["a", "b", "c", "a"] and chatter.speaker_order(["a", "b"], 3) == ["a", "b", "a"]
    c, slack, seen = mk()
    res = run(c, req(personas=["alenka", "kalousek", "babis"], turns=9, channel="C0C7BF296P4"))
    assert res[1]["turns"] == 4 and len(slack.posts) == 4                                          # hard cap 4 turns
    c2, slack2, _ = mk(cfg=dict(CFG, world={"chatter_max_turns": 3})); run(c2, req())
    assert len(slack2.posts) == 3

@pytest.mark.parametrize("bad,reason", [
    ({"channel": "C0OTHER"}, "channel_not_allowed"), ({"personas": ["babis"]}, "bad_personas"),
    ({"personas": ["babis", "babis"]}, "bad_personas"), ({"personas": ["babis", "alenka", "marty", "kalousek"]}, "bad_personas"),
    ({"personas": ["babis", "peta"]}, "persona_offline:peta"), ({"opener": "  "}, "bad_opener"),
    ({"thread_ts": "1.1"}, "bad_opener"), ({"turns": 1}, "bad_turns"), ({"turns": "x"}, "bad_turns")])
def test_invalid_requests_are_rejected_without_posting(bad, reason):
    c, slack, _ = mk()
    assert c.start_external(req(**bad)) == ("rejected", {"reason": reason}) and slack.posts == []

def test_world_chatter_channels_config_restricts_channels():
    cfg = dict(CFG, world={"chatter_channels": {"kantyna": "C0C7BF296P4"}})
    assert chatter.allowed_channels(cfg) == {"C0C7BF296P4"} and chatter.allowed_channels(CFG) == set(CFG["channel_defaults"])

def test_refused_while_meeting_or_chatter_runs_or_wheel_skit_live(monkeypatch):
    c, slack, _ = mk()
    c.active[(CH, "1.1")] = object(); assert c.start_external(req()) == "dup"; c.active.clear()
    c.chatters[CH] = object(); assert c.start_external(req()) == "dup"
    assert c.start_external({"channel": CH, "source": "world", "opener": "Porada!"}) == "dup"           # no porada over a chatter
    c.chatters.clear()
    handoff.write_live_threads({"9.9": {"channel": "C0X", "until": 4e9, "skit_running": True}})
    assert c.start_external(req()) == ("rejected", {"reason": "wheel_live"}) and slack.posts == []

def test_guardrails_strip_retry_and_fall_back():
    assert chatter.guard_hit("Moje rodina za to nemůže") == "sensitive:rodina" and chatter.guard_hit("Policie tu byla") == "sensitive:policie"
    assert chatter.guard_hit("Jak řekl premiér: „tohle je naprosto skandální věc“") == "quote"
    assert chatter.guard_hit("Synergie a soudruh Bourák, zisk 300 %") is None and chatter.guard_hit("Hranolky došly, Alenko!") is None
    assert chatter.clean("  „<@U1> @Babiš  chci\n čísla“ ") == "Babiš chci čísla"
    long = "Tohle je první věta. " * 30; assert len(chatter.clean(long)) <= chatter.MAX_CHARS and chatter.clean(long).endswith(".")
    c, slack, seen = mk(gen=lambda s, h: "Za všechno může jeho nemoc.")
    run(c, req(turns=2))
    assert slack.posts[1]["text"] == chatter.FALLBACK["babis"] and len(seen) == 3                  # 3 attempts, then fallback
    a = handoff.acks_for("r1")[-1]; assert a["guarded"] == 3 and a["fallbacks"] == 1 and a["llm_calls"] == 3

def test_human_stop_ends_exchange_early():
    c, slack, _ = mk(); orig = c.bridges["kalousek"].client.conversations_replies
    def replies(channel, ts, limit=100):
        if len(slack.posts) == 2 and not any(p.get("user") == "UKORE" for p in slack.posts):
            slack.posts.append({"channel": channel, "thread_ts": ts, "ts": "1790009999.1", "user": "UKORE", "text": "stop"})
        return orig(channel, ts, limit)
    c.bridges["kalousek"].client.conversations_replies = replies
    run(c, req())
    assert len([p for p in slack.posts if p["user"] != "UKORE"]) == 2 and handoff.acks_for("r1")[-1]["status"] == "stopped"

def test_loop_guard_bots_never_react_to_each_other_through_slack_events():
    c, slack, _ = mk(); res = run(c, req()); ts = res[1]["thread_ts"]
    for p in slack.posts:   # every chatter turn, as the Slack event other persona apps would receive (with a mention)
        ev = {"type": "message", "channel": CH, "ts": p["ts"], "thread_ts": ts, "user": p["user"], "bot_id": p["bot_id"],
              "text": p["text"] + " <@UALENKA> <@UMARTY> porada"}
        for b in c.bridges.values():
            assert bridge.ignored(ev, b.bot) and bridge.route_plain(b, ev) is None
        assert c.claim(ev) is None and c.route_plain(ev) is None
    human = {"channel": CH, "ts": "1790005555.1", "thread_ts": ts, "user": "UKORE", "text": "hezký"}
    assert c.route_plain(human) is None                                     # the chatter thread is not a meeting thread
    assert len(slack.posts) == 4 and c.active == {}

def test_inbox_end_to_end_started_then_done_ack_and_old_bridge_rejects():
    c, slack, _ = mk()
    r = handoff.request_chatter(CH, ["kalousek", "babis"], "Vina", "Já za nic nemůžu.", 3, storylet="KALOUSEK_VINA", slot="s1")
    assert r["kind"] == "chatter" and r["source"] == "world:chatter" and r["thread_ts"] is None
    inbox = handoff.Inbox(lambda q: c.start_chatter(q, sleep=lambda d: None))
    assert inbox.poll() == [(r["id"], "started")]; c.chatter_thread.join(5)
    acks = {a["status"]: a for a in handoff.acks_for(r["id"])}   # order may vary in tests (no pause): the world accepts both
    assert set(acks) == {"started", "done"} and acks["started"]["thread_ts"] == acks["done"]["thread_ts"] == slack.posts[0]["ts"]
    assert acks["done"]["turns"] == 3
    old = {k: v for k, v in r.items() if k != "kind"}                     # what a pre-P-005 bridge sees: no porada starts
    assert meeting.Coordinator(CFG).start_external(old) == "rejected"
