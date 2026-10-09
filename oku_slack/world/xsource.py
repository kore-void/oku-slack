"""X (Twitter) podnet poller (ECOSYSTEM-PLAN P-004 §4.1): new VIDEO posts of configured public accounts -> inbox.

- Enabled ONLY when env X_BEARER_TOKEN is set (on Windows also read from HKCU\\Environment, so a token set with setx
  works after `heimdall restart oku_world` even if the daemon env predates it). Without a token it logs
  `x source disabled: no token` once and does nothing. The token value is never logged or written anywhere.
- Official X API v2, read-only GETs: /2/users/by/username/<handle> (id cached in kv) and
  /2/users/<id>/tweets?since_id=..&exclude=retweets,replies&expansions=attachments.media_keys&media.fields=type.
  It never posts, likes or follows.
- Polls every `interval_min` (default 10) between `hours` (default 08:00-22:00 Europe/Prague).
- First poll per account only records the newest id as since_id (no flood of old posts).
- video_only (default): a post counts only if one of its media is a video (`kind = x_video`).
- 401/402/403/429 -> one `source.degraded` diary row per hour and back-off (3 x interval, or until x-rate-limit-reset).
- New posts are appended to the podnet inbox (podnet.append, source "x"); the inbox tail turns them into
  `podnet.received`, deduped by URL like every other feeder.
Config: [world.podnet_x] enabled = true, accounts = ["AndrejBabis"], interval_min = 10, hours = ["08:00", "22:00"],
video_only = true, max_results = 10. Stdlib only; `fetch` is injectable (tests never call the real API)."""
import json, logging, os, re, time, urllib.error, urllib.parse, urllib.request
from . import podnet, tz

log = logging.getLogger("oku_world.x")
API = "https://api.x.com/2"
DEFAULTS = {"enabled": True, "accounts": ["AndrejBabis"], "interval_min": 10, "hours": ["08:00", "22:00"], "video_only": True,
            "max_results": 10, "timezone": tz.PRAGUE}
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DEGRADE = (401, 402, 403, 429)

def bearer_token(env=None):
    """X_BEARER_TOKEN from env, else (Windows) HKCU\\Environment. Never logged."""
    env = os.environ if env is None else env
    v = env.get("X_BEARER_TOKEN")
    if v or os.name != "nt" or env is not os.environ: return v or None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: return winreg.QueryValueEx(k, "X_BEARER_TOKEN")[0] or None
    except OSError: return None

class HttpError(Exception):
    def __init__(self, status, reset=None): super().__init__(status); self.status, self.reset = status, reset

