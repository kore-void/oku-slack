"""Wheel ops (P1): timestamped + rotating log file, running git commit at startup and in /api/state."""
import asyncio, logging, random, re, subprocess
from oku_slack.wheel import config, engine, server, store

def test_setup_logging_rotating_file_with_timestamps(tmp_path):
    root = logging.getLogger(); before = list(root.handlers)
    try:
        path = server.setup_logging(tmp_path / "logs", max_bytes=2000, backups=2)
        assert path.endswith("oku_wheel.log")
        assert server.setup_logging(tmp_path / "logs") == path                           # idempotent
        fh = [h for h in root.handlers if getattr(h, "baseFilename", "") .endswith("oku_wheel.log")]
        assert len(fh) == 1 and isinstance(fh[0], logging.handlers.RotatingFileHandler)
        lg = logging.getLogger("oku_wheel.test")
        for i in range(100): lg.warning("line %d %s", i, "x" * 40)
        for h in fh: h.flush()
        txt = (tmp_path / "logs" / "oku_wheel.log").read_text(encoding="utf-8")
        assert re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} WARNING oku_wheel\.test line", txt, re.M)
        assert (tmp_path / "logs" / "oku_wheel.log.1").exists()                           # rotated
    finally:
        for h in list(root.handlers):
            if h not in before: root.removeHandler(h); h.close()

def test_git_commit_env_git_and_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("OKU_COMMIT", "abc1234"); assert server.git_commit() == "abc1234"
    monkeypatch.delenv("OKU_COMMIT")
    sha = server.git_commit()
    try: real = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True, text=True).stdout.strip()
    except OSError: real = ""
    if real: assert sha == real
    assert server.git_commit(tmp_path) == "unknown"                                      # not a checkout
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/x\n"); (tmp_path / ".git" / "refs" / "heads" / "x").write_text("0123456789abcdef\n")
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert server.git_commit(tmp_path) == "0123456"

def test_api_state_reports_commit():
    from aiohttp.test_utils import TestServer, TestClient
    async def run():
        e = engine.Engine(config.load(), store.Store(), rng=random.Random(1))
        c = TestClient(TestServer(server.make_app(e, run_loop=False, commit="deadbee"))); await c.start_server()
        try:
            j = await (await c.get("/api/state")).json()
            assert j["commit"] == "deadbee" and "wheel" in j
        finally: await c.close()
    asyncio.run(run())
