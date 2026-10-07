import asyncio, random, pytest
from oku_slack.wheel import config, engine, render, store, slack_adapter as SA

class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

@pytest.fixture
def env():
    clk = Clock(); cfg = config.load()
    return engine.Engine(cfg, store.Store(), clock=clk, rng=random.Random(7)), clk

def confirm_all(e, ev):
    for p in e.players(): e.confirm_code(ev["id"], p, ev["code"])

def test_config_players():
    cfg = config.load()
    assert set(cfg["players"]) == {"kore", "icik"} and cfg["players"]["kore"]["slack_id"] == "U0C6XAN3EG3"

def test_spin_deterministic_target_in_segment(env):
    e, _ = env; ev = e.spin("kore")
    assert render.segment_at(len(e.cfg["events"]), ev["target_angle"]) == ev["index"]
    assert e.cfg["events"][ev["index"]]["key"] == ev["key"] and len(ev["code"]) == 4

def test_single_active_event(env):
    e, _ = env; e.spin("kore")
    with pytest.raises(engine.WheelError) as x: e.spin("icik")
    assert x.value.code == "event_active"

def test_not_live_without_all_players(env):
    e, clk = env; ev = e.spin("kore", lead_s=60)
    e.confirm_code(ev["id"], "kore", ev["code"].lower())
    clk.adv(61); assert [k for k, _ in e.tick()] == ["alarm"] and e.active_event()["state"] == "pending"
    e.confirm_code(ev["id"], "icik", ev["code"])  # late confirmation -> starts on next tick
    assert [k for k, _ in e.tick()] == ["live"]

def test_bad_code(env):
    e, _ = env; ev = e.spin("kore")
    with pytest.raises(engine.WheelError) as x: e.confirm_code(ev["id"], "kore", "XXXX")
    assert x.value.code == "bad_code"

def test_hold_enforced_server_side(env):
    e, clk = env; ev = e.spin("kore")
    e.hold_start(ev["id"], "kore"); clk.adv(0.5)
    with pytest.raises(engine.WheelError) as x: e.hold_end(ev["id"], "kore")
    assert x.value.code == "hold_too_short"
    with pytest.raises(engine.WheelError): e.hold_end(ev["id"], "kore")  # no hold in progress
    e.hold_start(ev["id"], "kore"); clk.adv(2.1)
    assert e.hold_end(ev["id"], "kore")["confirmed"]["kore"]["how"].startswith("hold")

def test_alarm_once_5min_before_then_live_then_done(env):
    e, clk = env; ev = e.spin("kore"); confirm_all(e, ev)  # lead 600 s
    clk.adv(299); assert e.tick() == []
    clk.adv(1); assert [k for k, _ in e.tick()] == ["alarm"]
    clk.adv(10); assert e.tick() == []
    clk.adv(290); out = e.tick(); assert [k for k, _ in out] == ["live"]
    clk.adv(ev["duration_s"]); assert [k for k, _ in e.tick()] == ["done"]

def test_expires(env):
    e, clk = env; e.spin("kore")
    clk.adv(600 + 1800); kinds = [k for k, _ in e.tick()]
    assert "expired" in kinds and e.active_event() is None

def test_cooldown_30min_persisted(tmp_path):
    clk = Clock(); db = tmp_path / "w.db"; cfg = config.load()
    e = engine.Engine(cfg, store.Store(db), clock=clk)
    e.use_command("kore")
    with pytest.raises(engine.WheelError) as x: e.use_command("kore")
    assert x.value.code == "cooldown"
    e.use_command("icik")  # per-player
    e2 = engine.Engine(cfg, store.Store(db), clock=clk)  # restart keeps cooldown
    clk.adv(1799); assert e2.cooldown_left("kore") == pytest.approx(1)
    with pytest.raises(engine.WheelError): e2.use_command("kore")
    clk.adv(1); e2.use_command("kore")

