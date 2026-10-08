"""Append-only world diary: SQLite table `world_events` in the wheel DB (same connection + RLock as store.Store)
plus an optional JSONL mirror (logs/world.jsonl) for offline analysis.

Envelope (plan section 3.4): id, seq, ts, type, source, actor, subject, payload, causal_parents, regime,
content_version, run_id. Human free text is never copied in (types and ids only); room chat stays in `chat`.
Recording never raises into the caller: a diary failure is logged and the wheel keeps running."""
import contextlib, contextvars, json, logging, pathlib

log = logging.getLogger("oku_wheel.world")
SCHEMA = """
create table if not exists world_events(
  seq integer primary key autoincrement, id text unique, ts real not null, type text not null, source text,
  actor text, subject text, payload text, causal_parents text, regime text, content_version text, run_id text);
create index if not exists world_events_subject on world_events(subject);
create index if not exists world_events_ts on world_events(ts);
"""
COLS = ("seq", "id", "ts", "type", "source", "actor", "subject", "payload", "causal_parents", "regime", "content_version", "run_id")
_SOURCE = contextvars.ContextVar("oku_world_source", default=None)

@contextlib.contextmanager
def source(name):
    """Tag records made inside this block with an adapter source ('slack', 'room'); default is 'engine'."""
    tok = _SOURCE.set(name)
    try: yield
    finally: _SOURCE.reset(tok)

def current_source(default="engine"): return _SOURCE.get() or default

def git_version(root=None):
    """Short commit sha of the checkout (reads .git files, no subprocess). 'unknown' if unavailable."""
    try:
        g = pathlib.Path(root or pathlib.Path(__file__).resolve().parents[2]) / ".git"
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"): return head[:7]
        ref = head.split(" ", 1)[1].strip(); f = g / ref
        if f.exists(): return f.read_text().strip()[:7]
        for line in (g / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref): return line.split()[0][:7]
    except Exception: pass
    return "unknown"

