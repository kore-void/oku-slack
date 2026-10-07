import os, pytest
from oku_slack import core
from oku_slack.bridge import Bridge, FALLBACK

CFG = core.load_config()

@pytest.mark.parametrize("text,key", [("ahoj", "babis"), ("Alenko, co hranolky?", "alenka"),
    ("Bourák a Marty", "bourak"), ("marty pojď sem", "marty"), ("Peťa Maci řekni něco", "peta"),
    ("kalousek ahoj", "babis")])
def test_route(text, key): assert core.route(CFG, text)[0] == key

def test_channel_default():
    c = dict(CFG, channel_defaults={"C1": "marty"})
    assert core.route(c, "ahoj", "C1")[0] == "marty"
    assert core.route(c, "Alenka", "C1")[0] == "alenka"

def test_blame():
    assert core.route(CFG, "za to může Kalousek")[1]
    assert not core.route(CFG, "Kalousek je fajn")[1]
    assert not core.route(CFG, "za to může Fiala")[1]

def test_prompts_built_and_parody():
    for k, p in CFG["personas"].items():
        s = core.build_prompt(p)
        assert "PARODIE" in s and len(s) > 500, k

class FakeClient:
    def __init__(self): self.posts = []
    def chat_postMessage(self, **kw): self.posts.append(kw)
    def conversations_replies(self, **kw): return {"messages": [{"user": "U1", "text": "<@B> Alenko?"}]}

def test_handle_posts_as_persona_with_kalousek():
    c = FakeClient(); b = Bridge(c, CFG, "B", gen=lambda s, h: "odpověď")
    b.handle({"channel": "C", "ts": "1", "text": "<@B> Babiši, za to může Kalousek?"})
    assert [p["username"] for p in c.posts] == ["Andrej Babiš (parodie)", "Kalousek (parodie)"]
    assert all(p["thread_ts"] == "1" and "icon_url" not in p for p in c.posts)

def test_llm_failure_fallback_and_icon():
    c = FakeClient(); cfg = dict(CFG, icon_base_url="https://x.example/av")
    def boom(s, h): raise RuntimeError
    Bridge(c, cfg, "B", gen=boom).handle({"channel": "C", "ts": "2", "thread_ts": "1", "text": "Alenko"})
    assert c.posts[0]["text"] == FALLBACK and c.posts[0]["icon_url"] == "https://x.example/av/alenka-hranolka.jpg"

def test_llm_request(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    seen = {}
    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}
    def post(url, headers, json, timeout): seen.update(url=url, model=json["model"]); return R()
    assert core.llm("sys", [], post=post) == "ok"
    assert seen == {"url": "https://api.x.ai/v1/chat/completions", "model": "grok-4"}