def test_sequence_2min_server_timed(env):
    e, clk = env; q = e.use_command("kore")
    assert q["end_at"] - q["start_at"] == 120
    clk.adv(30); assert [k for k, _ in e.tick()] == ["seq_step"]
    clk.adv(89); e.tick(); clk.adv(1); assert ("seq_done" in [k for k, _ in e.tick()])
    assert e.snapshot()["sequences"] == []

def test_snapshot_hides_code_from_spectators(env):
    e, _ = env; e.spin("kore")
    assert "code" not in e.snapshot()["event"] and "code" in e.snapshot(viewer="kore")["event"]

def test_render_png_and_svg():
    segs = [{"key": x["key"], "color": x["color"]} for x in config.load()["events"]]
    b = render.png(segs, 123.4, title="Porada"); assert b[:8] == b"\x89PNG\r\n\x1a\n"
    assert render.svg(segs, 10).startswith("<svg") and render.segment_at(4, 95) == 1

def test_slack_command_flow(env):
    e, _ = env
    assert "Nejsi" in SA.handle_command(e, "UNOBODY", "")["text"]
    r = SA.handle_command(e, "U0C6XAN3EG3", ""); ev = r["spin"]; assert ev["code"] in r["text"]
    assert SA.handle_command(e, "U0C6XAN3EG3", "potvrdit")["open_modal"]
    assert SA.handle_modal(e, "U0C6XAN3EG3", ev["id"], "nope") == {"code": "Špatný kód."}
    assert SA.handle_modal(e, "U0C6XAN3EG3", ev["id"], ev["code"]) is None
    assert "2 minuty" in SA.handle_command(e, "U0C6XAN3EG3", "prikaz")["text"]
    assert "Nabíjí" in SA.handle_command(e, "U0C6XAN3EG3", "prikaz")["text"]
    assert "Kore" not in (SA.notification(e, "alarm", ev) or "") and "<@U0C6XAN3EG3>" in SA.notification(e, "alarm", ev)
    assert SA.modal_view(ev)["private_metadata"] == ev["id"]

def test_room_ws_sync():
    from aiohttp.test_utils import TestServer, TestClient
    from oku_slack.wheel import server
    async def run():
        clk = Clock(); e = engine.Engine(config.load(), store.Store(), clock=clk, rng=random.Random(1))
        app = server.make_app(e, run_loop=False); c = TestClient(TestServer(app)); await c.start_server()
        try:
            a = await c.ws_connect("/ws?p=kore&k=kore-local"); b = await c.ws_connect("/ws?p=icik&k=icik-local")
            spec = await c.ws_connect("/ws")
            for w in (a, b, spec): assert (await w.receive_json())["type"] == "hello"
            await spec.send_json({"type": "spin"}); assert (await spec.receive_json())["code"] == "spectator"
            await a.send_json({"type": "spin"})
            sa, sb, ss = [(await w.receive_json()) for w in (a, b, spec)]
            assert sa["payload"]["target_angle"] == sb["state"]["event"]["target_angle"] == ss["state"]["event"]["target_angle"]
            assert "code" not in ss["state"]["event"]
            ev = sa["payload"]
            await b.send_json({"type": "hold_start", "event_id": ev["id"]}); await asyncio.sleep(0.1); clk.adv(2.5)  # let server stamp start first
            await b.send_json({"type": "hold_end", "event_id": ev["id"]}); m = await b.receive_json()
            assert m["type"] == "confirm" and "icik" in m["state"]["event"]["confirmed"]
            await a.receive_json(); await spec.receive_json()
            await a.send_json({"type": "chat", "text": "Čau lidi"})
            assert (await b.receive_json())["payload"]["text"] == "Čau lidi"
            r = await c.get("/wheel.png"); assert r.status == 200 and (await r.read())[:4] == b"\x89PNG"
        finally: await c.close()
    asyncio.run(run())
