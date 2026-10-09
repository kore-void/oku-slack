"""Room server (aiohttp): authoritative tick loop + WebSocket hub + static room + optional Slack adapter.
Clients only render server state; they get `now` in each message to compute clock offset."""
import asyncio, collections, json, logging, logging.handlers, os, pathlib, subprocess, time
from aiohttp import web, WSMsgType
from . import config, engine, render, store
from ..world import log as world_log

log = logging.getLogger("oku_wheel")
STATIC = pathlib.Path(__file__).parent / "static"

DEFAULT_ORIGINS = ("https://itzkore.cz", "https://www.itzkore.cz", "http://127.0.0.1", "http://localhost")
# The named tunnel kolo-ws.itzkore.cz (cloudflared on this host) forwards EVERY path on :8797, and cloudflared
# connects from 127.0.0.1, so the peer address alone cannot tell local from tunnelled. New world routes are therefore
# loopback-only: local peer AND none of the proxy headers cloudflared/proxies add (decision D12, plan B13).
PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-IPCountry", "Cf-Warp-Tag-Id", "CF-Visitor", "X-Forwarded-For",
                 "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded")
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

def loopback_only(req):
    """True only for direct local requests (not via the tunnel or any proxy)."""
    if req.remote not in LOOPBACK: return False
    return not any(h in req.headers for h in PROXY_HEADERS)

def allowed_origins():
    env = os.environ.get("OKU_WHEEL_ORIGINS")
    return tuple(o.strip().rstrip("/") for o in env.split(",") if o.strip()) if env else DEFAULT_ORIGINS

def origin_ok(origin, allowed=None):
    """Browsers always send Origin; non-browser clients (tests, CLI) send none and are allowed.
    Localhost origins match any port."""
    if not origin: return True
    o = origin.rstrip("/")
    for a in allowed or allowed_origins():
        if o == a or (a in ("http://127.0.0.1", "http://localhost") and o.startswith(a + ":")): return True
    return False

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