def http_get(url, token, timeout=15.0):
    """GET only. Returns parsed JSON or raises HttpError(status). Proxies from env are ignored."""
    req = urllib.request.Request(url, method="GET", headers={"Authorization": "Bearer " + token, "User-Agent": "oku-world/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r: return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reset = e.headers.get("x-rate-limit-reset") if e.headers else None
        raise HttpError(e.code, float(reset) if reset and str(reset).isdigit() else None)

def in_hours(now, hours, zone=tz.PRAGUE):
    if not hours or len(hours) != 2: return True
    a, b = tz.hm(hours[0]), tz.hm(hours[1]); m = tz.minutes(now, zone)
    return a <= m < b if a <= b else (m >= a or m < b)

def is_video(tweet, media):
    keys = ((tweet.get("attachments") or {}).get("media_keys") or [])
    return any((media.get(k) or {}).get("type") == "video" for k in keys)

class XSource:
    name = "x"
    def __init__(self, svc, fetch=None, env=None):
        self.svc, self.fetch, self.env = svc, fetch or http_get, env
        self.state = "idle"; self._said = None

    def cfg(self):
        c = dict(DEFAULTS); raw = self.svc.settings().get("podnet_x")
        if isinstance(raw, dict): c.update(raw)
        c["timezone"] = self.svc.settings().get("timezone", tz.PRAGUE)
        c["accounts"] = [a.lstrip("@") for a in (c.get("accounts") or []) if isinstance(a, str) and HANDLE_RE.match(a.lstrip("@"))]
        return c

    def _once(self, state, msg, *a):
        if self._said != state: log.info(msg, *a); self._said = state
        self.state = state

    def poll(self, now=None):
        now = self.svc.clock() if now is None else now
        c = self.cfg(); d = self.svc.diary
        if not c.get("enabled", True): self._once("disabled: config", "x source disabled: config"); return {"state": self.state}
        token = bearer_token(self.env)
        if not token: self._once("disabled: no token", "x source disabled: no token"); return {"state": self.state}
        if not in_hours(now, c.get("hours"), c["timezone"]): self.state = "idle: outside hours"; return {"state": self.state}
        nxt = float(d.kv_get("x:next_poll", 0) or 0)
        if now < nxt: return {"state": self.state}
        d.kv_set("x:next_poll", now + max(1.0, float(c["interval_min"])) * 60)
        res = {"state": "ok", "new": 0}
        for h in c["accounts"]:
            try: res["new"] += self.poll_account(h, token, now, c)
            except HttpError as e:
                self.degraded(h, e, now, c); res["state"] = f"degraded:{e.status}"; break
            except Exception as e:
                log.warning("x poll %s failed: %s", h, type(e).__name__); res["state"] = "error"
        self.state = res["state"]; self._said = None if res["state"] == "ok" else self._said
        return res

    def degraded(self, handle, e, now, c):
        back = float(c["interval_min"]) * 60 * 3
        if e.reset and e.reset > now: back = max(back, e.reset - now + 5)
        self.svc.diary.kv_set("x:next_poll", now + back)
        hour = int(now // 3600)
        self.svc.diary.record("source.degraded", None, "source:x", {"source": "x", "status": e.status, "account": handle,
                              "retry_in_s": int(back)}, source="x", dedupe_key=f"x:degraded:{e.status}:{hour}")
        log.warning("x source degraded: HTTP %s (retry in %ds)", e.status, int(back))

    def user_id(self, handle, token):
        d = self.svc.diary; k = f"x:uid:{handle.lower()}"
        uid = d.kv_get(k)
        if uid: return uid
        r = self.fetch(f"{API}/users/by/username/{urllib.parse.quote(handle)}", token)
        uid = ((r or {}).get("data") or {}).get("id")
        if not uid or not str(uid).isdigit(): raise ValueError("no user id")
        d.kv_set(k, uid); return uid

    def poll_account(self, handle, token, now, c):
        d = self.svc.diary; uid = self.user_id(handle, token)
        sk = f"x:since_id:{handle.lower()}"; since = d.kv_get(sk)
        q = {"max_results": max(5, min(100, int(c.get("max_results", 10)))), "exclude": "retweets,replies",
             "expansions": "attachments.media_keys", "media.fields": "type", "tweet.fields": "created_at,attachments"}
        if since: q["since_id"] = since
        r = self.fetch(f"{API}/users/{uid}/tweets?" + urllib.parse.urlencode(q), token) or {}
        tweets = [t for t in (r.get("data") or []) if str(t.get("id", "")).isdigit()]
        if not tweets: return 0
        newest = max(tweets, key=lambda t: int(t["id"]))["id"]
        d.kv_set(sk, newest)
        if not since:
            log.info("x %s: first poll, since_id=%s (older posts ignored)", handle, newest); return 0
        media = {m.get("media_key"): m for m in ((r.get("includes") or {}).get("media") or [])}
        n = 0
        for t in sorted(tweets, key=lambda t: int(t["id"])):
            video = is_video(t, media)
            if c.get("video_only", True) and not video: continue
            url = f"https://x.com/{handle}/status/{t['id']}"
            if d.by_dedupe(podnet.key_for(url)): continue
            title = re.sub(r"https?://\S+", "", t.get("text") or "").strip()[:140] or None
            try:
                podnet.append(url, "x_video" if video else "x_post", title, "@" + handle, source="x",
                              observed_at=now, logs_dir=self.svc.logs_dir, path=getattr(self.svc, "inbox_path", None), now=now)
                n += 1
            except podnet.Invalid as e: log.warning("x %s: podnet rejected: %s", handle, e)
        return n
