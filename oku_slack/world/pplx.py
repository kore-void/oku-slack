"""Optional free podnet source via the Perplexity CLI `void-pplx-ask` (Kore's PC; unlimited plan).

A few times a day (default every 120 min between 08:00 and 22:00 Europe/Prague) it asks pplx for new public posts or
videos of the configured accounts (default Andrej Babiš on X) and, if configured, stream announcements, then writes
what it finds as podnety into the SAME inbox (podnet.append, source "pplx"), deduped by URL.

The pplx output is UNTRUSTED DATA. It is never executed, never followed and never passed to an LLM. The parser only
extracts URLs (+ an optional short title from the same answer line, cut to 120 chars) and keeps a URL ONLY when:
- its host is x.com/twitter.com, youtube.com/youtu.be, twitch.tv or kick.com (subdomains allowed);
- X: the path is /<configured handle>/status/<id> and the id's snowflake time is within `max_age_h` (default 48 h;
  pplx happily returns year-old posts);
- YouTube: a watch?v= / shorts / youtu.be video of a configured channel query is not verifiable, so YouTube/Twitch/Kick
  links are only kept for configured stream accounts (path must start with the configured channel).
At most `max_per_run` (3) podnety per run. One call at a time (a run in progress is never overlapped); the call runs in
a background thread so the world loop never blocks. Config [world.podnet_pplx]: enabled, interval_min, hours,
accounts (X handles), streams (e.g. ["twitch:somechannel", "kick:somechannel", "youtube:@somechannel"]),
mode (fast), timeout_s, command (default "void-pplx-ask"), max_age_h, max_per_run. Tests mock `runner`."""
import logging, os, re, shutil, subprocess, threading, time, urllib.parse
from . import podnet, tz, xsource

log = logging.getLogger("oku_world.pplx")
DEFAULTS = {"enabled": False, "interval_min": 120, "hours": ["08:00", "22:00"], "accounts": ["AndrejBabis"], "streams": [],
            "mode": "fast", "timeout_s": 150, "command": "void-pplx-ask", "max_age_h": 48, "max_per_run": 3}
ALLOWED_HOSTS = ("x.com", "twitter.com", "youtube.com", "youtu.be", "twitch.tv", "kick.com")
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`|]+")
X_EPOCH_MS = 1288834974657
STREAM_HOSTS = {"twitch": "twitch.tv", "kick": "kick.com", "youtube": "youtube.com"}

def host_allowed(host):
    h = (host or "").lower().rstrip(".")
    return any(h == a or h.endswith("." + a) for a in ALLOWED_HOSTS)

def snowflake_ts(status_id):
    """Unix time encoded in an X post id (ids after 2010-11)."""
    try: return ((int(status_id) >> 22) + X_EPOCH_MS) / 1000.0
    except (TypeError, ValueError): return None

def question(accounts, streams):
    acc = ", ".join("@" + a for a in accounts)
    q = (f"List the newest public posts, especially videos, published by the X account(s) {acc} in the last 48 hours. "
         "For each post give exactly one line: URL | short neutral title (max 10 words). "
         "Use direct post URLs like https://x.com/<handle>/status/<id>.")
    if streams:
        q += (" Also list announced or live streams of these channels in the next 24 hours, same line format with the channel or "
              "video URL: " + ", ".join(streams) + ".")
    return q + " Output only those lines, nothing else."

def _title(line, url):
    if "|" not in line: return None
    t = line.split("|", 1)[1]
    t = URL_RE.sub("", t)
    t = re.sub(r"[\[\]()*_`#<>@|{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -–:")
    return t[:120] or None

def parse(output, accounts, streams=(), now=None, max_age_h=48):
    """pplx output (untrusted text) -> [{"url", "kind", "author", "title"}], deduped by normalised URL. Pure."""
    now = time.time() if now is None else now
    handles = {a.lower(): a for a in accounts}
    chans = []
    for s in streams or []:
        if isinstance(s, str) and ":" in s:
            plat, ch = s.split(":", 1); ch = ch.strip().lstrip("/")
            if plat in STREAM_HOSTS and re.match(r"^@?[A-Za-z0-9_.-]{2,40}$", ch): chans.append((STREAM_HOSTS[plat], ch.lower()))
    out, seen = [], set()
    for line in str(output or "").splitlines()[:400]:
        for raw in URL_RE.findall(line)[:10]:
            raw = raw.rstrip(".,;:!?)]}*_")
            try: url = podnet.normalize_url(raw)
            except podnet.Invalid: continue
            p = urllib.parse.urlsplit(url); host = p.hostname or ""
            if not host_allowed(host) or url in seen: continue
            item = None
            m = re.match(r"^/([a-z0-9_]{1,15})/status/(\d{5,25})$", p.path) if host == "x.com" else None
            if m and m.group(1) in handles:
                t = snowflake_ts(m.group(2))
                if t is None or t < now - max_age_h * 3600 or t > now + 3600: continue
                hd = handles[m.group(1)]
                kind = "x_video" if re.search(r"(?i)video|reels?\b|klip|záznam", line) else "x_post"
                item = {"url": f"https://x.com/{hd}/status/{m.group(2)}", "kind": kind, "author": "@" + hd}
            else:
                for h, ch in chans:
                    if (host == h or host.endswith("." + h)) and p.path.lower().lstrip("/").startswith(ch):
                        item = {"url": url, "kind": "stream", "author": ch}
                        break
            if item:
                seen.add(url); item["title"] = _title(line, raw); out.append(item)
    return out

