"""World diary (ECOSYSTEM-PLAN.md 1.5): append-only `world_events` in logs/world.sqlite3 plus a JSONL mirror
(logs/world.jsonl). Owned by the oku_world service, the single writer; sources (wheel outbox, usage.jsonl tail,
scheduler, ...) only hand it drafts, which `ingest()` validates and dedupes (unique, nullable `dedupe_key`).

Envelope: id, seq, ts, type, source, actor, subject, dedupe_key, payload, causal_parents, regime, content_version,
run_id. Privacy: human free text never enters the diary; payload keys that usually carry text are rejected.
`ingest()`/`record()` never raise into the caller: a diary failure is logged and the caller keeps running.

`source()` / `current_source()` stay here because the wheel tags its outbox rows with them (payload.via)."""
import contextlib, contextvars, json, logging, math, pathlib, re, sqlite3, threading, time

log = logging.getLogger("oku_world.diary")
SCHEMA = """
create table if not exists world_events(
  seq integer primary key autoincrement, id text unique, ts real not null, type text not null, source text,
  actor text, subject text, dedupe_key text unique, payload text, causal_parents text, regime text,
  content_version text, run_id text);
create index if not exists world_events_subject on world_events(subject);
create index if not exists world_events_ts on world_events(ts);
create index if not exists world_events_type on world_events(type);
create table if not exists kv(k text primary key, v text);
"""
COLS = ("seq", "id", "ts", "type", "source", "actor", "subject", "dedupe_key", "payload", "causal_parents", "regime",
        "content_version", "run_id")
SOURCES = frozenset({"wheel", "bridge", "schedule", "backfill", "director", "agent", "consequence", "budget", "world",
                     "x", "stream", "slack", "manual", "chatter", "podnet"})
TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,3}$")
TEXT_KEYS = frozenset({"text", "message", "body", "content", "prompt", "reply", "chat", "transcript"})
MAX_PAYLOAD = 4096
MIN_TS = 1_577_836_800.0  # 2020-01-01

_SOURCE = contextvars.ContextVar("oku_world_source", default=None)

@contextlib.contextmanager
def source(name):
    """Tag records made inside this block with an adapter source ('slack', 'room'); default is 'engine'."""
    tok = _SOURCE.set(name)
    try: yield
    finally: _SOURCE.reset(tok)

def current_source(default="engine"): return _SOURCE.get() or default

def git_version(root=None):
    """Short commit sha of the checkout (reads .git files, no subprocess; worktrees supported). 'unknown' if unavailable."""
    try:
        g = pathlib.Path(root or pathlib.Path(__file__).resolve().parents[2]) / ".git"
        if g.is_file():  # git worktree: ".git" is a file "gitdir: <path>"
            g = pathlib.Path(g.read_text().split(":", 1)[1].strip())
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"): return head[:7]
        ref = head.split(" ", 1)[1].strip()
        for base in (g, (g / "commondir").exists() and (g / (g / "commondir").read_text().strip()).resolve()):
            if not base: continue
            f = pathlib.Path(base) / ref
            if f.exists(): return f.read_text().strip()[:7]
            pr = pathlib.Path(base) / "packed-refs"
            if pr.exists():
                for line in pr.read_text().splitlines():
                    if line.endswith(" " + ref): return line.split()[0][:7]
    except Exception: pass
    return "unknown"

class Invalid(ValueError):
    pass

def validate(draft, now):
    """Return a clean draft dict or raise Invalid(reason). Pure."""
    if not isinstance(draft, dict): raise Invalid("not_object")
    t = draft.get("type")
    if not isinstance(t, str) or not TYPE_RE.match(t) or len(t) > 64: raise Invalid("bad_type")
    src = draft.get("source")
    if src not in SOURCES: raise Invalid("bad_source")
    out = {"type": t, "source": src}
    for k, n in (("actor", 64), ("subject", 160), ("dedupe_key", 200)):
        v = draft.get(k)
        if v is not None and (not isinstance(v, str) or not v or len(v) > n): raise Invalid(f"bad_{k}")
        out[k] = v
    pl = draft.get("payload") if draft.get("payload") is not None else {}
    if not isinstance(pl, dict): raise Invalid("bad_payload")
    bad = sorted(TEXT_KEYS & set(pl))
    if bad: raise Invalid("free_text_key:" + bad[0])
    try: enc = json.dumps(pl, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError): raise Invalid("payload_not_json")
    if len(enc.encode("utf-8")) > MAX_PAYLOAD: raise Invalid("payload_too_big")
    out["payload"] = json.loads(enc)
    ts = draft.get("ts")
    if ts is None: ts = now
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts): raise Invalid("bad_ts")
    if ts < MIN_TS or ts > now + 86400: raise Invalid("ts_out_of_range")
    out["ts"] = float(ts)
    par = draft.get("causal_parents")
    if par is not None and (not isinstance(par, list) or len(par) > 20 or not all(isinstance(p, str) and len(p) <= 32 for p in par)):
        raise Invalid("bad_causal_parents")
    out["causal_parents"] = par
    return out

