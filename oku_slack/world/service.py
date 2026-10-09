"""oku_world: the world core as its own small service (plan 1.1). `python -m oku_slack.world.service`

- binds 127.0.0.1:8798 only and answers ONLY direct loopback clients: peer must be 127.0.0.1 and the request must
  carry none of the proxy/tunnel headers (CF-*, X-Forwarded-*, Forwarded, X-Real-IP, ...) -> otherwise 403;
- routes: GET /healthz, GET /api/world[?events=N&player=p], GET /api/budget, POST /api/events (diary drafts from
  local sources; validated and deduped like every other source);
- routes also: GET /api/world/brief[?persona=x] (persona memory briefs, P-003, used by the bridge for replies);
  GET /api/world/news[?date=YYYY-MM-DD&format=md] (OKÚ zpravodajství: today's matched headlines per person, news.py);
- loop (tick_s): podnet pollers (X API only with X_BEARER_TOKEN; pplx when enabled), then ingest wheel outbox +
  usage.jsonl + legacy wheel diary + podnet inbox (logs/inbox/podnety.jsonl), then the podnet reactor (P-004), the
  scheduler (porada), the chatter director (P-005) and the consequence engine (P-006, consequences.toml);
- dry_run = true by default: nothing is handed to the bridge; `porada.dry_run` / `chatter.dry_run` rows instead.

Paths: diary/logs dir = env OKU_WORLD_LOGS, else <checkout>/logs (world.sqlite3, world.jsonl, oku_world.log,
world.kill). Source logs (usage.jsonl, outbox/wheel.jsonl, outbox/meeting_*.jsonl, wheel.sqlite3) = env
OKU_WORLD_SOURCE_LOGS, else the diary logs dir. Config: [world] in config.toml (env OKU_CONFIG); env
OKU_WORLD_DRY_RUN=0/1 overrides dry_run. The config is re-read when the file changes (no restart needed)."""
import json, logging, logging.handlers, os, pathlib, threading, time, tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import backfill as backfill_mod, budget, chatter, consequences, ingest, log as wlog, memory, news, podnet, pplx, react, scheduler, state, tz, xsource

log = logging.getLogger("oku_world")
ROOT = pathlib.Path(__file__).resolve().parents[2]
PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-IPCountry", "Cf-Warp-Tag-Id", "CF-Visitor", "CF-Worker", "CDN-Loop",
                 "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Port", "X-Forwarded-Server",
                 "X-Real-IP", "X-Original-Forwarded-For", "Forwarded", "Via", "True-Client-IP", "X-Client-IP")
ALLOWED_PEERS = {"127.0.0.1"}
DEFAULTS = dict(budget.DEFAULTS, **scheduler.PORADA_DEFAULTS, **chatter.CHATTER_DEFAULTS, **react.DEFAULTS, regime="A_scarce",
                run_id="oku-world-1", port=8798, tick_s=15, legacy_wheel_tail=True, usage_tail=True, wheel_outbox=True,
                podnet_inbox=True, consequences_enabled=True, briefs_in_handoff=True)
RESERVED_SOURCES = ("wheel", "backfill", "bridge", "podnet", "consequence")   # each has its own tail/engine
NAMES = {"babis": "Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa", "kalousek": "Kalousek",
         "monika": "Monika", "kore": "Kore", "icik": "ICIK"}

def loopback_ok(peer, headers):
    """True only for a direct local client: peer 127.0.0.1 AND no proxy/tunnel header (any case)."""
    if peer not in ALLOWED_PEERS: return False
    present = {k.lower() for k in (headers.keys() if hasattr(headers, "keys") else headers)}
    return not any(h.lower() in present for h in PROXY_HEADERS)

def load_world_cfg(path):
    try:
        with open(path, "rb") as f: raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("config unreadable (%s): %s; using defaults", path, type(e).__name__); raw = {}
    c = dict(DEFAULTS); c.update(raw.get("world") or {})
    env = os.environ.get("OKU_WORLD_DRY_RUN")
    if env in ("0", "1", "true", "false"): c["dry_run"] = env in ("1", "true")
    c["dry_run"] = bool(c.get("dry_run", True))
    return c

