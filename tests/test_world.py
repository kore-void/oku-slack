"""P-001 wheel at the edge: the wheel only hands diary drafts to the world through logs/outbox/wheel.jsonl
(world_client), the oku_world service ingests them (source=wheel, payload.via, dedupe) and projects the state.
The wheel holds/renders no world state and no longer serves /api/world. Fakes only; no network."""
import asyncio, json, random
from oku_slack.wheel import config, engine, server, store, slack_adapter as SA
from oku_slack.world import ingest, log as wlog, state as wstate

class Clock:
    def __init__(self, t=1_800_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

def mk(outbox=None, **s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s); st = store.Store()
    return engine.Engine(cfg, st, clock=clk, rng=random.Random(3), world_outbox=outbox), st, clk

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"
def T(x): return json.dumps(x, ensure_ascii=False)
def types(e, **filt): return [ev["type"] for ev in e.wlog.events() if all(ev.get(k) == v for k, v in filt.items())]

def world_of(e, clk):
    """Feed the wheel's rows into a fresh world diary (as the oku_world WheelOutbox tail does)."""
    d = wlog.Diary(clock=clk, version="test")
    for r in e.wlog.events():
        st, _ = d.ingest({k: r.get(k) for k in ("type", "source", "actor", "subject", "payload", "ts", "dedupe_key")}); assert st == "ok", st
    return d, wstate.World(d, wstate.ctx_from_cfg(e.cfg), clk)

# ---------------- wheel -> outbox ----------------
def test_wheel_rows_go_to_the_outbox_and_the_world_ingests_them(tmp_path):
    ob = tmp_path / "outbox" / "wheel.jsonl"
    e, st, clk = mk(outbox=ob, bet_window_s=30)
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
    lines = [json.loads(x) for x in ob.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(e.wlog.events()) and all(x["source"] == "wheel" and x["dedupe_key"].startswith("wheel:") for x in lines)
    assert all((x["subject"] or "wheel:").startswith("wheel:") for x in lines)
    conf = [x for x in lines if x["type"] == "wheel.confirm"][0]
    assert conf["payload"]["via"] == "slack" and conf["actor"] == "kore" and conf["payload"]["how"] == "code" and conf["payload"]["on_time"]
    assert [x for x in lines if x["type"] == "wheel.live"][0]["payload"]["via"] == "engine"
    # the world side: tail -> diary (ids, causal parents) -> projection; a second poll is a no-op
    d = wlog.Diary(tmp_path / "w.sqlite3", jsonl=tmp_path / "world.jsonl", clock=clk, version="test")
    src = ingest.WheelOutbox(d, ob)
    r = src.poll(); assert r["ok"] == len(lines) and r["invalid"] == 0 and src.poll()["ok"] == 0
    rows = d.events(); spin = [x for x in rows if x["type"] == "wheel.spin"][0]
    assert rows[0]["id"] == "we_0001" and all(x["source"] == "wheel" for x in rows)
    assert all(x["causal_parents"] == [spin["id"]] for x in rows if x["subject"] == spin["subject"] and x is not spin)
    s = wstate.World(d, wstate.ctx_from_cfg(e.cfg), clk).state()
    assert s["totals"]["spun"] == 1 and s["totals"]["done"] == 1 and s["players"]["kore"]["stats"]["catches"] == 1
    assert s["totals"]["human_actions"] > 0 and s["sources"]["wheel"]["events"] == len(lines)

def test_reactions_room_via_and_no_free_text():
    e, st, clk = mk(); st.kv_set("panel_ts", "100.1"); ev = e.spin("kore", force="porada", lead_s=0)
    e.confirm_code(ev["id"], "kore", ev["code"]); clk.adv(7); e.tick()
    r = {"item": {"type": "message", "channel": "C0C6W8E6NP9", "ts": "100.1"}, "user": ICIK, "reaction": "fire"}
    assert SA.on_reaction(e, None, None, "C0C6W8E6NP9", r, +1)
    x = [q for q in e.wlog.events() if q["type"] == "reaction"][-1]
    assert x["actor"] == "icik" and x["payload"] == {"reaction": "fire", "delta": 1, "target": "panel", "hype": True, "via": "slack"}
    asyncio.run(server.Hub(e).handle("kore", {"type": "chat", "text": "tajná zpráva"}))
    c = [q for q in e.wlog.events() if q["type"] == "room.chat"][-1]
    assert c["payload"]["via"] == "room" and c["actor"] == "kore" and "tajná" not in T(e.wlog.events())

def test_outbox_failure_never_breaks_the_wheel(tmp_path):
    blocker = tmp_path / "file"; blocker.write_text("x")
    e, st, clk = mk(outbox=blocker / "sub" / "wheel.jsonl")       # parent is a file: every append fails
    ev = e.spin("kore", force="porada"); assert ev["state"] == "pending" and e.wlog.failures >= 1

def test_wheel_has_no_world_state_and_no_world_route():
    e, st, clk = mk()
    assert not hasattr(e, "world")
    from aiohttp.test_utils import TestServer, TestClient
    async def run():
        c = TestClient(TestServer(server.make_app(e, run_loop=False))); await c.start_server()
        try:
            assert (await c.get("/api/world")).status == 404
            assert (await c.get("/api/state")).status == 200
        finally: await c.close()
    asyncio.run(run())

# ---------------- projection over wheel rows (now in the world) ----------------
def _history(e, clk):
    ev = e.spin("kore", force="porada", lead_s=10); clk.adv(71); e.tick()          # expired, both missed
    e.use_command("icik")                                                           # steal: Kalousek takes the blame
    ev2 = e.spin("icik", force="kantyna", lead_s=0); e.confirm_code(ev2["id"], "icik", ev2["code"]); clk.adv(7); e.tick()
    clk.t = e.active_event()["end_at"]; e.tick()                                    # done
    return ev, ev2

def test_projection_blame_witnessed_acts_stats_and_seeds():
    e, st, clk = mk(); _history(e, clk); d, w = world_of(e, clk); s = w.state()
    assert s["resources"] == {"dotace": 5000, "kampan": 35, "hranolky": 80, "lajky": 1200}   # no consequences yet
    assert s["blame"] == {"kore": 1, "icik": 1, "kalousek": 1}
    assert s["totals"]["spun"] == 2 and s["totals"]["expired"] == 1 and s["totals"]["live"] == 1 and s["totals"]["done"] == 1
    kore = s["players"]["kore"]; icik = s["players"]["icik"]
    assert kore["stats"]["missed"] == 1 and kore["stats"]["spins"] == 1 and icik["stats"]["confirms_on_time"] == 1
    assert kore["witnessed_acts"]["babis"][0]["act"].startswith("missed:porada:we_")
    assert icik["witnessed_acts"]["alenka"][0]["act"].startswith("confirmed:kantyna:")
    assert icik["witnessed_acts"]["kalousek"][0]["act"].startswith("command:Kalousek za to může:")
    assert s["personas"]["babis"]["expired"] == 1 and s["personas"]["alenka"]["done"] == 1
    assert [x["state"] for x in s["recent"]] == ["expired", "done"]
    assert {m["type"] for m in s["memory"]["babis"]} >= {"wheel.spin", "wheel.expired"} and s["actors"]["kore"]["kind"] == "player"

def test_projection_rebuild_from_scratch_equals_incremental():
    e, st, clk = mk(); e.spin("kore", force="porada", lead_s=10); clk.adv(71); e.tick()
    d, w = world_of(e, clk); mid = w.state(); assert mid["as_of_seq"] > 0
    n0 = len(e.wlog.events())
    e.use_command("icik"); ev = e.spin("icik", force="socky", lead_s=0); e.confirm_code(ev["id"], "kore", ev["code"]); clk.adv(7); e.tick()
    for r in e.wlog.events()[n0:]: d.ingest({k: r.get(k) for k in ("type", "source", "actor", "subject", "payload", "ts", "dedupe_key")})
    inc = json.loads(json.dumps(w.state()))
    assert inc == json.loads(json.dumps(w.rebuild())) == json.loads(json.dumps(wstate.project(d.events(), w.ctx)))

def test_world_delta_clamped_and_metrics_window():
    st0 = wstate.initial(); ev = lambda seq, t, ts, **pl: {"seq": seq, "id": f"we_{seq:04d}", "type": t, "ts": ts, "payload": pl, "source": "wheel"}
    s = wstate.project([ev(1, "world.delta", 1, resource="kampan", delta=500), ev(2, "world.delta", 2, resource="dotace", delta=-200)])
    assert s["resources"]["kampan"] == 100 and s["resources"]["dotace"] == 4800 and st0["resources"]["kampan"] == 35
    m = wstate.metrics([ev(1, "wheel.spin", 10), ev(2, "wheel.spin", 1000), ev(3, "wheel.live", 1001)], since_ts=500)
    assert m["spun"] == 1 and m["live"] == 1 and m["live_rate"] == 1.0

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
