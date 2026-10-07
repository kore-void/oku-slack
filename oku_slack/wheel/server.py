"""Room server (aiohttp): authoritative tick loop + WebSocket hub + static room + optional Slack adapter.
Clients only render server state; they get `now` in each message to compute clock offset."""
import asyncio, json, logging, os, pathlib, time
from aiohttp import web, WSMsgType
from . import config, engine, render, store

log = logging.getLogger("oku_wheel")
STATIC = pathlib.Path(__file__).parent / "static"

class Hub:
    def __init__(self, eng, notify=None):
        self.eng, self.clients, self.notify = eng, {}, notify  # ws -> player
        self.lock = asyncio.Lock()

    async def broadcast(self, kind, payload=None):
        dead = []
        for ws, p in list(self.clients.items()):
            try: await ws.send_json({"type": kind, "payload": payload, "state": self.eng.snapshot(viewer=p)})
            except Exception: dead.append(ws)
        for ws in dead: self.clients.pop(ws, None)

    async def tick_once(self):
        for kind, obj in self.eng.tick():
            await self.broadcast(kind, obj)
            if self.notify:
                try: await asyncio.to_thread(self.notify, kind, obj)
                except Exception as e: log.warning("notify %s failed: %s", kind, type(e).__name__)

    async def loop(self, period=0.5):
        while True:
            try: await self.tick_once()
            except Exception as e: log.exception("tick failed: %s", e)
            await asyncio.sleep(period)

    def auth(self, p, k):
        pl = self.eng.cfg["players"].get(p)
        return p if pl and k and k == pl.get("room_key") else None

    async def handle(self, p, msg):
        t, eid, e = msg.get("type"), msg.get("event_id"), self.eng
        if t == "spin": ev = e.spin(p, force=msg.get("force") or None); await self.broadcast("spin", ev); self._notify("spin", ev)
        elif t == "code": await self.broadcast("confirm", e.confirm_code(eid, p, msg.get("code")))
        elif t == "hold_start": e.hold_start(eid, p)
        elif t == "hold_end": await self.broadcast("confirm", e.hold_end(eid, p))
        elif t == "command": q = e.use_command(p); await self.broadcast("seq_start", q); self._notify("seq_start", q)
        elif t == "chat": await self.broadcast("chat", e.chat(p, msg.get("text")))
        else: raise engine.WheelError("bad_type", str(t))

    def _notify(self, kind, obj):
        if self.notify: asyncio.get_running_loop().run_in_executor(None, self.notify, kind, obj)

def make_app(eng, notify=None, period=0.5, run_loop=True):
    hub = Hub(eng, notify)
    app = web.Application(); app["hub"] = hub

    async def ws_handler(req):
        p = hub.auth(req.query.get("p"), req.query.get("k"))
        ws = web.WebSocketResponse(heartbeat=20); await ws.prepare(req)
        hub.clients[ws] = p  # p None = spectator (read-only, no code)
        await ws.send_json({"type": "hello", "you": p, "state": eng.snapshot(viewer=p)})
        async for m in ws:
            if m.type != WSMsgType.TEXT: continue
            try:
                if p is None: raise engine.WheelError("spectator", "Jen divák (chybí ?p=&k=).")
                async with hub.lock: await hub.handle(p, json.loads(m.data))
            except engine.WheelError as err:
                await ws.send_json({"type": "error", "code": err.code, "msg": str(err)})
            except (ValueError, KeyError) as err:
                await ws.send_json({"type": "error", "code": "bad_request", "msg": type(err).__name__})
        hub.clients.pop(ws, None)
        return ws

    async def state(req): return web.json_response(eng.snapshot())
    async def wheel_png(req):
        e = eng.active_event()
        return web.Response(body=render.png(eng.snapshot()["wheel"], e["target_angle"] if e else 0), content_type="image/png")
    async def poster(req): return web.Response(body=render.titanic_poster(eng.cfg["scripts"].get("titanic")), content_type="image/png")
    async def index(req): return web.FileResponse(STATIC / "room.html")

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", state)
    app.router.add_get("/wheel.png", wheel_png)
    app.router.add_get("/poster.png", poster)
    app.router.add_static("/static", STATIC)

    async def start_bg(app):
        if run_loop: app["tick"] = asyncio.create_task(hub.loop(period))
    async def stop_bg(app):
        t = app.get("tick")
        if t: t.cancel()
    app.on_startup.append(start_bg); app.on_cleanup.append(stop_bg)
    return app

def main():
    logging.basicConfig(level=logging.INFO)
    cfg = config.load()
    db = pathlib.Path(os.environ.get("OKU_WHEEL_DB") or config.ROOT / "logs" / "wheel.sqlite3"); db.parent.mkdir(exist_ok=True)
    eng = engine.Engine(cfg, store.Store(db))
    notify = None
    if os.environ.get("OKU_WHEEL_SLACK") == "1":
        from . import slack_adapter
        notify = slack_adapter.start(eng)  # Socket Mode with the Babiš app tokens; registers /kolo
    host, port = os.environ.get("OKU_WHEEL_HOST", "127.0.0.1"), int(os.environ.get("OKU_WHEEL_PORT", "8787"))
    web.run_app(make_app(eng, notify), host=host, port=port)

if __name__ == "__main__": main()