def wheel_ctx():
    """Event hosts/titles/players from players.toml for the fold (optional; the world works without the wheel)."""
    try:
        import tomllib as t
        with open(os.environ.get("OKU_WHEEL_CONFIG") or ROOT / "players.toml", "rb") as f: raw = t.load(f)
        return state.ctx_from_cfg({"events": raw.get("events", []), "players": raw.get("players", {}), "settings": raw.get("settings", {})})
    except Exception as e:
        log.info("players.toml unavailable for the fold: %s", type(e).__name__); return {}

def retire_legacy_jsonl(logs_dir):
    """The P-000 wheel wrote logs/world.jsonl itself. If that file is there and it is not ours (our rows always carry
    dedupe_key) while no world.sqlite3 exists yet, rename it so the new mirror never mixes with it."""
    logs_dir = pathlib.Path(logs_dir); j, db = logs_dir / "world.jsonl", logs_dir / "world.sqlite3"
    if db.exists() or not j.exists(): return None
    try:
        with open(j, encoding="utf-8") as f: first = f.readline()
        if first.strip() and "dedupe_key" not in json.loads(first):
            dst = logs_dir / f"world.legacy-wheel.{int(time.time())}.jsonl"; os.replace(j, dst)
            log.info("legacy wheel world.jsonl moved to %s", dst.name); return dst
    except (OSError, ValueError) as e: log.warning("legacy world.jsonl check failed: %s", type(e).__name__)
    return None

