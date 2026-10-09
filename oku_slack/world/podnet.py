"""Podnety: real-life triggers (ECOSYSTEM-PLAN P-004) as diary-writing sources.

A *podnet* is one public link the world may react to (a Babiš video on X, a stream announcement, a news article).
Every feeder drops podnety into ONE append-only inbox, `<logs>/inbox/podnety.jsonl` (env OKU_WORLD_INBOX overrides):

    {"url": "https://x.com/AndrejBabis/status/1", "kind": "x_video", "author": "@AndrejBabis",
     "title": "short title <= 280 chars", "observed_at": 1791000000, "source": "manual", "test": false}

Feeders: Kore by hand, `python -m oku_slack.world.podnet <url> [--kind ..] [--text ..]` (this module's CLI), a script,
Grok Bot, the X API poller (`xsource.py`, only with X_BEARER_TOKEN) and the optional pplx poller (`pplx.py`).
The world service tails the inbox (`InboxSource`): each line is validated, normalised and deduped BY URL (dedupe key
`podnet:<sha1(normalised url)>`), and becomes one `podnet.received` diary row (source `podnet`). Invalid lines become
`podnet.rejected` (reason only). The title is stored as `title` (<= 280 chars); the diary never takes a `text` key.
Nothing here posts anywhere; the reaction is planned by `react.py`. Stdlib only."""
import argparse, datetime, hashlib, json, os, pathlib, re, sys, threading, time, urllib.parse
from . import ingest

KINDS = ("x_video", "x_post", "stream", "video", "news", "other")
MAX_URL, MAX_TITLE, MAX_AUTHOR, MAX_SOURCE = 500, 280, 64, 32
SOURCE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,31}$")
DROP_QUERY = re.compile(r"^(utm_.*|s|t|ref|ref_src|ref_url|fbclid|gclid|si|igshid|feature|mc_.*)$", re.I)
HOST_ALIASES = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "mobile.x.com": "x.com", "m.youtube.com": "youtube.com",
                "music.youtube.com": "youtube.com", "m.twitch.tv": "twitch.tv"}
_lock = threading.Lock()
ROOT = pathlib.Path(__file__).resolve().parents[2]

class Invalid(ValueError):
    pass

def inbox_path(logs_dir=None):
    """env OKU_WORLD_INBOX, else <logs_dir>/inbox/podnety.jsonl, else <OKU_WORLD_LOGS or checkout/logs>/inbox/podnety.jsonl."""
    env = os.environ.get("OKU_WORLD_INBOX")
    if env: return pathlib.Path(env)
    base = pathlib.Path(logs_dir or os.environ.get("OKU_WORLD_LOGS") or ROOT / "logs")
    return base / "inbox" / "podnety.jsonl"

def normalize_url(url):
    """Canonical form for dedupe: https, lower-case host without www./mobile., twitter.com -> x.com, no fragment,
    tracking query params dropped (YouTube's v= kept), no trailing slash. Raises Invalid."""
    if not isinstance(url, str): raise Invalid("bad_url")
    u = url.strip()
    if not u or len(u) > MAX_URL or any(c.isspace() for c in u) or any(ord(c) < 32 for c in u): raise Invalid("bad_url")
    try: p = urllib.parse.urlsplit(u)
    except ValueError: raise Invalid("bad_url")
    if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password: raise Invalid("bad_url")
    host = p.hostname.lower().rstrip(".")
    if host.startswith("www."): host = host[4:]
    host = HOST_ALIASES.get(host, host)
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) or host in ("localhost",): raise Invalid("bad_host")
    if p.port not in (None, 80, 443): raise Invalid("bad_port")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False) if not DROP_QUERY.match(k)]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1: path = path.rstrip("/")
    if host == "x.com": path = re.sub(r"^/([^/]+)", lambda m: "/" + m.group(1).lower(), path)   # handles are case-insensitive
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(sorted(q)), ""))

def host_of(url):
    return urllib.parse.urlsplit(url).hostname or ""

def key_for(url):
    """dedupe key of a podnet (by normalised URL)."""
    return "podnet:" + hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]

def _ts(v, now):
    if v is None or v == "": return now
    if isinstance(v, bool): raise Invalid("bad_observed_at")
    if isinstance(v, (int, float)): t = float(v)
    else:
        try: t = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError: raise Invalid("bad_observed_at")
    if not (1_577_836_800 <= t <= now + 86400): raise Invalid("observed_at_out_of_range")
    return t

