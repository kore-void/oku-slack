"""World event sources + one-time backfill: usage.jsonl tail (bridge.reply), legacy wheel diary tail (read-only),
backfill from wheel.sqlite3 (old world_events table, or reconstructed from the wheel tables), projection of bridge
activity and persona memory."""
import json, random, sqlite3
from oku_slack.wheel import config, engine, store
from oku_slack.world import backfill, ingest, log as wlog, state as wstate

NOW = 1_800_000_000.0
def diary(tmp_path): return wlog.Diary(tmp_path / "world.sqlite3", clock=lambda: NOW, version="test")

def U(persona="babis", kind="solo", ch="C0C76PTMJ9Z", th="1.1", ts="2027-01-15T09:00:00+01:00"):
    return {"ts": ts, "persona": persona, "channel": ch, "thread_ts": th, "kind": kind, "backend": "gemini", "model": "gemini-2.5-flash",
            "key_index": 0, "latency_ms": 900, "in": 100, "out": 20, "think": 0, "total": 120, "est_usd": 0.0001}

def test_usage_tail_bridge_reply_offsets_partial_lines_and_restart(tmp_path):
    p = tmp_path / "usage.jsonl"; d = diary(tmp_path)
    p.write_text(json.dumps(U()) + "\n" + json.dumps(U("kalousek", "meeting")) + "\n" + '{"ts": "2027-01-15T09:0', encoding="utf-8")
    src = ingest.UsageTail(d, p)
    assert src.poll()["ok"] == 2
    with open(p, "a", encoding="utf-8") as f: f.write('1:00+01:00", "persona": "marty", "kind": "solo"}\nnot json\n')
    r = src.poll(); assert r["ok"] == 1 and r["invalid"] == 1
    rows = d.events(types=["bridge.reply"])
    assert [x["actor"] for x in rows] == ["babis", "kalousek", "marty"] and rows[0]["subject"] == "slack:C0C76PTMJ9Z:1.1"
    assert rows[0]["payload"]["kind"] == "solo" and rows[0]["payload"]["llm_calls"] == 1 and rows[2]["subject"] is None
    assert "text" not in json.dumps(rows)
    d.kv_set("offset:usage", 0)                                       # lost offset (or replay): dedupe keys absorb it
    assert ingest.UsageTail(d, p).poll()["ok"] == 0 and d.count() == 3
    p.write_text(json.dumps(U("peta")) + "\n", encoding="utf-8")       # truncated/rotated file restarts at 0
    assert src.poll()["ok"] == 1

def test_wheel_outbox_forces_source_and_fills_missing_keys(tmp_path):
    p = tmp_path / "wheel.jsonl"; d = diary(tmp_path)
    p.write_text(json.dumps({"type": "wheel.spin", "source": "bridge", "actor": "kore", "subject": "wheel:event:1", "payload": {"via": "slack"}, "ts": NOW}) + "\n"
                 + json.dumps({"type": "room.chat", "source": "wheel", "payload": {"text": "secret"}, "ts": NOW, "dedupe_key": "wheel:x"}) + "\n", encoding="utf-8")
    r = ingest.WheelOutbox(d, p).poll(); assert r["ok"] == 1 and r["invalid"] == 1
    row = d.events()[0]; assert row["source"] == "wheel" and row["dedupe_key"].startswith("wheel:off:")
    assert ingest.WheelOutbox(d, tmp_path / "missing.jsonl").poll() == {"ok": 0, "dup": 0, "invalid": 0, "error": 0}