def setup_logging(logs_dir, level=logging.INFO, max_bytes=5_000_000, backups=5):
    """Timestamped log lines to stderr (Heimdall captures it) AND a rotating logs/oku_wheel.log. Idempotent."""
    logs_dir = pathlib.Path(logs_dir); logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(); root.setLevel(level); fmt = logging.Formatter(LOG_FORMAT)
    path = str(logs_dir / "oku_wheel.log")
    if not any(getattr(h, "baseFilename", None) == os.path.abspath(path) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.setFormatter(fmt); root.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    return path

def git_commit(root=None):
    """Short sha of the running checkout: env OKU_COMMIT, else `git rev-parse`, else .git/HEAD, else 'unknown'."""
    if os.environ.get("OKU_COMMIT"): return os.environ["OKU_COMMIT"][:40]
    root = pathlib.Path(root or config.ROOT)
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip(): return r.stdout.strip()
    except (OSError, subprocess.SubprocessError): pass
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "): head = (root / ".git" / head[5:]).read_text(encoding="utf-8").strip()
        return head[:7] if head else "unknown"
    except OSError: return "unknown"

class RateLimit:
    """Sliding window per connection: at most `n` messages per `window` seconds."""
    def __init__(self, n=8, window=2.0, clock=time.monotonic):
        self.n, self.window, self.clock, self.ts = n, window, clock, collections.deque()
    def allow(self):
        now = self.clock()
        while self.ts and now - self.ts[0] > self.window: self.ts.popleft()
        if len(self.ts) >= self.n: return False
        self.ts.append(now); return True

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
        with world_log.source("room"):  # diary rows made by room actions are tagged source=room
            await self._handle(p, msg)

    async def _handle(self, p, msg):
        t, eid, e = msg.get("type"), msg.get("event_id"), self.eng
        if t == "spin": ev = e.spin(p, force=msg.get("force") or None); await self.broadcast("spin", ev); self._notify("spin", ev)
        elif t == "code": ev = e.confirm_code(eid, p, msg.get("code")); await self.broadcast("confirm", ev); self._notify("confirm", ev)
        elif t == "hold_start": e.hold_start(eid, p)
        elif t == "hold_end": ev = e.hold_end(eid, p); await self.broadcast("confirm", ev); self._notify("confirm", ev)
        elif t == "command": q = e.use_command(p); await self.broadcast("seq_start", q); self._notify("seq_start", q)
        elif t == "chat": m = e.chat(p, msg.get("text")); await self.broadcast("chat", m); self._notify("chat", m)
        else: raise engine.WheelError("bad_type", str(t))

    def _notify(self, kind, obj):
        if self.notify: asyncio.get_running_loop().run_in_executor(None, self.notify, kind, obj)

def make_app(eng, notify=None, period=0.5, run_loop=True, commit=None):
    hub = Hub(eng, notify)
    app = web.Application(); app["hub"] = hub; app["commit"] = commit or "unknown"

    async def ws_handler(req):
        if not origin_ok(req.headers.get("Origin")):
            log.warning("ws rejected: origin not allowed")
            return web.Response(status=403, text="origin not allowed")
        p = hub.auth(req.query.get("p"), req.query.get("k"))
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192); await ws.prepare(req)
        rl, strikes = RateLimit(), 0
        hub.clients[ws] = p  # p None = spectator (read-only, no code)
        await ws.send_json({"type": "hello", "you": p, "state": eng.snapshot(viewer=p)})
        async for m in ws:
            if m.type != WSMsgType.TEXT: continue
            if not rl.allow():
                strikes += 1
                if strikes > 30: await ws.close(code=1008, message=b"rate limit"); break
                await ws.send_json({"type": "error", "code": "rate_limited", "msg": "Pomaleji."}); continue
            try:
                if p is None: raise engine.WheelError("spectator", "Jen divák (chybí ?p=&k=).")
                async with hub.lock: await hub.handle(p, json.loads(m.data))
            except engine.WheelError as err:
                await ws.send_json({"type": "error", "code": err.code, "msg": str(err)})
            except (ValueError, KeyError) as err:
                await ws.send_json({"type": "error", "code": "bad_request", "msg": type(err).__name__})
        hub.clients.pop(ws, None)
        return ws

    async def state(req):  # public on purpose (room feed, D12); `commit` = running git sha (short)
        return web.json_response(dict(eng.snapshot(), commit=app["commit"]))
    async def world(req):  # loopback-only (D12): never through the tunnel
        if not loopback_only(req): return web.Response(status=403, text="forbidden")
        snap = eng.world.snapshot(korun={k: eng.eco.balance(k) for k in eng.cfg["players"]})
        if req.query.get("events"):
            n = max(1, min(500, int(req.query.get("events") or 50) if str(req.query.get("events")).isdigit() else 50))
            snap["events"] = eng.wlog.events(after_seq=max(0, snap["as_of_seq"] - n))
        return web.json_response(snap)
    async def wheel_png(req):
        e = eng.active_event()
        return web.Response(body=render.png(eng.snapshot()["wheel"], e["target_angle"] if e else 0), content_type="image/png")
    async def poster(req): return web.Response(body=render.titanic_poster(eng.cfg["scripts"].get("titanic")), content_type="image/png")
    async def index(req): return web.FileResponse(STATIC / "room.html")
    async def config_js(req):  # local server: same-host WS; the static deploy ships its own config.js
        return web.Response(text="window.OKU_WS_URL = null;\n", content_type="application/javascript")

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/config.js", config_js)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/world", world)
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
    path = setup_logging(config.ROOT / "logs")
    commit = git_commit()
    log.info("oku_wheel starting: commit=%s log=%s", commit, path)
    cfg = config.load()
    db = pathlib.Path(os.environ.get("OKU_WHEEL_DB") or config.ROOT / "logs" / "wheel.sqlite3"); db.parent.mkdir(exist_ok=True)
    eng = engine.Engine(cfg, store.Store(db), world_jsonl=os.environ.get("OKU_WORLD_JSONL") or db.parent / "world.jsonl")
    eng.wlog.backfill(eng.players())  # one-time import of pre-diary history (no-op once the diary has rows)
    notify = None
    if os.environ.get("OKU_WHEEL_SLACK") == "1":
        from . import slack_adapter
        notify = slack_adapter.start(eng)  # Socket Mode with the dedicated OKÚ Kolo app tokens; registers /kolo
    host, port = os.environ.get("OKU_WHEEL_HOST", "127.0.0.1"), int(os.environ.get("OKU_WHEEL_PORT", "8797"))
    web.run_app(make_app(eng, notify, commit=commit), host=host, port=port)

if __name__ == "__main__": main()
