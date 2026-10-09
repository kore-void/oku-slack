"""Bridge side of the world -> bridge porada hand-off: a world request (source=world) carries no thread; the chair
(Babiš) posts the opener top-level and a REAL meeting.py porada runs in its thread; the ack carries thread_ts.
Wheel requests (with thread_ts) behave exactly as before."""
from oku_slack import handoff, meeting

CH = "C0C6W8E6NP9"

class FakeClient:
    def __init__(self): self.posts = []
    def chat_postMessage(self, **kw): self.posts.append(kw); return {"ts": f"{1790000000 + len(self.posts)}.000100"}
    def conversations_replies(self, **kw): return {"messages": []}

class FakeBridge:
    def __init__(self, key): self.bot, self.client, self.prompt = f"U{key.upper()}", FakeClient(), f"[{key}]"

def coord(monkeypatch):
    cfg = {"personas": {k: {"name": k.title(), "aliases": [k]} for k in ("babis", "alenka", "kalousek")}}
    c = meeting.Coordinator(cfg, gen=lambda s, h: "Chci čísla, @Alenka?")
    for k in cfg["personas"]: c.add(k, FakeBridge(k))
    started = []; monkeypatch.setattr(c, "start", lambda m, **kw: started.append(m)); return c, started

def test_bridge_world_request_posts_opener_then_runs_real_porada(monkeypatch):
    c, started = coord(monkeypatch)
    r = c.start_external({"channel": CH, "thread_ts": None, "source": "world", "topic": "Kampaň", "opener": "Porada! <!channel> Téma: Kampaň <@U1>"})
    assert r[0] == "started" and r[1]["thread_ts"] == "1790000001.000100"
    post = c.bridges["babis"].client.posts[0]; assert post == {"channel": CH, "text": "Porada!  Téma: Kampaň"}
    m = started[0]; assert (m.ch, m.ts, m.source, m.topic) == (CH, "1790000001.000100", "world", "Kampaň") and "kalousek" not in m.participants
    assert c.start_external({"channel": CH, "source": "world", "opener": "x"}) == "dup"                    # one porada per channel
    assert c.start_external({"channel": "C2", "source": "world", "opener": "  "}) == "rejected"            # no opener, no thread
    assert c.start_external({"channel": "C2", "thread_ts": None}) == "rejected"                            # old-style without thread

def test_world_porada_first_turn_prompt(monkeypatch):
    c, _ = coord(monkeypatch); seen = []
    monkeypatch.setattr(c, "say", lambda spk, ch, ts, msgs, extra: seen.append((spk, extra)) or "x")
    m = meeting.Meeting(c, CH, "1.1", ["babis", "alenka"], source="world", topic="Kampaň")
    m.run(turns=2, sleep=lambda d: None)
    assert seen[0][0] == "babis" and "pravidelná ranní porada OKÚ, téma: Kampaň" in seen[0][1][0]

def test_inbox_ack_carries_thread_ts_and_old_string_status_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("OKU_MEETING_OUTBOX", str(tmp_path))
    req = handoff.request_world_meeting(CH, "Kampaň", "Porada!", slot="2026-10-12 10:00")
    inbox = handoff.Inbox(lambda r: ("started", {"thread_ts": "9.9"}))
    assert inbox.poll() == [(req["id"], "started")] and handoff.ack_for(req["id"])["thread_ts"] == "9.9"
    r2 = handoff.request_meeting(CH, "1.1"); assert handoff.Inbox(lambda r: "dup").poll() == [(r2["id"], "dup")]
    assert handoff.ack_for(r2["id"]) ["status"] == "dup" and handoff.ack_for(req["id"], tmp_path)["status"] == "started"