CLI_SLOTS = threading.BoundedSemaphore(1)   # one void-pplx-ask at a time for the whole world (podnet + news pollers)

def run_cli(command, q, mode, timeout_s):
    """Run the pplx CLI once (argument list, no shell). Returns stdout text (UTF-8, errors replaced).
    Waits for CLI_SLOTS, so the podnet and news pollers never run two pplx calls at once."""
    return slot_call(_run_cli, command, q, mode, timeout_s)

def slot_call(fn, command, q, mode, timeout_s):
    if not CLI_SLOTS.acquire(timeout=float(timeout_s) * 3 + 60): raise subprocess.TimeoutExpired(command, timeout_s)
    try: return fn(command, q, mode, timeout_s)
    finally: CLI_SLOTS.release()

def _run_cli(command, q, mode, timeout_s):
    exe = shutil.which(command) or command
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run([exe, "--mode", mode, "--timeout-s", str(int(timeout_s)), q], capture_output=True, timeout=float(timeout_s) + 30,
                       env=env, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (r.stdout or b"").decode("utf-8", errors="replace")

class PplxSource:
    name = "pplx"
    def __init__(self, svc, runner=None, background=True):
        self.svc, self.runner, self.background = svc, runner or run_cli, background
        self.state, self._said, self.running, self.last = "idle", None, False, None
        self._lock = threading.Lock()

    def cfg(self):
        c = dict(DEFAULTS); raw = self.svc.settings().get("podnet_pplx")
        if isinstance(raw, dict): c.update(raw)
        c["timezone"] = self.svc.settings().get("timezone", tz.PRAGUE)
        c["accounts"] = [a.lstrip("@") for a in (c.get("accounts") or []) if isinstance(a, str) and xsource.HANDLE_RE.match(a.lstrip("@"))]
        return c

    def poll(self, now=None, force=False):
        now = self.svc.clock() if now is None else now
        c = self.cfg(); d = self.svc.diary
        if not c.get("enabled") and not force:
            if self._said != "disabled": log.info("pplx source disabled: config"); self._said = "disabled"
            self.state = "disabled: config"; return {"state": self.state}
        if not force and not xsource.in_hours(now, c.get("hours"), c["timezone"]): self.state = "idle: outside hours"; return {"state": self.state}
        if not force and now < float(d.kv_get("pplx:next_poll", 0) or 0): return {"state": self.state}
        if not (c["accounts"] or c.get("streams")): self.state = "idle: nothing configured"; return {"state": self.state}
        with self._lock:
            if self.running: return {"state": "running"}
            self.running = True
        d.kv_set("pplx:next_poll", now + max(30.0, float(c["interval_min"])) * 60)
        if self.background and not force:
            threading.Thread(target=self.run_once, args=(now, c), daemon=True, name="pplx").start(); return {"state": "started"}
        return self.run_once(now, c)

    def run_once(self, now, c):
        res = {"state": "ok", "found": 0, "appended": 0, "urls": []}
        try:
            out = self.runner(c["command"], question(c["accounts"], c.get("streams") or []), c["mode"], c["timeout_s"])
            items = parse(out, c["accounts"], c.get("streams") or [], now=now, max_age_h=float(c["max_age_h"]))
            res["found"] = len(items)
            for it in items:
                if res["appended"] >= int(c["max_per_run"]): break
                if self.svc.diary.by_dedupe(podnet.key_for(it["url"])): continue
                try:
                    podnet.append(it["url"], it["kind"], it.get("title"), it.get("author"), source="pplx", observed_at=now,
                                  logs_dir=self.svc.logs_dir, path=getattr(self.svc, "inbox_path", None), now=now)
                    res["appended"] += 1; res["urls"].append(it["url"])
                except podnet.Invalid: pass
            log.info("pplx run: %d candidate url(s), %d new podnet(s)", res["found"], res["appended"])
        except subprocess.TimeoutExpired: res["state"] = "timeout"; log.warning("pplx run timed out")
        except FileNotFoundError: res["state"] = "error: no cli"; log.warning("pplx source: %s not found", c.get("command"))
        except Exception as e: res["state"] = "error"; log.warning("pplx run failed: %s", type(e).__name__)
        finally:
            self.running = False; self.state = res["state"]; self.last = dict(res, at=now)
        return res
