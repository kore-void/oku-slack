"""P-001 World v0: append-only world_events diary (engine/Slack/room hooks, JSONL mirror, backfill), read-only
projection (rebuild == incremental), loopback-only /api/world (D12/B13). Fakes only; no network."""
import asyncio, json, random
from oku_slack.wheel import config, engine, server, store, slack_adapter as SA
from oku_slack.world import state as wstate

class Clock:
    def __init__(self, t=6_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

def mk(jsonl=None, **s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3), world_jsonl=jsonl), st, clk

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"
def T(x): return json.dumps(x, ensure_ascii=False)
def img(call): return [b for b in call[1]["blocks"] if b["type"] == "image"]
def types(e, **filt): return [ev["type"] for ev in e.wlog.events() if all(ev.get(k) == v for k, v in filt.items())]

# ---------------- world diary ----------------
def test_diary_full_flow_engine_events_bets_commands_show(tmp_path):
    e, st, clk = mk(jsonl=tmp_path / "world.jsonl", bet_window_s=30)
    SA.handle_command(e, KORE, "toc")                               # bets.open (slack)
    SA.handle_command(e, ICIK, "sazka porada 100")                 # bet.placed (slack)
    clk.adv(31); out = dict(e.tick()); ev = out["spin"]            # wheel.spin
    clk.adv(7); e.tick()                                           # wheel.reveal + bet.settled
    SA.handle_modal(e, KORE, ev["id"], ev["code"])                 # wheel.confirm + wheel.ready (slack)
    clk.t = ev["start_at"]; e.tick()                               # wheel.live
    e.vote("icik", 0)
    sh = e.active_event()["show"]
    clk.t = sh["catch"]["at"] + 0.1; e.tick(); e.catch("kore")    # show.catch
    clk.t = sh["quiz"]["at"] + 0.1; e.tick(); e.quiz("kore", 0)   # show.quiz
    e.use_command("icik")                                          # command.used
    clk.t = e.active_event()["end_at"]; e.tick()                   # wheel.done
    ts = types(e)
    for must in ("bets.open", "bet.placed", "wheel.spin", "wheel.reveal", "bet.settled", "wheel.confirm", "wheel.ready",
                 "wheel.alarm", "wheel.live", "show.vote", "show.catch", "show.quiz", "command.used", "wheel.done", "ui.kolo"):
        assert must in ts, must
    rows = e.wlog.events(); spin = [r for r in rows if r["type"] == "wheel.spin"][0]
    assert rows[0]["id"] == "we_0001" and [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["causal_parents"] == [spin["id"]] for r in rows if r["subject"] == spin["subject"] and r is not spin)
    conf = [r for r in rows if r["type"] == "wheel.confirm"][0]
    assert conf["source"] == "slack" and conf["actor"] == "kore" and conf["payload"]["how"] == "code" and conf["payload"]["on_time"]
    assert [r for r in rows if r["type"] == "wheel.live"][0]["source"] == "engine"
    assert {r["regime"] for r in rows} == {"A_scarce"} and all(r["content_version"] for r in rows)
    lines = (tmp_path / "world.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows) and json.loads(lines[0])["id"] == "we_0001"

def test_diary_reactions_room_source_and_no_free_text():
    e, st, clk = mk(); st.kv_set("panel_ts", "100.1"); ev = e.spin("kore", force="porada", lead_s=0)
    e.confirm_code(ev["id"], "kore", ev["code"]); clk.adv(7); e.tick()
    r = {"item": {"type": "message", "channel": "C0C6W8E6NP9", "ts": "100.1"}, "user": ICIK, "reaction": "fire"}
    assert SA.on_reaction(e, None, None, "C0C6W8E6NP9", r, +1)
    x = [q for q in e.wlog.events() if q["type"] == "reaction"][-1]
    assert x["actor"] == "icik" and x["source"] == "slack" and x["payload"] == {"reaction": "fire", "delta": 1, "target": "panel", "hype": True}
    asyncio.run(server.Hub(e).handle("kore", {"type": "chat", "text": "tajná zpráva"}))
    c = [q for q in e.wlog.events() if q["type"] == "room.chat"][-1]
    assert c["source"] == "room" and c["actor"] == "kore" and "tajná" not in T(e.wlog.events())

def test_diary_never_breaks_the_wheel():
    e, st, clk = mk()
    class Boom:
        lock = st.lock
        @property
        def db(self): raise RuntimeError("disk full")
    e.wlog.store = Boom()
    ev = e.spin("kore", force="porada"); assert ev["state"] == "pending"   # recording failed silently

# ---------------- world projection ----------------
def _history(e, clk):
    ev = e.spin("kore", force="porada", lead_s=10); clk.adv(71); e.tick()          # expired, both missed
    e.use_command("icik")                                                           # steal: Kalousek takes the blame
    ev2 = e.spin("icik", force="kantyna", lead_s=0); e.confirm_code(ev2["id"], "icik", ev2["code"]); clk.adv(7); e.tick()
    clk.t = e.active_event()["end_at"]; e.tick()                                    # done
    return ev, ev2

def test_projection_blame_witnessed_acts_stats_and_seeds():
    e, st, clk = mk(); _history(e, clk); s = e.world.state()
    assert s["resources"] == {"dotace": 5000, "kampan": 35, "hranolky": 80, "lajky": 1200}   # no consequences in P-001
    assert s["blame"] == {"kore": 1, "icik": 1, "kalousek": 1}
    assert s["totals"]["spun"] == 2 and s["totals"]["expired"] == 1 and s["totals"]["live"] == 1 and s["totals"]["done"] == 1
    kore = s["players"]["kore"]; icik = s["players"]["icik"]
    assert kore["stats"]["missed"] == 1 and kore["stats"]["spins"] == 1 and icik["stats"]["confirms_on_time"] == 1
    assert kore["witnessed_acts"]["babis"][0]["act"].startswith("missed:porada:we_")
    assert icik["witnessed_acts"]["alenka"][0]["act"].startswith("confirmed:kantyna:")
    assert icik["witnessed_acts"]["kalousek"][0]["act"].startswith("command:Kalousek za to může:")
    assert s["personas"]["babis"]["expired"] == 1 and s["personas"]["alenka"]["done"] == 1
    assert [x["state"] for x in s["recent"]] == ["expired", "done"]

def test_projection_rebuild_from_scratch_equals_incremental():
    e, st, clk = mk(); e.spin("kore", force="porada", lead_s=10); clk.adv(71); e.tick()
    mid = e.world.state(); assert mid["as_of_seq"] > 0                               # cached
    e.use_command("icik"); ev = e.spin("icik", force="socky", lead_s=0); e.confirm_code(ev["id"], "kore", ev["code"]); clk.adv(7); e.tick()
    inc = json.loads(json.dumps(e.world.state()))
    assert inc == json.loads(json.dumps(e.world.rebuild())) == json.loads(json.dumps(wstate.project(e.wlog.events(), e.world.ctx)))

def test_world_delta_clamped_and_metrics_window():
    st0 = wstate.initial(); ev = lambda seq, t, ts, **pl: {"seq": seq, "id": f"we_{seq:04d}", "type": t, "ts": ts, "payload": pl, "source": "engine"}
    s = wstate.project([ev(1, "world.delta", 1, resource="kampan", delta=500), ev(2, "world.delta", 2, resource="dotace", delta=-200)])
    assert s["resources"]["kampan"] == 100 and s["resources"]["dotace"] == 4800 and st0["resources"]["kampan"] == 35
    m = wstate.metrics([ev(1, "wheel.spin", 10), ev(2, "wheel.spin", 1000), ev(3, "wheel.live", 1001)], since_ts=500)
    assert m["spun"] == 1 and m["live"] == 1 and m["live_rate"] == 1.0

def test_backfill_imports_pre_diary_history_once():
    e, st, clk = mk(bet_window_s=30); SA.handle_command(e, KORE, "toc"); e.bet("kore", "disko", 250)
    clk.adv(31); e.tick(); clk.adv(7); e.tick(); clk.adv(700); e.tick()          # spin, settle, expire
    with st.lock: st.db.execute("delete from world_events"); st.db.commit()       # = a DB from before P-001
    n = e.wlog.backfill(e.players()); assert n >= 4
    assert set(types(e)) >= {"wheel.spin", "wheel.expired", "bet.placed", "bet.settled"} and set(types(e, source="backfill")) == set(types(e))
    s = e.world.rebuild(); assert s["totals"]["spun"] == 1 and s["totals"]["expired"] == 1 and s["blame"] == {"icik": 1, "kore": 1}
    assert e.wlog.backfill(e.players()) == 0                                          # once

def test_snapshot_persisted_to_kv():
    e, st, clk = mk(); _history(e, clk)
    snap = e.world.snapshot(korun={"kore": 1, "icik": 2}); kv = json.loads(st.kv_get("world:snapshot"))
    assert kv == json.loads(json.dumps(snap)) and kv["metrics_7d"]["spun"] == 2 and kv["players"]["icik"]["korun"] == 2

# ---------------- D12 / B13: new world route is loopback-only ----------------
def test_api_world_loopback_only_state_stays_public():
    from aiohttp.test_utils import TestServer, TestClient
    async def run():
        e, st, clk = mk(); e.spin("kore", force="porada")
        c = TestClient(TestServer(server.make_app(e, run_loop=False))); await c.start_server()
        try:
            r = await c.get("/api/world?events=5"); assert r.status == 200
            j = await r.json(); assert j["resources"]["dotace"] == 5000 and j["events"][0]["type"] == "wheel.spin"
            for h in ({"CF-Connecting-IP": "203.0.113.9"}, {"X-Forwarded-For": "203.0.113.9"}, {"CF-Ray": "abc"}):
                assert (await c.get("/api/world", headers=h)).status == 403, h
            assert (await c.get("/api/state", headers={"CF-Connecting-IP": "203.0.113.9"})).status == 200
        finally: await c.close()
    asyncio.run(run())

def test_loopback_only_rejects_remote_peer():
    class R:
        def __init__(self, remote, headers=None): self.remote, self.headers = remote, headers or {}
    assert server.loopback_only(R("127.0.0.1")) and server.loopback_only(R("::1"))
    assert not server.loopback_only(R("192.168.1.5")) and not server.loopback_only(R("127.0.0.1", {"CF-Connecting-IP": "1.1.1.1"}))

def test_double_bet_use_is_recorded():
    e, st, clk = mk(bet_window_s=30); st.kv_set("double:kore", "1")
    e.open_bets("kore"); e.bet("kore", "porada", 50); clk.adv(31); e.tick(); clk.adv(7); e.tick()
    assert [r["actor"] for r in e.wlog.events() if r["type"] == "effect.double_used"] == ["kore"] and not st.kv_get("double:kore")

def test_expired_row_lists_missing_players_and_quorum():
    e, st, clk = mk(); e.spin("kore", force="kantyna", lead_s=10); clk.adv(10 + 61); e.tick()
    x = [r for r in e.wlog.events() if r["type"] == "wheel.expired"][0]
    assert x["payload"]["missing"] == ["kore", "icik"] and x["payload"]["confirmed"] == [] and x["payload"]["quorum"] == 1