class Diary:
    """SQLite diary + JSONL mirror. Thread-safe (one connection, RLock)."""
    def __init__(self, path=":memory:", jsonl=None, clock=time.time, regime="A_scarce", run_id="oku-world-1", version=None):
        if path != ":memory:": pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()
        self.clock, self.regime, self.run_id = clock, regime, run_id
        self.jsonl = pathlib.Path(jsonl) if jsonl else None
        self.version = version or git_version()
        self.stats = {"ok": 0, "dup": 0, "invalid": 0, "error": 0}
        self.last_invalid = None
        with self.lock: self.db.executescript(SCHEMA); self.db.commit()

    # ---------- kv ----------
    def kv_get(self, k, default=None):
        with self.lock: r = self.db.execute("select v from kv where k=?", (k,)).fetchone()
        return r[0] if r and r[0] is not None else default

    def kv_set(self, k, v):
        with self.lock: self.db.execute("insert or replace into kv(k,v) values(?,?)", (k, None if v is None else str(v))); self.db.commit()

    # ---------- write ----------
    def _first(self, subject):
        r = self.db.execute("select id from world_events where subject=? order by seq limit 1", (subject,)).fetchone()
        return r[0] if r else None

    def ingest(self, draft):
        """Validate + append one draft. Returns (status, row): ("ok", row) | ("dup", None) | ("invalid", reason) |
        ("error", None). Causal parent defaults to the first event about the same subject. Never raises."""
        try:
            d = validate(draft, self.clock())
        except Invalid as e:
            self.stats["invalid"] += 1; self.last_invalid = str(e)
            log.warning("diary: invalid draft (%s) type=%s source=%s", e, str((draft or {}).get("type") if isinstance(draft, dict) else "?")[:40],
                        str((draft or {}).get("source") if isinstance(draft, dict) else "?")[:20])
            return "invalid", str(e)
        try:
            with self.lock:
                if d["dedupe_key"] and self.db.execute("select 1 from world_events where dedupe_key=?", (d["dedupe_key"],)).fetchone():
                    self.stats["dup"] += 1; return "dup", None
                parents = d["causal_parents"]
                if parents is None:
                    p = self._first(d["subject"]) if d["subject"] else None; parents = [p] if p else []
                row = dict(d, causal_parents=parents, regime=self.regime, content_version=self.version, run_id=self.run_id)
                cur = self.db.execute(
                    "insert into world_events(ts,type,source,actor,subject,dedupe_key,payload,causal_parents,regime,content_version,run_id)"
                    " values(?,?,?,?,?,?,?,?,?,?,?)",
                    (row["ts"], row["type"], row["source"], row["actor"], row["subject"], row["dedupe_key"],
                     json.dumps(row["payload"], ensure_ascii=False), json.dumps(parents), self.regime, self.version, self.run_id))
                seq = cur.lastrowid; row["seq"], row["id"] = seq, f"we_{seq:04d}"
                self.db.execute("update world_events set id=? where seq=?", (row["id"], seq)); self.db.commit()
                self._mirror(row)
            self.stats["ok"] += 1
            return "ok", row
        except sqlite3.IntegrityError:
            self.stats["dup"] += 1; return "dup", None
        except Exception as e:
            self.stats["error"] += 1; log.warning("diary ingest %s failed: %s", d.get("type"), type(e).__name__); return "error", None

    def record(self, type, actor=None, subject=None, payload=None, source="world", parents=None, ts=None, dedupe_key=None):
        """Convenience for the core's own rows. Returns the row or None (dup/invalid/error). Never raises."""
        st, row = self.ingest({"type": type, "actor": actor, "subject": subject, "payload": payload or {}, "source": source,
                               "causal_parents": parents, "ts": ts, "dedupe_key": dedupe_key})
        return row if st == "ok" else None

    def _mirror(self, row):
        if not self.jsonl: return
        try:
            self.jsonl.parent.mkdir(parents=True, exist_ok=True)
            with open(self.jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps({k: row.get(k) for k in COLS}, ensure_ascii=False) + "\n")
        except Exception as e: log.warning("world jsonl mirror failed: %s", type(e).__name__)

    # ---------- read ----------
    def events(self, after_seq=0, since_ts=None, types=None, limit=None):
        q, a = "select " + ",".join(COLS) + " from world_events where seq>?", [after_seq]
        if since_ts is not None: q += " and ts>=?"; a.append(since_ts)
        if types: q += " and type in (" + ",".join("?" * len(types)) + ")"; a.extend(types)
        q += " order by seq"
        if limit: q += " limit ?"; a.append(int(limit))
        with self.lock: rows = self.db.execute(q, a).fetchall()
        out = []
        for r in rows:
            d = dict(zip(COLS, r)); d["payload"] = json.loads(d["payload"] or "{}"); d["causal_parents"] = json.loads(d["causal_parents"] or "[]")
            out.append(d)
        return out

    def last(self, n=50):
        with self.lock: m = self.db.execute("select coalesce(max(seq),0) from world_events").fetchone()[0]
        return self.events(after_seq=max(0, m - int(n)))

    def by_dedupe(self, key):
        with self.lock: r = self.db.execute("select seq from world_events where dedupe_key=?", (key,)).fetchone()
        return self.events(after_seq=r[0] - 1, limit=1)[0] if r else None

    def count(self):
        with self.lock: return self.db.execute("select count(*) from world_events").fetchone()[0]

    def close(self):
        with self.lock: self.db.close()
