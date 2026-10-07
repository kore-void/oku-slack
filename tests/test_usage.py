import json, pytest
from oku_slack import core, usage, meeting as M
from oku_slack.bridge import Bridge

CFG = core.load_config()
META = {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 300, "totalTokenCount": 1500}

@pytest.fixture(autouse=True)
def _log(tmp_path, monkeypatch):
    monkeypatch.setenv("OKU_USAGE_LOG", str(tmp_path / "usage.jsonl")); usage.configure(CFG)
    yield tmp_path / "usage.jsonl"

class R:
    def __init__(self, data, code=200): self.data, self.status_code = data, code
    def json(self): return self.data
    def raise_for_status(self): pass

def test_gemini_records_usage(monkeypatch, _log):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    for k in ("OKU_GEMINI_ENV_FILE", "GEMINI_MODEL", "LLM_MODEL"): monkeypatch.delenv(k, raising=False)
    calls = []
    def post(url, headers, json, timeout):
        calls.append(1)
        if len(calls) == 1: return R({}, 429)
        return R({"candidates": [{"content": {"parts": [{"text": "zisk"}]}}], "usageMetadata": META, "modelVersion": "gemini-2.5-flash"})
    usage.set_context(persona="babis", channel="C1", thread_ts="1.0", kind="solo")
    assert core.gemini("s", [], post=post) == "zisk"
    e = json.loads(_log.read_text(encoding="utf-8").splitlines()[-1])
    assert (e["persona"], e["channel"], e["thread_ts"], e["model"], e["key_index"]) == ("babis", "C1", "1.0", "gemini-2.5-flash", 1)
    assert (e["in"], e["out"], e["think"], e["total"]) == (1000, 200, 300, 1500)
    assert "k2" not in _log.read_text() and isinstance(e["latency_ms"], int) and e["ts"]
    assert e["est_usd"] == pytest.approx((1000 * 0.30 + 500 * 2.50) / 1e6)

def test_missing_usage_is_unknown_not_zero(_log):
    e = usage.record("gemini-2.5-flash", 0, 0.1, {"candidates": []})
    assert e["in"] == e["out"] == e["think"] == e["total"] == "UNKNOWN" and e["est_usd"] == "UNKNOWN"
    assert "~$UNKNOWN" in usage.format_call(e)

def test_cost_math():
    p = CFG["pricing"]
    assert usage.cost({"model": "gemini-2.5-flash-lite", "in": 1_000_000, "out": 1_000_000, "think": 0}, p) == pytest.approx(0.50)
    assert usage.cost({"model": "gemini-2.5-flash", "in": 2_000_000, "out": 0, "think": 1_000_000}, p) == pytest.approx(3.10)
    assert usage.cost({"model": "gemini-2.5-flash-001", "in": 1_000_000, "out": 0, "think": 0}, p) == pytest.approx(0.30)
    assert usage.cost({"model": "other", "in": 1, "out": 1, "think": 0}, p) is None
    assert CFG["reporting"] == {"per_call": True, "meeting_summary": True, "dm_user": "U0C6XAN3EG3"}

def test_format_call():
    e = {"persona": "babis", "model": "gemini-2.5-flash", "in": 1000, "out": 200, "think": 300, "est_usd": 0.00156}
    assert usage.format_call(e) == "babis · gemini-2.5-flash · 1000/200(+300) tokens · ~$0.0016"

def test_summary_aggregation():
    calls = [{"persona": "babis", "in": 100, "out": 10, "think": 5, "est_usd": 0.01},
             {"persona": "alenka", "in": 200, "out": 20, "think": 0, "est_usd": 0.02},
             {"persona": "babis", "in": 300, "out": 30, "think": 0, "est_usd": 0.03}]
    s = usage.summarize(calls, turns=3, duration_s=125)
    assert s["total"] == {"calls": 3, "in": 600, "out": 60, "think": 5, "usd": pytest.approx(0.06)}
    assert s["per_persona"]["babis"]["in"] == 400 and s["per_persona"]["alenka"]["calls"] == 1
    txt = usage.format_summary(s, "C", "1.0")
    assert "3 tahů" in txt and "2m05s" in txt and "• alenka" in txt and "~$0.0600" in txt
    s2 = usage.summarize(calls + [{"persona": "marty", "in": "UNKNOWN", "out": "UNKNOWN", "think": "UNKNOWN", "est_usd": "UNKNOWN"}])
    assert s2["total"]["in"] == "UNKNOWN" and s2["per_persona"]["babis"]["in"] == 400

class Client:
    def __init__(self, fail=False): self.posts, self.fail = [], fail
    def conversations_open(self, users):
        if self.fail: raise RuntimeError("missing_scope")
        return {"channel": {"id": "D1"}}
    def chat_postMessage(self, **kw): self.posts.append(kw)
    def conversations_replies(self, **kw): return {"messages": []}

def _gen_with_usage(s, h):
    usage.record("gemini-2.5-flash", 0, 0.2, {"usageMetadata": META}); return "odpověď"

def test_solo_reply_dms_report():
    c = Client(); b = Bridge(c, CFG, "B", "babis", gen=_gen_with_usage); b.reporter = usage.Reporter(c, CFG)
    b.handle({"channel": "C", "ts": "1", "text": "ahoj"})
    assert c.posts[0] == {"channel": "C", "thread_ts": "1", "text": "odpověď"}
    assert c.posts[1]["channel"] == "D1" and c.posts[1]["text"].startswith("babis · gemini-2.5-flash · 1000/200(+300)")

def test_reporting_failure_isolated():
    c = Client(); b = Bridge(c, CFG, "B", "babis", gen=_gen_with_usage); b.reporter = usage.Reporter(Client(fail=True), CFG)
    b.handle({"channel": "C", "ts": "1", "text": "ahoj"})
    assert c.posts == [{"channel": "C", "thread_ts": "1", "text": "odpověď"}]
    class Boom:
        def call(self, e): raise RuntimeError("x")
    b.reporter = Boom(); b.handle({"channel": "C", "ts": "2", "text": "ahoj"}); assert len(c.posts) == 2

def test_per_call_switch_off():
    c = Client(); r = usage.Reporter(c, dict(CFG, reporting={"per_call": False, "dm_user": "U"}))
    assert r.call({"persona": "x"}) is False and c.posts == []

def test_meeting_summary_dm():
    clients, c = {}, M.Coordinator(CFG, gen=_gen_with_usage)
    for k, u in (("babis", "UB"), ("alenka", "UA")):
        clients[k] = Client(); c.add(k, Bridge(clients[k], CFG, u, k, gen=_gen_with_usage))
    dm = Client(); c.reporter = usage.Reporter(dm, CFG)
    m = c.claim({"channel": "C", "ts": "1.0", "user": "UK", "text": "<@UB> <@UA> porada"})
    c.start(m, turns=3, sleep=lambda x: None).join(5)
    assert len(dm.posts) == 1 and "3 tahů" in dm.posts[0]["text"] and "3000/600(+900)" in dm.posts[0]["text"]
    assert sum(len(cl.posts) for cl in clients.values()) == 3
