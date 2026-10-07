from oku_slack import core, meeting as M
from oku_slack.bridge import Bridge, dispatch

CFG = core.load_config()
UIDS = {"babis": "U0C75A26Y0K", "alenka": "U0C6WGVD1RV", "bourak": "U0C75ND6QKD",
        "marty": "U0C76LPH8R3", "peta": "U0C7BRJ4VRQ", "kalousek": "U0C7FM4QU7N"}

class Client:
    def __init__(self, thread): self.thread, self.posts = thread, []
    def chat_postMessage(self, **kw):
        self.posts.append(kw); self.thread.append({"user": self.uid, "bot_id": "B" + self.uid, "text": kw["text"], "ts": str(len(self.thread) + 100)})
    def conversations_replies(self, **kw): return {"messages": list(self.thread)}

def setup(thread, gen=lambda s, h: "Dobře, @Alenka, kolik to vydělalo?"):
    c = M.Coordinator(CFG, gen=gen); clients = {}
    for k, u in UIDS.items():
        cl = Client(thread); cl.uid = u; clients[k] = cl
        c.add(k, Bridge(cl, CFG, u, k, gen=gen))
    return c, clients

def test_trigger_detection():
    assert M.is_trigger(f"<@{UIDS['babis']}> <@{UIDS['peta']}> ahoj", UIDS)
    assert M.is_trigger("Svolávám poradu? ne, porada hned", UIDS)
    assert not M.is_trigger(f"<@{UIDS['babis']}> ahoj", UIDS)

def test_next_speaker_addressed_then_round_robin():
    parts = ["babis", "alenka", "marty", "peta"]
    assert M.next_speaker(CFG, parts, "babis", "@Marty, kolik?", 0)[0] == "marty"
    assert M.next_speaker(CFG, parts, "babis", f"<@{UIDS['peta']}> čísla!", 0, UIDS)[0] == "peta"
    assert M.next_speaker(CFG, parts, "babis", "Babiš sám sobě", 0)[0] == "alenka"  # self excluded -> rr
    s1, i = M.next_speaker(CFG, parts, "alenka", "nic", 0); s2, _ = M.next_speaker(CFG, parts, "alenka", "nic", i)
    assert (s1, s2) == ("babis", "marty")
    assert M.next_speaker(CFG, parts, "babis", "Kalousku, vysvětli to", 0)[0] == "kalousek"
    assert M.next_speaker(CFG, parts, "babis", "x", 0, blame=True)[0] == "kalousek"

def test_dedupe_one_meeting_no_solo_replies():
    th = [{"user": "UKORE", "ts": "1.0", "text": f"<@{UIDS['babis']}> <@{UIDS['alenka']}> <@{UIDS['marty']}> co zisk?"}]
    c, cl = setup(th); started = []
    c.start = lambda m, **kw: started.append(m)
    ev = dict(th[0], channel="C")
    res = [dispatch(c.bridges[k], dict(ev)) for k in ("babis", "alenka", "marty")]
    assert res == ["meeting", "dup", "dup"] and len(started) == 1
    assert started[0].participants == ["babis", "alenka", "marty"]
    assert all(not x.posts for x in cl.values())

def test_single_mention_is_solo():
    c, _ = setup([]); assert c.claim({"channel": "C", "ts": "2", "user": "U", "text": f"<@{UIDS['peta']}> ahoj"}) is None

def test_meeting_runs_babis_opens_and_closes():
    th = [{"user": "UKORE", "ts": "1.0", "text": "porada o zisku"}]
    c, cl = setup(th); m = c.claim({"channel": "C", "ts": "1.0", "user": "UKORE", "text": "porada o zisku"})
    sleeps = []
    assert m.run(turns=5, sleep=sleeps.append) == 5
    bots = [x for x in th if x.get("bot_id")]
    assert len(bots) == 5 and bots[0]["user"] == UIDS["babis"] and bots[-1]["user"] == UIDS["babis"]
    assert bots[1]["user"] == UIDS["alenka"]  # addressed by previous
    assert f"<@{UIDS['alenka']}>" in bots[0]["text"]
    assert len(sleeps) == 4 and all(8 <= s <= 15 for s in sleeps)

def test_stop_word_ends_meeting():
    th = [{"user": "UKORE", "ts": "1.0", "text": "porada"}]
    c, cl = setup(th); m = c.claim({"channel": "C", "ts": "1.0", "user": "UKORE", "text": "porada"})
    def sl(_): th.append({"user": "UKORE", "ts": "9.0", "text": "Konec, díky"})
    assert m.run(turns=10, sleep=sl) == 1 and m.stopped
    # mid-meeting human message is deduped (not answered solo)
    assert c.claim({"channel": "C", "ts": "9.5", "thread_ts": "1.0", "user": "UKORE", "text": f"<@{UIDS['peta']}> hej"}) == "dup"
    assert M.is_stop("STOP!") and not M.is_stop("stopka")

def test_interjection_prompted():
    th = [{"user": "UKORE", "ts": "1.0", "text": "porada"}]; seen = []
    gen = lambda s, h: seen.append(h[0]["content"]) or "@Marty, čísla?"
    c, _ = setup(th, gen); m = c.claim({"channel": "C", "ts": "1.0", "user": "UKORE", "text": "porada"})
    def sl(_):
        if len(th) == 2: th.append({"user": "UKORE", "ts": "5.0", "text": "a co dotace?"})
    m.run(turns=3, sleep=sl)
    assert "Kore (člověk): a co dotace?" in seen[1] and "vstoupil" in seen[1]

def test_never_empty_kalousek_cutting(monkeypatch):
    c, _ = setup([], gen=lambda s, h: "…")
    assert c.generate_ok("kalousek", "s", [], sleep=lambda x: None) == M.KALOUSEK_FALLBACK
    assert c.generate_ok("marty", "s", [], sleep=lambda x: None) == M.GENERIC_FALLBACK

def test_gemini_backoff_rounds(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2"); [monkeypatch.delenv(k, raising=False) for k in ("OKU_GEMINI_ENV_FILE", "GEMINI_MODEL", "LLM_MODEL")]
    class R:
        def __init__(s, code, t="ok"): s.status_code, s.t = code, t
        def raise_for_status(s): pass
        def json(s): return {"candidates": [{"content": {"parts": [{"text": s.t}]}}]}
    calls = []; slept = []
    def post(url, headers, json, timeout):
        calls.append(1); return R(429) if len(calls) <= 6 else R(200, "zisk 3 %")
    assert core.gemini("s", [], post=post, sleep=slept.append) == "zisk 3 %" and len(slept) == 1
