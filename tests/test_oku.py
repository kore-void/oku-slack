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
    c = FakeClient(); b = Bridge(c, dict(CFG, icon_base_url=""), "B", gen=lambda s, h: "odpověď")
    b.handle({"channel": "C", "ts": "1", "text": "<@B> Babiši, za to může Kalousek?"})
    assert [p["username"] for p in c.posts] == ["Andrej Babiš (parodie)", "Kalousek (parodie)"]
    assert all(p["thread_ts"] == "1" and "icon_url" not in p for p in c.posts)

def test_llm_failure_fallback_and_icon():
    c = FakeClient(); cfg = dict(CFG, icon_base_url="https://x.example/av")
    def boom(s, h): raise RuntimeError
    Bridge(c, cfg, "B", gen=boom).handle({"channel": "C", "ts": "2", "thread_ts": "1", "text": "Alenko"})
    assert c.posts[0]["text"] == FALLBACK and c.posts[0]["icon_url"] == "https://x.example/av/alenka.png"

def test_llm_request(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    seen = {}
    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}
    def post(url, headers, json, timeout): seen.update(url=url, model=json["model"]); return R()
    assert core.llm("sys", [], post=post) == "ok"
    assert seen == {"url": "https://api.x.ai/v1/chat/completions", "model": "grok-4"}


class GR:
    def __init__(self, code, text="ahoj"): self.status_code, self._t = code, text
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)
    def json(self): return {"candidates": [{"content": {"parts": [{"text": self._t}]}}]}

def test_gemini_rotation_model_major(monkeypatch, tmp_path):
    for k in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GEMINI_MODEL", "LLM_MODEL"): monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"; f.write_text("DISCORD_TOKEN=x\nGEMINI_API_KEYS=k1,k2\nGEMINI_MODEL=gemini-x\n")
    monkeypatch.setenv("OKU_GEMINI_ENV_FILE", str(f))
    calls = []
    def post(url, headers, json, timeout):
        calls.append((url.split("/models/")[1].split(":")[0], headers["x-goog-api-key"]))
        assert json["systemInstruction"]["parts"][0]["text"] == "sys"
        assert json["contents"][1]["role"] == "model"
        return GR(429) if len(calls) < 3 else GR(200, "dobrý")
    out = core.gemini("sys", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], post=post)
    assert out == "dobrý"
    assert calls == [("gemini-x", "k1"), ("gemini-x", "k2"), ("gemini-2.5-flash", "k1")]

def test_gemini_env_file_only_reads_gemini_names(tmp_path):
    f = tmp_path / ".env"; f.write_text("DISCORD_TOKEN=secret\nGEMINI_API_KEY=\"k\"\n")
    assert core._env_file_values(f) == {"GEMINI_API_KEY": "k"}

def test_gemini_no_keys(monkeypatch):
    for k in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "OKU_GEMINI_ENV_FILE"): monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError): core.gemini("s", [])