class WorldService:
    def __init__(self, config_path=None, logs_dir=None, source_logs=None, wheel_db=None, clock=time.time, ctx=None, news_config=None):
        self.config_path = pathlib.Path(config_path or os.environ.get("OKU_CONFIG") or ROOT / "config.toml")
        self.logs_dir = pathlib.Path(logs_dir or os.environ.get("OKU_WORLD_LOGS") or ROOT / "logs")
        self.source_logs = pathlib.Path(source_logs or os.environ.get("OKU_WORLD_SOURCE_LOGS") or self.logs_dir)
        self.outbox_dir = self.source_logs / "outbox"
        self.wheel_db = pathlib.Path(wheel_db or os.environ.get("OKU_WORLD_WHEEL_DB") or self.source_logs / "wheel.sqlite3")
        self.clock = clock; self._cfg, self._cfg_mtime = None, None
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_moved = retire_legacy_jsonl(self.logs_dir) if self.logs_dir == self.source_logs else None
        c = self.settings()
        self.diary = wlog.Diary(self.logs_dir / "world.sqlite3", jsonl=self.logs_dir / "world.jsonl", clock=clock,
                                regime=c["regime"], run_id=c["run_id"])
        self.ctx = wheel_ctx() if ctx is None else ctx
        self.consequences = consequences.Engine(self)
        cq = self.consequences.cfg()   # seeds + thresholds (P-006); players.toml world_<k> settings still win
        state.THRESHOLDS.update(cq.get("thresholds") or {})
        seeds = dict(self.ctx.get("seeds") or {})
        self.ctx = dict(self.ctx, seeds=dict(cq.get("seeds") or {}, **{k: v for k, v in seeds.items() if v != state.RESOURCES[k][0]}))
        self.inbox_path = podnet.inbox_path(self.logs_dir)
        self.names = dict(NAMES, **{k: v.get("name", k) for k, v in (self.ctx.get("players") or {}).items()})
        self.titles = dict(self.ctx.get("titles") or {})
        self.world = state.World(self.diary, self.ctx, clock)
        self.sources = {"wheel": ingest.WheelOutbox(self.diary, self.outbox_dir / "wheel.jsonl"),
                        "usage": ingest.UsageTail(self.diary, self.source_logs / "usage.jsonl"),
                        "wheel_legacy": ingest.LegacyWheelDb(self.diary, self.wheel_db),
                        "podnet_inbox": podnet.InboxSource(self.diary, self.inbox_path, clock)}
        self.news_cfg = news.Config(news_config)
        self.pollers = {"x": xsource.XSource(self), "pplx": pplx.PplxSource(self), "news": news.NewsSource(self)}
        self.reactor = react.Reactor(self)
        self.news = news.NewsDirector(self)
        self.scheduler = scheduler.PoradaScheduler(self)
        self.chatter = chatter.ChatterDirector(self)
        self.started_at, self.last_tick, self.tick_errors, self.lock = clock(), None, 0, threading.RLock()
        self.last_ingest = {}

    def settings(self):
        """[world] config, re-read when config.toml changes (dry_run / budgets / kill switch apply without restart)."""
        try: m = self.config_path.stat().st_mtime
        except OSError: m = None
        if self._cfg is None or m != self._cfg_mtime:
            self._cfg, self._cfg_mtime = load_world_cfg(self.config_path), m
        return dict(self._cfg)

    def startup(self):
        n = backfill_mod.backfill(self.diary, self.wheel_db, players=list((self.ctx.get("players") or {}).keys()))
        c = self.settings()
        self.diary.record("world.started", None, None, {"dry_run": c["dry_run"], "version": self.diary.version, "backfilled": n,
                          "porada_schedule": c["porada_schedule"],
                          "chatter_schedule": c["chatter_schedule"] if c.get("chatter_enabled", True) else None}, source="world")
        log.info("oku_world ready: dry_run=%s diary=%s source_logs=%s backfill=%s", c["dry_run"], self.logs_dir, self.source_logs, n)
        return n

    def tick(self, now=None):
        now = self.clock() if now is None else now
        c = self.settings(); res = {}
        with self.lock:
            for name, pol in self.pollers.items():   # pollers only append to the podnet inbox (never post anywhere)
                try: res[name] = pol.poll(now)
                except Exception as e: self.tick_errors += 1; log.warning("poller %s failed: %s", name, type(e).__name__)
            for name, src in self.sources.items():
                if name == "wheel_legacy" and not c.get("legacy_wheel_tail", True): continue
                if name == "usage" and not c.get("usage_tail", True): continue
                if name == "wheel" and not c.get("wheel_outbox", True): continue
                if name == "podnet_inbox" and not c.get("podnet_inbox", True): continue
                try: res[name] = src.poll(now)
                except Exception as e: self.tick_errors += 1; log.warning("source %s failed: %s", name, type(e).__name__)
            try: res["podnet"] = [r["type"] for r in self.reactor.tick(now) if r]
            except Exception as e: self.tick_errors += 1; log.warning("podnet reactor failed: %s", type(e).__name__)
            try: res["news"] = [r["type"] for r in self.news.tick(now) if r]
            except Exception as e: self.tick_errors += 1; log.warning("news director failed: %s", type(e).__name__)
            if (res.get("podnet_inbox") or {}).get("ok") or res.get("news"): self.pollers["news"].write_digest(now)   # digest file
            try: res["scheduler"] = [r["type"] for r in self.scheduler.tick(now) if r]
            except Exception as e: self.tick_errors += 1; log.warning("scheduler failed: %s", type(e).__name__)
            try: res["chatter"] = [r["type"] for r in self.chatter.tick(now) if r]
            except Exception as e: self.tick_errors += 1; log.warning("chatter director failed: %s", type(e).__name__)
            try: res["consequences"] = len(self.consequences.tick(now))
            except Exception as e: self.tick_errors += 1; log.warning("consequences failed: %s", type(e).__name__)
            self.last_tick, self.last_ingest = now, res
        return res

    def news_settings(self):
        """news.toml (oku_slack/world/news.toml or env OKU_NEWS_CONFIG), re-read on change."""
        return self.news_cfg.get()

    def news_headline(self, persona=None, now=None, hours=18):
        """Freshest guard-passing headline about persona's real person (or anyone) for porada/chatter topics, or None."""
        try: return news.headline_for(self.diary, self.clock() if now is None else now, persona, hours)
        except Exception as e: log.info("news headline lookup failed: %s", type(e).__name__); return None

    def api_news(self, query):
        c = self.settings(); now = self.clock()
        date = query.get("date")
        if date and not __import__("re").match(r"^\d{4}-\d{2}-\d{2}$", date): return None
        dg = news.digest(self.diary, now, self.news_settings(), c.get("timezone", tz.PRAGUE), date)
        nc = self.news_settings()
        dg["budget"] = dict(self.news.usage(now, c.get("timezone", tz.PRAGUE)), reactions_per_day=nc["reactions_per_day"],
                            per_person_per_day=nc["per_person_per_day"])
        if query.get("format") == "md": dg["markdown"] = news.markdown(dg)
        return dg

    def budget_view(self, now=None):
        now = self.clock() if now is None else now; c = self.settings()
        rows = self.diary.events(since_ts=min(tz.day_start(now, c.get("timezone", tz.PRAGUE)), now - float(c["per_channel_gap_h"]) * 3600) - 1)
        kill = budget.killed(c, self.logs_dir, self.diary.kv_get, now)
        dec = budget.check("post", rows, now, c, channel=c["porada_channel"], llm=int(c["porada_llm_estimate"]),
                           wheel_live=state.wheel_live(self.world.state(), now), kill=kill)
        return {"dry_run": c["dry_run"], "kill": kill, "quiet": budget.is_quiet(now, c["quiet_hours"], c.get("timezone", tz.PRAGUE)),
                "limits": {k: c[k] for k in ("posts_per_day", "per_channel_gap_h", "chatter_threads_per_day", "chatter_max_turns", "llm_calls_per_day", "quiet_hours")},
                "usage": dec["usage"], "porada_now": {"ok": dec["ok"], "reasons": dec["reasons"]},
                "porada_schedule": c["porada_schedule"], "porada_channel": c["porada_channel"],
                "chatter_now": {k: v for k, v in budget.check("chatter", rows, now, c, wheel_live=state.wheel_live(self.world.state(), now),
                                                              kill=kill).items() if k in ("ok", "reasons")},
                "chatter_schedule": c["chatter_schedule"] if c.get("chatter_enabled", True) else None,
                "chatter_pending": bool(self.diary.kv_get("chatter:pending")),
                "news": dict(self.news.usage(now, c.get("timezone", tz.PRAGUE)), reactions_per_day=self.news_settings()["reactions_per_day"],
                             per_person_per_day=self.news_settings()["per_person_per_day"], channel_gap_h=self.news_settings()["channel_gap_h"],
                             pending=bool(self.diary.kv_get("news:pending")))}

    def health(self):
        c = self.settings()
        return {"ok": True, "service": "oku_world", "version": self.diary.version, "dry_run": c["dry_run"], "events": self.diary.count(),
                "last_tick": self.last_tick, "tick_errors": self.tick_errors, "ingest": self.diary.stats, "uptime_s": round(self.clock() - self.started_at),
                "podnet": {"inbox": str(self.inbox_path), "x": self.pollers["x"].state, "pplx": self.pollers["pplx"].state,
                           "pplx_last": self.pollers["pplx"].last},
                "consequences": {"applied": self.consequences.applied, "error": self.consequences.error},
                "news": {"rss": self.pollers["news"].state, "pplx": self.pollers["news"].pplx_state, "config_error": self.news_cfg.error,
                         "last_rss": self.pollers["news"].last_rss, "last_pplx": self.pollers["news"].last_pplx,
                         "feeds_ok": sum(1 for f in self.pollers["news"].feeds.values() if f.get("ok")), "feeds": len(self.pollers["news"].feeds)}}

    def briefs_for(self, personas):
        """{persona: memory brief} for hand-off lines (P-003); {} when disabled."""
        if not self.settings().get("briefs_in_handoff", True): return {}
        return memory.briefs(self.world.state(), personas, self.names, self.titles)

    def api_brief(self, query):
        st = self.world.state(); p = query.get("persona")
        if p:
            if p not in state.PERSONAS: return None
            b = memory.brief(st, p, self.names, self.titles)
            return {"persona": p, "brief": b, "chars": len(b), "as_of_seq": st["as_of_seq"]}
        return {"as_of_seq": st["as_of_seq"], "briefs": memory.briefs(st, state.PERSONAS, self.names, self.titles)}

    def api_world(self, query):
        snap = self.world.snapshot()
        n = query.get("events")
        if n:
            n = max(1, min(500, int(n))) if str(n).isdigit() else 50
            snap["events"] = self.diary.last(n)
        p = query.get("player")
        if p and p in snap.get("players", {}): snap["player"] = dict(snap["players"][p], id=p)
        snap["dry_run"] = self.settings()["dry_run"]
        return snap

    def post_events(self, body):
        drafts = body if isinstance(body, list) else [body]
        out = []
        for d in drafts[:100]:
            if isinstance(d, dict) and d.get("source") in RESERVED_SOURCES:
                out.append({"status": "invalid", "reason": "source_reserved"}); continue  # those have their own tails
            st, row = self.diary.ingest(d)
            out.append({"status": st, "id": row["id"]} if st == "ok" else {"status": st, "reason": row} if st == "invalid" else {"status": st})
        return out

