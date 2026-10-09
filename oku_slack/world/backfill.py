"""One-time import of what happened before oku_world existed (plan 2.3, `b38a8a8` backfill moved here).
Reads logs/wheel.sqlite3 READ-ONLY:
  1. if the old in-wheel diary table `world_events` has rows (P-000 wheel code wrote it), those rows are imported
     verbatim (source=backfill, payload.via = original source, payload.legacy_id, dedupe_key wheel-legacy:<seq>);
  2. otherwise the history is reconstructed from the wheel tables `events`, `bets`, `sequences`.
Guarded by diary kv `world:backfilled`; re-running is a no-op. Never raises (returns -1 on failure).
Bridge history needs no backfill: the usage.jsonl tail starts at byte 0 on first start."""
import json, logging, pathlib, sqlite3
from .ingest import legacy_rows, legacy_draft, open_ro

log = logging.getLogger("oku_world.backfill")

def _table(con, name):
    if not con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone(): return []
    return [json.loads(r[0]) for r in con.execute(f"select data from {name} order by rowid")]

def reconstruct(events, bets, sequences, players=()):
    """Drafts (chronological) from the wheel tables, as the old WorldLog.backfill did. Pure."""
    items = []
    def add(ts, order, type, actor, subject, payload):
        items.append((ts or 0, order, {"type": type, "source": "backfill", "actor": actor, "subject": subject,
                                       "payload": dict(payload, backfill=True, via="engine"), "ts": ts}))
    for e in events:
        subj = f"event:{e['id']}"; base = {"key": e["key"], "round_id": e.get("round_id"), "host": e.get("host")}
        add(e["spin_at"], 0, "wheel.spin", e.get("spun_by"), subj, dict(base, forced=e.get("forced", False), legendary=e.get("legendary", False)))
        for p, c in (e.get("confirmed") or {}).items():
            add(c.get("at", e["spin_at"]), 1, "wheel.confirm", p, subj,
                dict(base, how=str(c.get("how", "")).split(":")[0], on_time=c.get("at", 0) <= e["start_at"]))
        st = e["state"]
        end = {"expired": e["start_at"], "done": e.get("end_at"), "live": e.get("live_at"), "vetoed": e["spin_at"]}.get(st)
        if end:
            missing = [p for p in players if p not in (e.get("confirmed") or {})]
            add(end, 2, f"wheel.{st}", e.get("vetoed_by") if st == "vetoed" else None, subj,
                dict(base, confirmed=sorted(e.get("confirmed") or {}), missing=missing))
    for b in bets:
        add(b["at"], 0, "bet.placed", b["player"], f"round:{b['round_id']}", {"key": b["key"], "amount": b["amount"], "odds": b["odds"]})
        if b["state"] in ("won", "lost", "refunded"):
            add(b["at"] + 1e-3, 3, "bet.settled" if b["state"] != "refunded" else "bet.refunded", b["player"], f"round:{b['round_id']}",
                {"key": b["key"], "amount": b["amount"], "state": b["state"], "payout": b.get("payout")})
    for q in sequences:
        add(q["start_at"], 0, "command.used", q["player"], f"seq:{q['id']}",
            {"label": q["label"], "effects": [{"effect": r.get("effect"), "ok": r.get("ok")} for r in q.get("effects", [])]})
    items.sort(key=lambda x: (x[0], x[1]))
    for i, (_, _, d) in enumerate(items): d["dedupe_key"] = f"backfill:wheel:{i}:{d['type']}:{d['subject']}:{d['actor']}"
    return [d for _, _, d in items]

def backfill(diary, wheel_db, players=()):
    """Returns the number of imported rows (0 when already done or nothing to import, -1 on error)."""
    if diary.kv_get("world:backfilled"): return 0
    try:
        p = pathlib.Path(wheel_db); n = 0; mode = "none"
        if p.exists():
            rows = legacy_rows(p, 0, limit=1_000_000)
            if rows:
                mode = "legacy_diary"
                for r in rows:
                    if diary.ingest(legacy_draft(r, "backfill"))[0] == "ok": n += 1
                diary.kv_set("offset:wheel_legacy", max(int(r["seq"]) for r in rows))
            else:
                con = open_ro(p)
                try: evs, bets, seqs = _table(con, "events"), _table(con, "bets"), _table(con, "sequences")
                finally: con.close()
                mode = "wheel_tables"
                for d in reconstruct(evs, bets, seqs, players):
                    if diary.ingest(d)[0] == "ok": n += 1
        diary.kv_set("world:backfilled", mode)
        diary.record("world.backfilled", None, None, {"rows": n, "mode": mode}, source="world", dedupe_key="world:backfilled")
        log.info("world backfill: %d rows (%s)", n, mode)
        return n
    except (sqlite3.Error, OSError, ValueError, KeyError) as e:
        log.warning("world backfill failed: %s", type(e).__name__); return -1
