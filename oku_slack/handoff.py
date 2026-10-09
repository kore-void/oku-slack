"""Wheel -> bridge hand-off over local files (both services run on the same host from the same checkout).
The wheel (service oku_wheel, its own Slack app) cannot make the persona bridge (service oku_slack) start a real
porada through Slack: the bridge ignores every bot message (loop guard). Instead the wheel appends a request line to
<outbox>/meeting_start.jsonl; the bridge polls it, starts meeting.Meeting in that thread and answers with a line in
<outbox>/meeting_ack.jsonl (status started | dup | rejected). The wheel also keeps <outbox>/live_threads.json, the
wheel-skit threads that are live right now, so the bridge can route plain (no @mention) human replies there.
Only local files: nothing listens on a port, nothing is reachable through the tunnel. No secrets in any file.
outbox = env OKU_MEETING_OUTBOX, else <repo>/logs/outbox."""
import json, logging, os, pathlib, threading, time, uuid

log = logging.getLogger("oku.handoff")
REQUESTS, ACKS, LIVE = "meeting_start.jsonl", "meeting_ack.jsonl", "live_threads.json"
MAX_AGE_S = 90.0          # older requests are stale (bridge was down) and never start a meeting
_lock = threading.Lock()

def outbox():
    env = os.environ.get("OKU_MEETING_OUTBOX")
    return pathlib.Path(env) if env else pathlib.Path(__file__).resolve().parent.parent / "logs" / "outbox"

def _append(name, obj):
    d = outbox(); d.mkdir(parents=True, exist_ok=True)
    with _lock, open(d / name, "a", encoding="utf-8") as f: f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return obj

def _read(name, offset=0):
    """Complete lines from byte offset. Returns (objects, new_offset); a half-written last line is left for later."""
    p = outbox() / name
    try:
        with open(p, "rb") as f:
            size = f.seek(0, 2)
            if offset > size: offset = 0  # truncated / rotated
            f.seek(offset); data = f.read()
    except FileNotFoundError: return [], 0
    except OSError as e: log.warning("handoff read %s: %s", name, type(e).__name__); return [], offset
    end = data.rfind(b"\n")
    if end < 0: return [], offset
    out = []
    for line in data[: end + 1].splitlines():
        try: out.append(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError): log.warning("handoff %s: bad line skipped", name)
    return out, offset + end + 1

# ---------- wheel side ----------
def request_meeting(channel, thread_ts, event_id=None, topic="", host="babis", clock=time.time):
    """Ask the bridge to start a real porada in channel/thread_ts. Returns the request (with its id)."""
    return _append(REQUESTS, {"id": uuid.uuid4().hex[:12], "at": clock(), "channel": channel, "thread_ts": thread_ts,
                              "event_id": event_id, "topic": (topic or "")[:500], "host": host})

def ack_for(req_id):
    """Latest ack for a request id, or None."""
    acks, _ = _read(ACKS)
    hit = [a for a in acks if a.get("id") == req_id]
    return hit[-1] if hit else None

def write_live_threads(threads, clock=time.time):
    """threads: {thread_ts: {channel, event_id, key, host, until, skit_running}}; expired entries are dropped."""
    now = clock(); keep = {ts: v for ts, v in threads.items() if float(v.get("until") or 0) > now}
    d = outbox(); d.mkdir(parents=True, exist_ok=True); tmp = d / (LIVE + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        for i in range(5):  # Windows: the reader may hold the file for a moment
            try: os.replace(tmp, d / LIVE); break
            except PermissionError: time.sleep(0.05 * (i + 1))
    return keep

def read_live_threads(clock=time.time):
    try: v = json.loads((outbox() / LIVE).read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}
    now = clock()
    return {ts: x for ts, x in (v or {}).items() if isinstance(x, dict) and float(x.get("until") or 0) > now}

# ---------- bridge side ----------
def ack(req_id, status, **extra):
    return _append(ACKS, dict({"id": req_id, "status": status, "at": time.time()}, **extra))

class Inbox:
    """Bridge-side poller: calls on_request(req) -> status for each fresh, not yet acked request, then acks it."""
    def __init__(self, on_request, clock=time.time, max_age=MAX_AGE_S):
        self.on_request, self.clock, self.max_age = on_request, clock, max_age
        self.offset, self.done = 0, {a.get("id") for a in _read(ACKS)[0]}

    def poll(self):
        reqs, self.offset = _read(REQUESTS, self.offset); out = []
        for r in reqs:
            rid = r.get("id")
            if not rid or rid in self.done: continue
            self.done.add(rid)
            if self.clock() - float(r.get("at") or 0) > self.max_age:
                log.info("meeting request %s stale, ignored", rid); ack(rid, "stale"); out.append((rid, "stale")); continue
            try: status = self.on_request(r) or "rejected"
            except Exception as e: log.error("meeting request %s failed: %s", rid, type(e).__name__); status = "error"
            ack(rid, status); out.append((rid, status))
        return out

    def run(self, period=1.0):
        def loop():
            while True:
                try: self.poll()
                except Exception as e: log.warning("handoff inbox: %s", type(e).__name__)
                time.sleep(period)
        t = threading.Thread(target=loop, daemon=True, name="meeting-inbox"); t.start(); return t