def make_handler(svc):
    class H(BaseHTTPRequestHandler):
        server_version = "oku_world"; sys_version = ""
        def log_message(self, fmt, *a): log.debug("http %s", fmt % a)
        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(b)
        def _guard(self):
            if loopback_ok(self.client_address[0], self.headers): return True
            log.warning("refused %s %s from %s (non-loopback or proxied)", self.command, self.path.split("?")[0][:40], self.client_address[0])
            self._send(403, {"error": "forbidden"}); return False
        def _query(self):
            from urllib.parse import urlsplit, parse_qs
            u = urlsplit(self.path); return u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}
        def do_GET(self):
            if not self._guard(): return
            path, q = self._query()
            try:
                if path == "/healthz": return self._send(200, svc.health())
                if path == "/api/world": return self._send(200, svc.api_world(q))
                if path == "/api/budget": return self._send(200, svc.budget_view())
                if path == "/api/world/news":
                    n = svc.api_news(q)
                    return self._send(200, n) if n is not None else self._send(400, {"error": "bad_date"})
                if path == "/api/world/brief":
                    b = svc.api_brief(q)
                    return self._send(200, b) if b is not None else self._send(404, {"error": "unknown_persona"})
                return self._send(404, {"error": "not_found"})
            except Exception as e:
                log.warning("GET %s failed: %s", path, type(e).__name__); return self._send(500, {"error": "internal"})
        def do_POST(self):
            if not self._guard(): return
            path, _ = self._query()
            if path != "/api/events": return self._send(404, {"error": "not_found"})
            if "json" not in (self.headers.get("Content-Type") or ""): return self._send(415, {"error": "json_only"})
            try: n = int(self.headers.get("Content-Length") or 0)
            except ValueError: n = -1
            if n <= 0 or n > 256_000: return self._send(413, {"error": "bad_length"})
            try: body = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError): return self._send(400, {"error": "bad_json"})
            return self._send(200, {"results": svc.post_events(body)})
        def do_PUT(self): self._guard() and self._send(405, {"error": "method"})
        do_DELETE = do_PATCH = do_PUT
    return H