class WorldLog:
    def __init__(self, store, clock, regime="A_scarce", run_id="oku-world-1", jsonl=None, version=None):
        self.store, self.clock, self.regime, self.run_id = store, clock, regime, run_id
        self.jsonl = pathlib.Path(jsonl) if jsonl else None
        self.version = version or git_version()
        with store.lock: store.db.executescript(SCHEMA); store.db.commit()

    def _first(self, subject):
        with self.store.lock:
            r = self.store.db.execute("select id from world_events where subject=? order by seq limit 1", (subject,)).fetchone()
        return r[0] if r else None

    def record(self, type, actor=None, subject=None, payload=None, source=None, parents=None, ts=None):
        """Append one event. Causal parent defaults to the first event about the same subject (e.g. the spin)."""
        try:
            src = source or current_source()
            with self.store.lock:
                if parents is None and subject:
                    p = self._first(subject); parents = [p] if p else []
                row = {"ts": float(self.clock() if ts is None else ts), "type": type, "source": src, "actor": actor,
                       "subject": subject, "payload": payload or {}, "causal_parents": parents or [],
                       "regime": self.regime, "content_version": self.version, "run_id": self.run_id}
                cur = self.store.db.execute(
                    "insert into world_events(ts,type,source,actor,subject,payload,causal_parents,regime,content_version,run_id) values(?,?,?,?,?,?,?,?,?,?)",
                    (row["ts"], type, src, actor, subject, json.dumps(row["payload"], ensure_ascii=False),
                     json.dumps(row["causal_parents"]), self.regime, self.version, self.run_id))
                seq = cur.lastrowid; row["seq"], row["id"] = seq, f"we_{seq:04d}"
                self.store.db.execute("update world_events set id=? where seq=?", (row["id"], seq)); self.store.db.commit()
            self._mirror(row)
            return row
        except Exception as e:
            log.warning("world record %s failed: %s", type, e.__class__.__name__); return None

    def _mirror(self, row):
        if not self.jsonl: return
        try:
            self.jsonl.parent.mkdir(parents=True, exist_ok=True)
            with open(self.jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps({k: row[k] for k in COLS if k != "run_id" or row.get(k)}, ensure_ascii=False) + "\n")
        except Exception as e: log.warning("world jsonl mirror failed: %s", e.__class__.__name__)

    def events(self, after_seq=0, since_ts=None):
        q, a = "select " + ",".join(COLS) + " from world_events where seq>?", [after_seq]
        if since_ts is not None: q += " and ts>=?"; a.append(since_ts)
        with self.store.lock: rows = self.store.db.execute(q + " order by seq", a).fetchall()
        out = []
        for r in rows:
            d = dict(zip(COLS, r)); d["payload"] = json.loads(d["payload"] or "{}"); d["causal_parents"] = json.loads(d["causal_parents"] or "[]")
            out.append(d)
        return out

    def count(self):
        with self.store.lock: return self.store.db.execute("select count(*) from world_events").fetchone()[0]

    def backfill(self, players=()):
        """One-time import of pre-diary history (events, bets, sequences tables) so the projection starts from what
        really happened. Runs only while the diary is empty; guarded by kv world:backfilled. Rows get source='backfill'."""
        if self.store.kv_get("world:backfilled") or self.count(): return 0
        n = 0
        def rec(*a, **k):
            nonlocal n
            if self.record(*a, source="backfill", **k): n += 1
        evs = self.store.events(); bets = self.store.bets()
        items = []  # (ts, order, fn) so the diary is chronological
        for e in evs:
            subj = f"event:{e['id']}"; base = {"key": e["key"], "round_id": e.get("round_id"), "backfill": True}
            items.append((e["spin_at"], 0, lambda e=e, subj=subj, base=base: rec("wheel.spin", e.get("spun_by"), subj,
                          dict(base, forced=e.get("forced", False), legendary=e.get("legendary", False), host=e.get("host")), ts=e["spin_at"])))
            for p, c in (e.get("confirmed") or {}).items():
                items.append((c.get("at", e["spin_at"]), 1, lambda e=e, p=p, c=c, subj=subj, base=base: rec("wheel.confirm", p, subj,
                              dict(base, how=str(c.get("how", "")).split(":")[0], on_time=c.get("at", 0) <= e["start_at"], host=e.get("host")), ts=c.get("at"))))
            st = e["state"]
            end = {"expired": e["start_at"], "done": e.get("end_at"), "live": e.get("live_at"), "vetoed": e["spin_at"]}.get(st)
            if end:
                missing = [p for p in players if p not in (e.get("confirmed") or {})]
                items.append((end, 2, lambda e=e, st=st, end=end, subj=subj, base=base, missing=missing: rec(f"wheel.{st}", e.get("vetoed_by") if st == "vetoed" else None, subj,
                              dict(base, confirmed=sorted(e.get("confirmed") or {}), missing=missing, host=e.get("host")), ts=end)))
        for b in bets:
            items.append((b["at"], 0, lambda b=b: rec("bet.placed", b["player"], f"round:{b['round_id']}", {"key": b["key"], "amount": b["amount"], "odds": b["odds"], "backfill": True}, ts=b["at"])))
            if b["state"] in ("won", "lost", "refunded"):
                items.append((b["at"] + 1e-3, 3, lambda b=b: rec("bet.settled" if b["state"] != "refunded" else "bet.refunded", b["player"], f"round:{b['round_id']}",
                              {"key": b["key"], "amount": b["amount"], "state": b["state"], "payout": b["payout"], "backfill": True}, ts=b["at"] + 1e-3)))
        for q in self.store.sequences():
            items.append((q["start_at"], 0, lambda q=q: rec("command.used", q["player"], f"seq:{q['id']}",
                          {"label": q["label"], "effects": [{"effect": r.get("effect"), "ok": r.get("ok")} for r in q.get("effects", [])], "backfill": True}, ts=q["start_at"])))
        for _, _, fn in sorted(items, key=lambda x: (x[0] or 0, x[1])): fn()
        self.store.kv_set("world:backfilled", "1")
        if n: log.info("world diary backfilled: %d events", n)
        return n
