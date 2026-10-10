import asyncio, random
from oku_slack.wheel import config, engine, server, store

def test_origin_policy():
    ok = server.origin_ok
    assert ok(None) and ok("https://itzkore.cz") and ok("https://www.itzkore.cz/") and ok("http://127.0.0.1:8797") and ok("http://localhost:5173")
    assert not ok("https://evil.example") and not ok("https://itzkore.cz.evil.example") and not ok("http://127.0.0.1.evil:80")

def test_rate_limit_window():
    t = [0.0]; rl = server.RateLimit(n=3, window=2.0, clock=lambda: t[0])
    assert [rl.allow() for _ in range(4)] == [True, True, True, False]
    t[0] = 2.5; assert rl.allow()

def test_local_secrets_override(tmp_path, monkeypatch):
    loc = tmp_path / "players.local.toml"
    loc.write_text('[players.kore]\nroom_key = "real-key-123"\n', encoding="utf-8")
    monkeypatch.setenv("OKU_WHEEL_LOCAL", str(loc))
    cfg = config.load(); assert cfg["players"]["kore"]["room_key"] == "real-key-123" and cfg["players"]["icik"]["name"] == "ICIK"

def test_ws_origin_and_rate_limit_live():
    from aiohttp.test_utils import TestServer, TestClient
    from aiohttp import WSServerHandshakeError
    async def run():
        e = engine.Engine(config.load(), store.Store(), rng=random.Random(1))
        c = TestClient(TestServer(server.make_app(e, run_loop=False))); await c.start_server()
        try:
            try:
                await c.ws_connect("/ws?p=kore&k=kore-local", headers={"Origin": "https://evil.example"}); assert False
            except WSServerHandshakeError as err: assert err.status == 403
            p = e.cfg["players"]["kore"]["room_key"]
            w = await c.ws_connect(f"/ws?p=kore&k={p}", headers={"Origin": "https://itzkore.cz"}); await w.receive_json()
            for i in range(12): await w.send_json({"type": "chat", "text": f"m{i}"})
            got = [await w.receive_json() for _ in range(12)]
            assert sum(1 for g in got if g.get("code") == "rate_limited") >= 3
            r = await c.get("/config.js"); assert "OKU_WS_URL" in await r.text()
        finally: await c.close()
    asyncio.run(run())