def legacy_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript("""create table world_events(seq integer primary key autoincrement, id text unique, ts real not null, type text not null,
        source text, actor text, subject text, payload text, causal_parents text, regime text, content_version text, run_id text);
        create table kv(k text primary key, v text);""")
    for i, (t, src, actor, subj, pl) in enumerate(rows, 1):
        con.execute("insert into world_events(id,ts,type,source,actor,subject,payload,causal_parents,regime,content_version,run_id) values(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"we_{i:04d}", NOW - 1000 + i, t, src, actor, subj, json.dumps(pl), "[]", "A_scarce", "6dee5a3", "oku-world-1"))
    con.commit(); con.close()

ROWS = [("wheel.spin", "slack", "kore", "event:e1", {"key": "porada", "host": "babis"}),
        ("wheel.expired", "engine", None, "event:e1", {"key": "porada", "host": "babis", "missing": ["icik"], "confirmed": ["kore"]}),
        ("command.used", "room", "icik", None, {"label": "Kalousek za to může", "persona": "kalousek", "effects": [{"effect": "steal_points", "ok": True}]})]

def test_backfill_from_legacy_world_events_once_then_tail_new_rows(tmp_path):
    db = tmp_path / "wheel.sqlite3"; legacy_db(db, ROWS); d = diary(tmp_path)
    before = db.read_bytes()
    assert backfill.backfill(d, db, ["kore", "icik"]) == 3 and backfill.backfill(d, db) == 0
    assert db.read_bytes() == before                                                   # opened read-only: byte-identical
    rows = d.events(); assert [r["source"] for r in rows[:3]] == ["backfill"] * 3 and rows[0]["payload"]["via"] == "slack"
    assert rows[0]["payload"]["legacy_id"] == "we_0001" and rows[0]["dedupe_key"] == "wheel-legacy:1" and rows[3]["type"] == "world.backfilled"
    s = wstate.project(d.events()); assert s["blame"] == {"icik": 1, "kalousek": 1} and s["totals"]["human_actions"] == 2
    tail = ingest.LegacyWheelDb(d, db); assert tail.poll()["ok"] == 0                    # nothing new
    con = sqlite3.connect(db); con.execute("insert into world_events(id,ts,type,source,actor,subject,payload,causal_parents) values('we_0004',?,'wheel.spin','slack','icik','event:e2','{}','[]')", (NOW,)); con.commit(); con.close()
    assert tail.poll()["ok"] == 1 and d.events()[-1]["source"] == "wheel" and d.events()[-1]["dedupe_key"] == "wheel-legacy:4"

def test_backfill_reconstructs_from_wheel_tables_when_no_legacy_diary(tmp_path):
    clk = [NOW - 5000]; cfg = config.load(); dbp = tmp_path / "wheel.sqlite3"; st = store.Store(dbp)
    e = engine.Engine(cfg, st, clock=lambda: clk[0], rng=random.Random(3))
    e.open_bets("kore"); e.bet("kore", "disko", 250); clk[0] += 31; e.tick(); clk[0] += 7; e.tick(); clk[0] += 700; e.tick()
    st.db.close(); d = diary(tmp_path)
    n = backfill.backfill(d, dbp, ["kore", "icik"]); assert n >= 4
    types = {r["type"] for r in d.events() if r["source"] == "backfill"}
    assert types >= {"wheel.spin", "wheel.expired", "bet.placed", "bet.settled"}
    s = wstate.project(d.events()); assert s["totals"]["spun"] == 1 and s["totals"]["expired"] == 1 and s["blame"] == {"icik": 1, "kore": 1}
    assert d.kv_get("world:backfilled") == "wheel_tables" and backfill.backfill(d, dbp) == 0

def test_backfill_without_wheel_db_is_a_recorded_noop(tmp_path):
    d = diary(tmp_path); assert backfill.backfill(d, tmp_path / "none.sqlite3") == 0 and d.kv_get("world:backfilled") == "none"
    assert d.events()[-1]["type"] == "world.backfilled" and ingest.LegacyWheelDb(d, tmp_path / "none.sqlite3").poll()["ok"] == 0

def test_projection_bridge_actors_memory_porada_and_metrics(tmp_path):
    d = diary(tmp_path)
    for i, (p, k) in enumerate([("babis", "solo"), ("babis", "meeting"), ("kalousek", "meeting")]):
        d.ingest({"type": "bridge.reply", "source": "bridge", "actor": p, "payload": {"kind": k, "llm_calls": 1}, "ts": NOW - 100 + i})
    d.ingest({"type": "porada.dry_run", "source": "schedule", "actor": "babis", "subject": "schedule:porada:x", "payload": {"storylet": "PORADA", "topic": "Kampaň", "chair": "babis"}, "ts": NOW - 10})
    d.ingest({"type": "budget.denied", "source": "schedule", "actor": "babis", "payload": {"storylet": "PORADA", "reasons": ["quiet_hours"]}, "ts": NOW - 5})
    for i in range(25): d.ingest({"type": "bridge.reply", "source": "bridge", "actor": "marty", "payload": {"kind": "solo"}, "ts": NOW - 50 + i})
    w = wstate.World(d, {}, lambda: NOW); s = w.state()
    assert s["bridge"]["calls"] == 28 and s["bridge"]["by_persona"] == {"babis": 2, "kalousek": 1, "marty": 25} and s["bridge"]["by_kind"]["meeting"] == 2
    assert s["actors"]["babis"]["kind"] == "persona" and s["actors"]["babis"]["by_source"] == {"bridge": 2, "schedule": 2}
    assert "marty" not in s["memory"] and s["memory"]["babis"][-1]["type"] == "budget.denied"   # bridge.reply: counted, not remembered
    assert [m["type"] for m in s["memory"]["babis"]] == ["porada.dry_run", "budget.denied"] and s["memory"]["babis"][0]["topic"] == "Kampaň"
    assert s["porada"]["dry_run"] == 1 and s["porada"]["denied"] == 1 and s["porada"]["last"]["status"] == "quiet_hours"
    snap = w.snapshot(); m = snap["metrics_7d"]
    assert m["bridge_calls"] == 28 and m["dry_runs"] == 1 and m["budget_denied"] == 1 and snap["sources"]["bridge"]["events"] == 28
    assert snap["wheel_live"] is False and json.loads(json.dumps(snap)) == snap
