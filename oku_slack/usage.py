"""LLM usage/cost accounting + Slack DM reporting. Never raises into reply/meeting paths.
Token counts come from Gemini usageMetadata; missing usage is recorded as "UNKNOWN" (never 0).
Costs are ESTIMATES from config [pricing] (USD per 1M tokens); free-tier keys actually cost $0."""
import json, os, threading, time, logging, datetime

log = logging.getLogger("oku.usage")
UNKNOWN = "UNKNOWN"
DEFAULT_PRICING = {"gemini-2.5-flash": {"input": 0.30, "output": 2.50},
                   "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40}}
_tl = threading.local()
_file_lock = threading.Lock()

def set_context(**kw):
    """Per-thread call context: persona, channel, thread_ts, kind (solo/meeting)."""
    _tl.ctx = dict(kw)

def get_context(): return dict(getattr(_tl, "ctx", {}) or {})

def begin_collect(): _tl.calls = []
def end_collect():
    c = getattr(_tl, "calls", None) or []; _tl.calls = None; return c
def take_last():
    e = getattr(_tl, "last", None); _tl.last = None; return e

def usage_log_path():
    from . import core
    return os.environ.get("OKU_USAGE_LOG") or str(core.ROOT / "logs" / "usage.jsonl")

def _tok(meta, name):
    v = meta.get(name) if isinstance(meta, dict) else None
    return v if isinstance(v, int) else UNKNOWN

def parse_usage(resp):
    meta = (resp or {}).get("usageMetadata") if isinstance(resp, dict) else None
    if not isinstance(meta, dict) or not meta:
        return {"in": UNKNOWN, "out": UNKNOWN, "think": UNKNOWN, "total": UNKNOWN}
    think = meta.get("thoughtsTokenCount")
    return {"in": _tok(meta, "promptTokenCount"), "out": _tok(meta, "candidatesTokenCount"),
            # thoughts absent while usage present = model did no thinking
            "think": think if isinstance(think, int) else 0, "total": _tok(meta, "totalTokenCount")}

def _price(model, pricing):
    pricing = pricing or DEFAULT_PRICING
    if model in pricing: return pricing[model]
    for m in sorted(pricing, key=len, reverse=True):  # e.g. versioned names "gemini-2.5-flash-001"
        if isinstance(pricing[m], dict) and model and model.startswith(m): return pricing[m]
    return None

def cost(entry, pricing=None):
    """USD estimate; output price applies to candidates + thinking tokens. None if unknown."""
    p = _price(entry.get("model"), pricing)
    i, o, t = entry.get("in"), entry.get("out"), entry.get("think")
    if not p or not isinstance(i, int) or not isinstance(o, int): return None
    t = t if isinstance(t, int) else 0
    return (i * p["input"] + (o + t) * p["output"]) / 1_000_000

def record(model, key_index, latency_s, resp, pricing=None, backend="gemini"):
    """Build entry, write log line + JSONL. Returns the entry. Never raises."""
    try:
        e = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), **{
             k: None for k in ("persona", "channel", "thread_ts", "kind")}}
        e.update(get_context())
        e.update({"backend": backend, "model": model, "key_index": key_index,
                  "latency_ms": int(round(latency_s * 1000))}, **parse_usage(resp))
        c = cost(e, pricing if pricing is not None else _CFG_PRICING.get("p"))
        e["est_usd"] = round(c, 6) if c is not None else UNKNOWN
        log.info(format_log(e))
        with _file_lock:
            path = usage_log_path(); os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f: f.write(json.dumps(e, ensure_ascii=False) + "\n")
        _tl.last = e
        if getattr(_tl, "calls", None) is not None: _tl.calls.append(e)
        return e
    except Exception as ex:
        log.warning("usage record failed: %s", type(ex).__name__); return None

_CFG_PRICING = {}
def configure(cfg):
    _CFG_PRICING["p"] = {k: v for k, v in (cfg.get("pricing") or {}).items() if isinstance(v, dict)} or None

def _usd(c): return f"~${c:.4f}" if isinstance(c, (int, float)) else "~$UNKNOWN"

def format_log(e):
    return (f"usage persona={e.get('persona')} kind={e.get('kind')} ch={e.get('channel')} thread={e.get('thread_ts')} "
            f"model={e.get('model')} key#{e.get('key_index')} latency={e.get('latency_ms')}ms "
            f"in={e.get('in')} out={e.get('out')} think={e.get('think')} total={e.get('total')} est={_usd(e.get('est_usd'))}")

def format_call(e):
    th = e.get("think")
    tok = f"{e.get('in')}/{e.get('out')}" + (f"(+{th})" if th not in (0, None) else "")
    return f"{e.get('persona')} · {e.get('model')} · {tok} tokens · {_usd(e.get('est_usd'))}"

def summarize(calls, turns=None, duration_s=None):
    def add(a, b):
        return UNKNOWN if not isinstance(a, int) or not isinstance(b, int) else a + b
    tot = {"calls": 0, "in": 0, "out": 0, "think": 0, "usd": 0.0}; per = {}
    for e in calls:
        for bucket in (tot, per.setdefault(e.get("persona") or "?", {"calls": 0, "in": 0, "out": 0, "think": 0, "usd": 0.0})):
            bucket["calls"] += 1
            for k in ("in", "out", "think"): bucket[k] = add(bucket[k], e.get(k))
            c = e.get("est_usd"); bucket["usd"] = bucket["usd"] + c if isinstance(c, (int, float)) and isinstance(bucket["usd"], float) else UNKNOWN
    return {"turns": turns, "duration_s": duration_s, "total": tot, "per_persona": per}

def format_summary(s, channel=None, thread_ts=None):
    t = s["total"]; d = s.get("duration_s")
    dur = f"{int(d) // 60}m{int(d) % 60:02d}s" if isinstance(d, (int, float)) else "?"
    def tk(b): return f"{b['in']}/{b['out']}" + (f"(+{b['think']})" if b["think"] not in (0,) else "")
    lines = [f"Porada skončila ({channel} {thread_ts}) · {s.get('turns')} tahů · {t['calls']} LLM volání · {dur} · "
             f"{tk(t)} tokens · {_usd(t['usd'])} (odhad; free-tier = $0)"]
    for p, b in sorted(s["per_persona"].items()):
        lines.append(f"• {p}: {b['calls']}× · {tk(b)} · {_usd(b['usd'])}")
    return "\n".join(lines)

class Reporter:
    """DMs Kore via one app's bot client (Babiš). Every failure is swallowed + logged."""
    def __init__(self, client, cfg):
        r = cfg.get("reporting") or {}
        self.client = client
        self.per_call = bool(r.get("per_call", True))
        self.meeting_summary = bool(r.get("meeting_summary", True))
        self.user = r.get("dm_user") or None
        self._im = None

    def dm(self, text):
        if not (self.client and self.user and text): return False
        try:
            if not self._im:
                self._im = self.client.conversations_open(users=self.user)["channel"]["id"]
            self.client.chat_postMessage(channel=self._im, text=text)
            return True
        except Exception as ex:
            log.warning("usage DM failed: %s", type(ex).__name__); return False

    def call(self, entry):
        try:
            if self.per_call and entry: return self.dm(format_call(entry))
        except Exception as ex: log.warning("usage report failed: %s", type(ex).__name__)
        return False

    def meeting(self, calls, turns, duration_s, channel=None, thread_ts=None):
        try:
            if self.meeting_summary:
                return self.dm(format_summary(summarize(calls, turns, duration_s), channel, thread_ts))
        except Exception as ex: log.warning("usage summary failed: %s", type(ex).__name__)
        return False
