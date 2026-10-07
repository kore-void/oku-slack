import os, pytest
from oku_slack import core
from oku_slack.bridge import Bridge, FALLBACK, register, start, wants

CFG = core.load_config()

def test_prompts_built_and_parody():
    for k, p in CFG["personas"].items():
        s = core.build_prompt(p)
        assert "PARODIE" in s and len(s) > 500, k

class FakeClient:
    def __init__(self, replies=None): self.posts, self.replies = [], replies or [{"user": "U1", "text": "<@B> Alenko?"}]
    def chat_postMessage(self, **kw): self.posts.append(kw)
    def conversations_replies(self, **kw): return {"messages": self.replies}

def test_handle_answers_only_as_own_persona():
    seen = []
    c = FakeClient(); b = Bridge(c, CFG, "babis", "B", gen=lambda s, h: seen.append(s) or "odpověď")
    b.handle({"channel": "C", "ts": "1", "text": "<@B> Alenko, za to může Kalousek?"})
    assert c.posts == [{"channel": "C", "thread_ts": "1", "text": "odpověď"}]  # no username/icon override, one post
    assert seen == [b.prompt] and "ANDREJ BABIŠ" in b.prompt

def test_kalousek_answers_mention():
    c = FakeClient(); b = Bridge(c, CFG, "kalousek", "K", gen=lambda s, h: "…")
    b.handle({"channel": "C", "ts": "5", "text": "<@K> ahoj"})
    assert c.posts == [{"channel": "C", "thread_ts": "5", "text": "…"}] and "KALOUSEK" in b.prompt

def test_llm_failure_fallback():
    c = FakeClient()
    def boom(s, h): raise RuntimeError
    Bridge(c, CFG, "alenka", "B", gen=boom).handle({"channel": "C", "ts": "2", "thread_ts": "1", "text": "Alenko"})
    assert c.posts == [{"channel": "C", "thread_ts": "1", "text": FALLBACK}] and FALLBACK.endswith("kampaň!")

def test_history_roles_and_peer_names():
    msgs = [{"user": "U1", "text": "<@B> a co <@M>?"}, {"user": "B", "bot_id": "BB", "text": "já"},
            {"user": "M", "bot_id": "BM", "text": "<@B> sociálky"}, {"user": "X", "bot_id": "BX", "bot_profile": {"name": "Cizí"}, "text": "spam"}]
    b = Bridge(FakeClient(msgs), CFG, "babis", "B", peers={"B": "Andrej", "M": "Marty"})
    assert b.history({"channel": "C", "ts": "3", "thread_ts": "1"}) == [
        {"role": "user", "content": "a co @Marty?"}, {"role": "assistant", "content": "já"},
        {"role": "user", "content": "[Marty] sociálky"}, {"role": "user", "content": "[Cizí] spam"}]

@pytest.mark.parametrize("event,ok", [({"user": "U1"}, True), ({"user": "U1", "bot_id": "BM"}, False),
    ({"user": "U1", "subtype": "message_changed"}, False), ({"subtype": "bot_message"}, False), ({"user": "B"}, False)])
def test_loop_guard(event, ok): assert wants(event, "B") is ok

class FakeApp:
    def __init__(self, uid): self.handlers, self.client = {}, type("C", (), {"auth_test": lambda s: {"user_id": uid}})()
    def event(self, name): return lambda f: self.handlers.setdefault(name, f)

def test_register_dispatch():
    app, got = FakeApp("B"), []
    b = Bridge(FakeClient(), CFG, "babis", "B", gen=lambda s, h: "x")
    register(app, b, spawn=got.append)
    app.handlers["app_mention"]({"user": "U1", "text": "<@B>"}); app.handlers["app_mention"]({"user": "M", "bot_id": "BM", "text": "<@B>"})
    app.handlers["message"]({"user": "U1", "channel_type": "im"}); app.handlers["message"]({"user": "U1", "channel_type": "channel"})
    app.handlers["message"]({"user": "B", "channel_type": "im"})
    assert got == [{"user": "U1", "text": "<@B>"}, {"user": "U1", "channel_type": "im"}]

