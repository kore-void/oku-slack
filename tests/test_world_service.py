"""oku_world service: loopback-only HTTP (127.0.0.1 peer, no proxy/tunnel headers), routes, config defaults."""
import http.client, json, threading
import pytest
from oku_slack.world import log as wlog, service

NOW = 1_800_000_000.0

@pytest.fixture
def svc(tmp_path):
    (tmp_path / "config.toml").write_text("[world]\ndry_run = true\n", encoding="utf-8")
    s = service.WorldService(config_path=tmp_path / "config.toml", logs_dir=tmp_path / "logs", source_logs=tmp_path / "src", clock=lambda: NOW, ctx={})
    s.startup(); return s

def serve(svc, server_cls=None):
    srv = service.make_server(svc, "127.0.0.1", 0) if server_cls is None else server_cls(("127.0.0.1", 0), service.make_handler(svc))
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True); t.start()
    return srv

def call(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    h = dict(headers or {}); data = None
    if body is not None: data = json.dumps(body).encode(); h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h); r = c.getresponse(); raw = r.read(); c.close()
    return r.status, (json.loads(raw) if raw else None)

def test_loopback_ok_unit():
    assert service.loopback_ok("127.0.0.1", {}) and service.loopback_ok("127.0.0.1", {"Host": "x", "User-Agent": "y"})
    for peer in ("::1", "192.168.1.5", "10.0.0.1", "127.0.0.2", "", None): assert not service.loopback_ok(peer, {}), peer
    for h in service.PROXY_HEADERS: assert not service.loopback_ok("127.0.0.1", {h: "1"}), h
    assert not service.loopback_ok("127.0.0.1", {"x-forwarded-for": "1.2.3.4"}) and not service.loopback_ok("127.0.0.1", {"cf-ray": "a"})

def test_routes_and_proxy_headers_refused(svc):
    srv = serve(svc)
    try:
        st, j = call(srv, "GET", "/healthz"); assert st == 200 and j["ok"] and j["service"] == "oku_world" and j["dry_run"] is True
        st, j = call(srv, "GET", "/api/world?events=5&player=kore"); assert st == 200 and j["resources"]["dotace"] == 5000 and j["events"][-1]["type"] == "world.started"
        assert j["dry_run"] is True and "porada" in j and "memory" in j
        st, j = call(srv, "GET", "/api/budget"); assert st == 200 and j["limits"]["posts_per_day"] == 6 and j["porada_schedule"] == "mon-fri 10:00"
        for h in ({"X-Forwarded-For": "203.0.113.9"}, {"CF-Connecting-IP": "203.0.113.9"}, {"cf-ray": "abc"}, {"Forwarded": "for=1.2.3.4"}, {"Via": "1.1 proxy"}):
            assert call(srv, "GET", "/healthz", headers=h)[0] == 403, h
            assert call(srv, "GET", "/api/world", headers=h)[0] == 403, h
            assert call(srv, "POST", "/api/events", {"type": "x.y", "source": "manual"}, headers=h)[0] == 403, h
        assert call(srv, "GET", "/nope")[0] == 404 and call(srv, "DELETE", "/api/world")[0] == 405
    finally: srv.shutdown(); srv.server_close()

def test_post_events_validates_and_dedupes(svc):
    srv = serve(svc)
    try:
        d = {"type": "manual.podnet", "source": "manual", "actor": "kore", "payload": {"url": "https://example.org"}, "dedupe_key": "manual:1", "ts": NOW}
        st, j = call(srv, "POST", "/api/events", [d, d, dict(d, payload={"text": "x"}, dedupe_key="manual:2"), dict(d, source="wheel", dedupe_key="w:1")])
        assert st == 200 and [r["status"] for r in j["results"]] == ["ok", "dup", "invalid", "invalid"]
        assert j["results"][2]["reason"] == "free_text_key:text" and j["results"][3]["reason"] == "source_reserved"
        assert call(srv, "POST", "/api/events", {"a": 1}, headers={"Content-Type": "text/plain"})[0] == 415
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("POST", "/api/events", body=b"{bad", headers={"Content-Type": "application/json"}); assert c.getresponse().status == 400; c.close()
        assert svc.diary.by_dedupe("manual:1")["actor"] == "kore"
    finally: srv.shutdown(); srv.server_close()

def test_non_loopback_peer_refused(svc):
    class Remote(service.ThreadingHTTPServer):
        daemon_threads = True
        def get_request(self):
            sock, _ = super().get_request(); return sock, ("192.168.1.5", 50000)
    srv = serve(svc, Remote)
    try: assert call(srv, "GET", "/healthz")[0] == 403 and call(srv, "GET", "/api/world")[0] == 403
    finally: srv.shutdown(); srv.server_close()

def test_binds_loopback_only(svc):
    for host in ("0.0.0.0", "::", "192.168.1.5", "localhost"):
        with pytest.raises(ValueError): service.make_server(svc, host, 0)

def test_config_defaults_from_repo_config_toml():
    c = service.load_world_cfg(service.ROOT / "config.toml")
    # live since 2026-10-09 (Kore's go-live); posts/day 6 -> 16 when news reactions were added
    assert c["dry_run"] is False and c["porada_schedule"] == "mon-fri 10:00" and c["porada_channel"] == "C0C6W8E6NP9"
    assert (c["posts_per_day"], c["per_channel_gap_h"], c["chatter_threads_per_day"], c["chatter_max_turns"], c["llm_calls_per_day"]) == (16, 3, 2, 4, 40)
    assert c["quiet_hours"] == ["22:00", "08:00"] and c["timezone"] == "Europe/Prague" and c["kill_switch"] is False
    m = service.load_world_cfg(service.ROOT / "missing.toml"); assert m["dry_run"] is True and m["port"] == 8798

def test_service_runs_without_wheel_and_ingests_usage(svc):
    (svc.source_logs).mkdir(parents=True, exist_ok=True)
    (svc.source_logs / "usage.jsonl").write_text(json.dumps({"ts": "2027-01-15T09:00:00+01:00", "persona": "babis", "kind": "solo"}) + "\n", encoding="utf-8")
    r = svc.tick(); assert r["usage"]["ok"] == 1 and r["wheel"]["ok"] == 0 and r["wheel_legacy"]["ok"] == 0
    assert svc.world.state()["bridge"]["calls"] == 1 and svc.health()["tick_errors"] == 0

def test_legacy_wheel_world_jsonl_is_retired_not_mixed(tmp_path):
    legacy = {"seq": 1, "id": "we_0001", "ts": 1.0, "type": "wheel.spin", "source": "backfill"}
    (tmp_path / "world.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    moved = service.retire_legacy_jsonl(tmp_path)
    assert moved and moved.exists() and not (tmp_path / "world.jsonl").exists()
    d = wlog.Diary(tmp_path / "world.sqlite3", jsonl=tmp_path / "world.jsonl", clock=lambda: NOW, version="test")
    d.ingest({"type": "x.y", "source": "manual"}); d.close()
    assert service.retire_legacy_jsonl(tmp_path) is None and (tmp_path / "world.jsonl").exists()   # ours stays
