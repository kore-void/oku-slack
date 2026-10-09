"""Event sources feeding the diary (plan 1.3/1.4). Sources never post to Slack and never mutate state: they only
turn raw input into diary drafts that Diary.ingest() validates and dedupes.

- JsonlTail: generic byte-offset tail of an append-only JSONL file (offset kept in diary kv; a half-written last
  line waits for the next poll; truncation/rotation restarts from 0, dedupe keys absorb the replay).
- WheelOutbox: logs/outbox/wheel.jsonl written by oku_slack.wheel.world_client (rows already carry dedupe keys).
- UsageTail: logs/usage.jsonl (one line per LLM call by the bridge/meeting/wheel scenes) -> `bridge.reply`.
- LegacyWheelDb: read-only tail of the old in-wheel diary (wheel.sqlite3 table world_events, P-000 code), so the
  world keeps seeing wheel rows until the wheel with the outbox adapter is deployed. Same dedupe keys as backfill."""
import datetime, hashlib, json, logging, pathlib, sqlite3

log = logging.getLogger("oku_world.ingest")

def read_lines(path, offset):
    """Complete JSON lines from byte offset -> ([(line_offset, obj|None)], new_offset)."""
    p = pathlib.Path(path)
    try:
        with open(p, "rb") as f:
            size = f.seek(0, 2)
            if offset > size: offset = 0  # truncated / rotated
            f.seek(offset); data = f.read()
    except FileNotFoundError: return [], 0
    except OSError as e: log.warning("tail %s: %s", p.name, type(e).__name__); return [], offset
    end = data.rfind(b"\n")
    if end < 0: return [], offset
    out, pos = [], offset
    for raw in data[: end + 1].split(b"\n")[:-1]:
        here = pos; pos += len(raw) + 1
        if not raw.strip(): continue
        try: out.append((here, json.loads(raw.decode("utf-8")), raw))
        except (ValueError, UnicodeDecodeError): out.append((here, None, raw))
    return out, offset + end + 1

class JsonlTail:
    name = "jsonl"
    def __init__(self, diary, path, kv_key=None, max_lines=2000):
        self.diary, self.path, self.max_lines = diary, pathlib.Path(path), max_lines
        self.kv_key = kv_key or f"offset:{self.name}"
        self.errors = 0

    def to_draft(self, obj, line_offset, raw): raise NotImplementedError

    def poll(self, now=None):
        """Ingest new complete lines. Returns {"ok": n, "dup": n, "invalid": n, "error": n}."""
        off = int(self.diary.kv_get(self.kv_key, 0) or 0)
        lines, new_off = read_lines(self.path, off)
        res = {"ok": 0, "dup": 0, "invalid": 0, "error": 0}
        if lines and lines[0][0] < off: off = 0  # file was truncated: read_lines restarted at 0
        for i, (lo, obj, raw) in enumerate(lines):
            if i >= self.max_lines: new_off = lo; break
            try: draft = self.to_draft(obj, lo, raw) if obj is not None else None
            except Exception as e: log.warning("%s: bad line at %d: %s", self.name, lo, type(e).__name__); draft = None
            if draft is None: res["invalid"] += 1; continue
            st, _ = self.diary.ingest(draft); res[st] = res.get(st, 0) + 1
        if new_off != off: self.diary.kv_set(self.kv_key, new_off)
        return res

class WheelOutbox(JsonlTail):
    """oku_wheel -> world: rows already shaped as drafts by wheel.world_client (source=wheel, dedupe_key=wheel:<uuid>)."""
    name = "wheel"
    def to_draft(self, obj, line_offset, raw):
        if not isinstance(obj, dict): return None
        d = {k: obj.get(k) for k in ("type", "actor", "subject", "payload", "ts", "dedupe_key")}
        d["source"] = "wheel"  # the outbox can only speak for the wheel
        if not d["dedupe_key"]: d["dedupe_key"] = "wheel:off:" + hashlib.sha1(raw).hexdigest()[:16] + f":{line_offset}"
        return d

def _iso_ts(s):
    return datetime.datetime.fromisoformat(str(s)).timestamp()

class UsageTail(JsonlTail):
    """logs/usage.jsonl -> bridge.reply (one per LLM call; metadata only, the file never holds texts)."""
    name = "usage"
    KEEP = ("kind", "channel", "thread_ts", "model", "backend", "latency_ms", "in", "out", "think", "est_usd")
    def to_draft(self, obj, line_offset, raw):
        if not isinstance(obj, dict) or not obj.get("ts"): return None
        pl = {k: obj.get(k) for k in self.KEEP if obj.get(k) is not None}
        pl["llm_calls"] = 1
        ch, th = obj.get("channel"), obj.get("thread_ts")
        return {"type": "bridge.reply", "source": "bridge", "actor": (obj.get("persona") or None),
                "subject": f"slack:{ch}:{th}" if ch and th else None, "ts": _iso_ts(obj["ts"]), "payload": pl,
                "dedupe_key": f"usage:{line_offset}:{hashlib.sha1(raw).hexdigest()[:12]}"}

def open_ro(path):
    """Read-only SQLite connection (never creates or writes the wheel DB)."""
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)

def legacy_rows(path, after_seq=0, limit=5000):
    """Rows of the old in-wheel diary (wheel.sqlite3:world_events) after seq. [] when the DB/table is absent."""
    p = pathlib.Path(path)
    if not p.exists(): return []
    con = open_ro(p)
    try:
        if not con.execute("select 1 from sqlite_master where type='table' and name='world_events'").fetchone(): return []
        cols = [r[1] for r in con.execute("pragma table_info(world_events)")]
        rows = con.execute("select * from world_events where seq>? order by seq limit ?", (after_seq, limit)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally: con.close()

def legacy_draft(r, source):
    pl = json.loads(r.get("payload") or "{}")
    pl = {k: v for k, v in pl.items()}
    pl["via"] = pl.get("via") or r.get("source") or "engine"
    pl["legacy_id"] = r.get("id")
    subj = r.get("subject")
    return {"type": r["type"], "source": source, "actor": r.get("actor"), "subject": subj, "ts": r["ts"],
            "payload": pl, "dedupe_key": f"wheel-legacy:{r['seq']}"}

class LegacyWheelDb:
    name = "wheel_legacy"
    def __init__(self, diary, path, kv_key="offset:wheel_legacy"):
        self.diary, self.path, self.kv_key = diary, pathlib.Path(path), kv_key

    def poll(self, now=None):
        res = {"ok": 0, "dup": 0, "invalid": 0, "error": 0}
        off = int(self.diary.kv_get(self.kv_key, 0) or 0)
        try: rows = legacy_rows(self.path, off)
        except sqlite3.Error as e: log.info("legacy wheel db busy/unreadable: %s", type(e).__name__); return res
        for r in rows:
            st, _ = self.diary.ingest(legacy_draft(r, "wheel")); res[st] = res.get(st, 0) + 1
            off = max(off, int(r["seq"]))
        if rows: self.diary.kv_set(self.kv_key, off)
        return res
