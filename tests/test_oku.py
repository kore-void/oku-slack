import os, pytest
from oku_slack import core
from oku_slack.bridge import Bridge, FALLBACK, tokens, ignored, start_all

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

def test_handle_posts_as_own_persona_only():
    c = FakeClient(); b = Bridge(c, dict(CFG, icon_base_url=""), "B", "babis", gen=lambda s, h: "odpověď")
    b.handle({"channel": "C", "ts": "1", "text": "<@B> Alenko, za to může Kalousek?"})
    assert len(c.posts) == 1 and c.posts[0]["text"] == "odpověď" and c.posts[0]["thread_ts"] == "1"
    assert "username" not in c.posts[0] and "icon_url" not in c.posts[0]

def test_llm_failure_fallback_no_icon():
    c = FakeClient(); cfg = dict(CFG, icon_base_url="https://x.example/av")
    def boom(s, h): raise RuntimeError
    Bridge(c, cfg, "B", "alenka", gen=boom).handle({"channel": "C", "ts": "2", "thread_ts": "1", "text": "Alenko"})
    assert c.posts[0]["text"] == FALLBACK and "icon_url" not in c.posts[0] and "username" not in c.posts[0]

def test_kalousek_answers_mention():
    c = FakeClient(); seen = []
    b = Bridge(c, CFG, "K", "kalousek", gen=lambda s, h: seen.append(s) or "nejsem vinen")
    b.handle({"channel": "C", "ts": "3", "text": "<@K> ahoj"})
    assert c.posts[0]["text"] == "nejsem vinen" and seen[0] == core.build_prompt(CFG["personas"]["kalousek"])

def test_tokens_and_babis_fallback():
    env = {"SLACK_OKU_BOT_TOKEN": "b0", "SLACK_OKU_APP_TOKEN": "a0", "SLACK_OKU_ALENKA_BOT_TOKEN": "b1", "SLACK_OKU_ALENKA_APP_TOKEN": "a1"}
    assert tokens("babis", env) == ("b0", "a0")
    assert tokens("babis", dict(env, SLACK_OKU_BABIS_BOT_TOKEN="bb", SLACK_OKU_BABIS_APP_TOKEN="ab")) == ("bb", "ab")
    assert tokens("alenka", env) == ("b1", "a1")
    assert tokens("marty", env) == (None, None)

def test_start_all_skips_missing(caplog):
    class App:
        def __init__(self, token):
            self.token = token
            class Cl:
                def auth_test(s): return {"user_id": "U_" + token}
            self.client = Cl()
        def event(self, name): return lambda f: f
    class H:
        def __init__(self, app, tok): pass
        def connect(self): pass
    env = {"SLACK_OKU_BOT_TOKEN": "b0", "SLACK_OKU_APP_TOKEN": "a0", "SLACK_OKU_PETA_BOT_TOKEN": "bp"}
    with caplog.at_level("INFO"):
        started = start_all(CFG, env, App, H)
    assert list(started) == ["babis"] and started["babis"].bot == "U_b0"
    assert "persona=peta skipped" in caplog.text and "b0" not in caplog.text.replace("U_b0", "")

def test_loop_guard():
    c = FakeClient(); b = Bridge(c, CFG, "B", "babis", gen=lambda s, h: "x")
    for ev in ({"bot_id": "X"}, {"subtype": "message_changed"}, {"user": "B"}):
        b.handle(dict(ev, channel="C", ts="1", text="hi"))
    assert c.posts == [] and ignored({"bot_id": "X"}, "B") and not ignored({"user": "U1"}, "B")

def test_history_roles():
    class C(FakeClient):
        def conversations_replies(self, **kw): return {"messages": [
            {"user": "U1", "text": "<@B> ahoj"}, {"user": "B", "text": "moje"}, {"bot_id": "BX", "username": "Alenka", "text": "jiný bot"}]}
    h = Bridge(C(), CFG, "B", "babis").history({"channel": "C", "ts": "2", "thread_ts": "1"})
    assert [m["role"] for m in h] == ["user", "assistant", "user"] and h[2]["content"] == "[Alenka] jiný bot"

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