def _short(v, n, name):
    if v is None: return None
    if not isinstance(v, str): raise Invalid("bad_" + name)
    v = re.sub(r"[\x00-\x1f\x7f]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    if len(v) > n: raise Invalid(name + "_too_long")
    return v or None

def validate(obj, now=None):
    """One inbox line -> clean podnet dict, or raise Invalid(reason). Pure (except the clock default)."""
    now = time.time() if now is None else now
    if not isinstance(obj, dict): raise Invalid("not_object")
    url = normalize_url(obj.get("url"))
    kind = obj.get("kind") or "other"
    if kind not in KINDS: raise Invalid("bad_kind")
    title = obj.get("title") if obj.get("title") is not None else obj.get("text")
    title = _short(title, MAX_TITLE, "title")
    author = _short(obj.get("author"), MAX_AUTHOR, "author")
    src = obj.get("source") or "inbox"
    if not isinstance(src, str) or not SOURCE_RE.match(src): raise Invalid("bad_source")
    test = obj.get("test", False)
    if not isinstance(test, bool): raise Invalid("bad_test")
    return {"url": url, "kind": kind, "title": title, "author": author, "observed_at": _ts(obj.get("observed_at"), now),
            "source": src, "test": test, "host": host_of(url), "key": "podnet:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]}

def to_draft(p):
    """Clean podnet -> `podnet.received` diary draft (deduped by URL)."""
    a = p.get("author")
    actor = ("ext:" + re.sub(r"[^A-Za-z0-9_.@-]", "", a).lstrip("@").lower())[:64] if a and re.sub(r"[^A-Za-z0-9_.@-]", "", a).lstrip("@") else None
    pl = {"url": p["url"], "kind": p["kind"], "host": p["host"], "author": p["author"], "title": p["title"],
          "observed_at": p["observed_at"], "source": p["source"], "test": p["test"]}
    return {"type": "podnet.received", "source": "podnet", "actor": actor, "subject": p["key"], "dedupe_key": p["key"],
            "payload": {k: v for k, v in pl.items() if v is not None}}

class InboxSource(ingest.JsonlTail):
    """Tail of <logs>/inbox/podnety.jsonl -> podnet.received (valid) / podnet.rejected (invalid line, reason only)."""
    name = "podnet_inbox"
    def __init__(self, diary, path, clock=time.time):
        super().__init__(diary, path, kv_key="offset:podnet_inbox", max_lines=500); self.clock = clock

    def to_draft(self, obj, line_offset, raw):
        try: return to_draft(validate(obj, self.clock()))
        except Invalid as e:
            return {"type": "podnet.rejected", "source": "podnet", "payload": {"reason": str(e)[:60], "line_offset": line_offset},
                    "dedupe_key": f"podnet:rejected:{line_offset}:{hashlib.sha1(raw).hexdigest()[:12]}"}

    def poll(self, now=None):
        off = int(self.diary.kv_get(self.kv_key, 0) or 0)
        lines, _ = ingest.read_lines(self.path, off)
        # unparseable JSON lines: JsonlTail counts them invalid; record a rejected row for them too
        for lo, obj, raw in lines:
            if obj is None and raw.strip():
                self.diary.ingest({"type": "podnet.rejected", "source": "podnet", "payload": {"reason": "bad_json", "line_offset": lo},
                                   "dedupe_key": f"podnet:rejected:{lo}:{hashlib.sha1(raw).hexdigest()[:12]}"})
        return super().poll(now)

def append(url, kind="other", title=None, author=None, source="manual", test=False, observed_at=None, path=None, logs_dir=None, now=None):
    """Validate and append one podnet line to the inbox. Returns the line written. Raises Invalid."""
    now = time.time() if now is None else now
    obj = {"url": url, "kind": kind, "author": author, "title": title, "observed_at": observed_at if observed_at is not None else now,
           "source": source, "test": bool(test)}
    clean = validate(obj, now)
    line = {k: clean[k] for k in ("url", "kind", "author", "title", "observed_at", "source", "test") if clean[k] is not None}
    p = pathlib.Path(path) if path else inbox_path(logs_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(p, "a", encoding="utf-8") as f: f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m oku_slack.world.podnet", description="Append one podnet (public link) to the OKÚ world inbox.")
    ap.add_argument("url")
    ap.add_argument("--kind", default="other", choices=KINDS)
    ap.add_argument("--text", "--title", dest="title", default=None, help="short title, <= 280 chars")
    ap.add_argument("--author", default=None, help="e.g. @AndrejBabis")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--test", action="store_true", help="mark as a test podnet (never handed to the bridge)")
    ap.add_argument("--inbox", default=None, help="inbox file (default: env OKU_WORLD_INBOX or <logs>/inbox/podnety.jsonl)")
    a = ap.parse_args(argv)
    try: line = append(a.url, a.kind, a.title, a.author, a.source, a.test, path=a.inbox)
    except Invalid as e:
        print(f"podnet rejected: {e}", file=sys.stderr); return 2
    print(json.dumps({"appended": line, "inbox": str(pathlib.Path(a.inbox) if a.inbox else inbox_path())}, ensure_ascii=False))
    return 0

if __name__ == "__main__": sys.exit(main())