def make_server(svc, host="127.0.0.1", port=8798):
    if host not in ("127.0.0.1",): raise ValueError("oku_world binds loopback 127.0.0.1 only")
    srv = ThreadingHTTPServer((host, port), make_handler(svc)); srv.daemon_threads = True
    return srv

def setup_logging(logs_dir, level=logging.INFO):
    logs_dir = pathlib.Path(logs_dir); logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(); root.setLevel(level); fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    path = str(logs_dir / "oku_world.log")
    if not any(getattr(h, "baseFilename", None) == os.path.abspath(path) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"); fh.setFormatter(fmt); root.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler(); sh.setFormatter(fmt); root.addHandler(sh)
    return path

def run_loop(svc, stop):
    while not stop.is_set():
        try: svc.tick()
        except Exception as e: log.exception("tick crashed: %s", e)
        stop.wait(max(2.0, float(svc.settings().get("tick_s", 15))))

def main():
    svc_logs = pathlib.Path(os.environ.get("OKU_WORLD_LOGS") or ROOT / "logs")
    path = setup_logging(svc_logs)
    svc = WorldService()
    c = svc.settings()
    host, port = "127.0.0.1", int(os.environ.get("OKU_WORLD_PORT") or c.get("port", 8798))
    log.info("oku_world starting: version=%s log=%s port=%s dry_run=%s", svc.diary.version, path, port, c["dry_run"])
    svc.startup()
    srv = make_server(svc, host, port)
    stop = threading.Event()
    threading.Thread(target=run_loop, args=(svc, stop), daemon=True, name="world-loop").start()
    try: srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt: pass
    finally: stop.set(); srv.server_close()

if __name__ == "__main__": main()