def test_tokens_names_and_babis_fallback():
    env = {"SLACK_OKU_BOT_TOKEN": "lb", "SLACK_OKU_APP_TOKEN": "la", "SLACK_OKU_MARTY_BOT_TOKEN": "mb", "SLACK_OKU_MARTY_APP_TOKEN": "ma"}
    assert core.tokens("babis", env.get) == ("lb", "la", ("SLACK_OKU_BOT_TOKEN", "SLACK_OKU_APP_TOKEN"))
    assert core.tokens("marty", env.get) == ("mb", "ma", ("SLACK_OKU_MARTY_BOT_TOKEN", "SLACK_OKU_MARTY_APP_TOKEN"))
    assert core.tokens("alenka", env.get) == (None, None, ("SLACK_OKU_ALENKA_BOT_TOKEN", "SLACK_OKU_ALENKA_APP_TOKEN"))
    env.update(SLACK_OKU_BABIS_BOT_TOKEN="bb", SLACK_OKU_BABIS_APP_TOKEN="ba")
    assert core.tokens("babis", env.get)[:2] == ("bb", "ba")

def test_env_prefers_process_env_then_user_env(monkeypatch):
    monkeypatch.setenv("OKU_T", "proc"); monkeypatch.delenv("OKU_T2", raising=False)
    assert core.env("OKU_T", user_env=lambda n: "reg") == "proc"
    assert core.env("OKU_T2", user_env=lambda n: "reg") == "reg"

def test_start_skips_missing_bad_and_duplicate_tokens(caplog):
    env = {"SLACK_OKU_BOT_TOKEN": "xoxb-b", "SLACK_OKU_APP_TOKEN": "xapp-b",
           "SLACK_OKU_MARTY_BOT_TOKEN": "xoxb-m", "SLACK_OKU_MARTY_APP_TOKEN": "xapp-m",
           "SLACK_OKU_PETA_BOT_TOKEN": "xoxb-bad", "SLACK_OKU_PETA_APP_TOKEN": "xapp-p",
           "SLACK_OKU_KALOUSEK_BOT_TOKEN": "xoxb-m", "SLACK_OKU_KALOUSEK_APP_TOKEN": "xapp-k",
           "SLACK_OKU_ALENKA_BOT_TOKEN": "xoxb-a"}
    apps, handlers = {}, []
    def make_app(token):
        if token == "xoxb-bad": raise RuntimeError("invalid_auth")
        apps[token] = FakeApp({"xoxb-b": "UB", "xoxb-m": "UM"}[token]); return apps[token]
    class H:
        def __init__(self, app, tok): self.tok, self.on = tok, False; handlers.append(self)
        def connect(self): self.on = True
    with caplog.at_level("INFO", logger="oku"):
        up = start(CFG, get=env.get, make_app=make_app, make_handler=H)
    assert [k for k, _ in up] == ["babis", "marty"] and all(h.on for _, h in up)
    assert [h.tok for h in handlers] == ["xapp-b", "xapp-m"]
    log = caplog.text
    assert "persona=alenka skipped: missing SLACK_OKU_ALENKA_APP_TOKEN" in log
    assert "persona=bourak skipped: missing SLACK_OKU_BOURAK_BOT_TOKEN/SLACK_OKU_BOURAK_APP_TOKEN" in log
    assert "persona=peta failed to start: RuntimeError" in log
    assert "persona=kalousek skipped: SLACK_OKU_KALOUSEK_BOT_TOKEN is the same Slack app as persona=marty" in log
    assert "xoxb-" not in log and "xapp-" not in log

def test_start_nothing_connected():
    assert start(CFG, get=lambda n: None, make_app=None, make_handler=None) == []

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
