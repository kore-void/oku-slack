"""Standalone scheduled porada (independent of the wheel): storylet calendar, dry run, budget/quiet hours/kill
switch, the file hand-off to the bridge (meeting_start.jsonl -> meeting_ack.jsonl) and the bridge side that posts
the opener and runs a real meeting.py porada in its thread."""
import json, pathlib, pytest
from oku_slack import handoff, meeting
from oku_slack.world import scheduler, service, tz
from test_handoff_world import coord

def P(y, mo, d, h, mi=0): return tz.to_ts(y, mo, d, h, mi)
MON10 = P(2026, 10, 12, 10)
CH = "C0C6W8E6NP9"

class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t

def mk(tmp_path, world=None, **env):
    tmp_path.mkdir(parents=True, exist_ok=True); cfg = tmp_path / "config.toml"
    w = {"dry_run": True, "porada_schedule": "mon-fri 10:00"}; w.update(world or {})
    def val(v): return json.dumps(v) if not isinstance(v, bool) else ("true" if v else "false")
    cfg.write_text("[world]\n" + "".join(f"{k} = {val(v)}\n" for k, v in w.items()), encoding="utf-8")
    clk = Clock(MON10 - 3600)
    svc = service.WorldService(config_path=cfg, logs_dir=tmp_path / "logs", source_logs=tmp_path / "src", clock=clk, ctx={})
    svc.startup(); return svc, clk

def types(svc): return [r["type"] for r in svc.diary.events()]
def requests(svc): return handoff._read(handoff.REQUESTS, 0, svc.outbox_dir)[0]

def test_parse_schedule_and_due_slot():
    assert scheduler.parse_schedule("mon-fri 10:00") == ({0, 1, 2, 3, 4}, [600])
    assert scheduler.parse_schedule("daily 09:30,15:00") == (set(range(7)), [570, 900])
    assert scheduler.parse_schedule("mon,wed,fri 10:00")[0] == {0, 2, 4} and scheduler.parse_schedule("fri-mon 8:00")[0] == {4, 5, 6, 0}
    for bad in ("", "mon-fri", "xyz 10:00", "mon 25:00"):
        with pytest.raises(ValueError): scheduler.parse_schedule(bad)
    assert scheduler.due_slot("mon-fri 10:00", MON10 - 60) is None
    assert scheduler.due_slot("mon-fri 10:00", MON10 + 60) == ("2026-10-12 10:00", MON10, False)
    assert scheduler.due_slot("mon-fri 10:00", MON10 + 31 * 60)[2] is True
    assert scheduler.due_slot("mon-fri 10:00", P(2026, 10, 17, 10, 5)) is None          # Saturday
    assert scheduler.due_slot("mon-fri 10:00", P(2026, 12, 7, 10, 1))[1] == P(2026, 12, 7, 10)  # winter time

def test_dry_run_logs_what_it_would_do_and_writes_no_handoff(tmp_path):
    svc, clk = mk(tmp_path)
    svc.tick(); assert "schedule.due" not in types(svc)                                 # 09:00: nothing due
    clk.t = MON10 + 20; res = svc.tick()
    assert res["scheduler"] == ["schedule.due", "porada.dry_run"]
    dr = svc.diary.events(types=["porada.dry_run"])[0]
    assert dr["payload"]["would"]["channel"] == CH and dr["payload"]["would"]["file"] == "meeting_start.jsonl" and dr["payload"]["topic"]
    assert dr["causal_parents"] == [svc.diary.events(types=["schedule.due"])[0]["id"]] and dr["source"] == "schedule"
    assert not (svc.outbox_dir / handoff.REQUESTS).exists()
    clk.t += 60; svc.tick(); clk.t += 3600; svc.tick()
    assert types(svc).count("schedule.due") == 1 and types(svc).count("porada.dry_run") == 1  # exactly once per slot
    svc2 = service.WorldService(config_path=svc.config_path, logs_dir=svc.logs_dir, source_logs=svc.source_logs, clock=clk, ctx={})
    svc2.tick(); assert [r["type"] for r in svc2.diary.events()].count("schedule.due") == 1  # restart-safe
    assert svc.world.state()["porada"]["dry_run"] == 1

def test_live_writes_handoff_line_and_records_started_ack(tmp_path):
    svc, clk = mk(tmp_path, {"dry_run": False}); clk.t = MON10 + 5
    svc.tick(); req = requests(svc)
    assert len(req) == 1 and req[0]["channel"] == CH and req[0]["source"] == "world" and req[0]["thread_ts"] is None
    assert req[0]["opener"] and req[0]["topic"] and req[0]["slot"] == "2026-10-12 10:00" and abs(req[0]["at"] - clk.t) < 1
    r = svc.diary.events(types=["porada.requested"])[0]
    assert r["payload"]["request_id"] == req[0]["id"] and r["payload"]["llm_calls"] == 12 and r["payload"]["top_level"]
    handoff._append(handoff.ACKS, {"id": req[0]["id"], "status": "started", "thread_ts": "1790000000.1"}, svc.outbox_dir)
    clk.t += 15; svc.tick()
    s = svc.diary.events(types=["porada.started"])[0]
    assert s["payload"]["thread_ts"] == "1790000000.1" and s["causal_parents"] == [r["id"]] and svc.diary.kv_get("porada:pending") is None
    b = svc.budget_view(); assert b["usage"]["posts_today"] == 1 and b["usage"]["llm_today"] == 12 and not b["porada_now"]["ok"]

def test_live_rejected_ack_and_missing_ack_become_failed(tmp_path):
    svc, clk = mk(tmp_path, {"dry_run": False}); clk.t = MON10 + 5; svc.tick()
    rid = requests(svc)[0]["id"]; handoff._append(handoff.ACKS, {"id": rid, "status": "rejected"}, svc.outbox_dir)
    svc.tick(); assert svc.diary.events(types=["porada.failed"])[0]["payload"]["status"] == "rejected"
    svc2, clk2 = mk(tmp_path / "b", {"dry_run": False}); clk2.t = MON10 + 5; svc2.tick()
    clk2.t += 121; svc2.tick(); assert svc2.diary.events(types=["porada.failed"])[0]["payload"]["status"] == "no_ack"

@pytest.mark.parametrize("world,setup,reason", [
    ({"quiet_hours": ["09:00", "11:00"]}, None, "quiet_hours"),
    ({"kill_switch": True}, None, "kill_switch:config"),
    ({}, "killfile", "kill_switch:file"),
    ({"posts_per_day": 0}, None, "posts_per_day 0/0"),
    ({"llm_calls_per_day": 5}, None, "llm_calls_per_day 0+12>5"),
])
def test_denied_by_budget_quiet_hours_or_kill_switch(tmp_path, world, setup, reason):
    svc, clk = mk(tmp_path, dict(world, dry_run=False)); clk.t = MON10 + 5
    if setup == "killfile": (svc.logs_dir / "world.kill").write_text("")
    svc.tick()
    den = svc.diary.events(types=["budget.denied"]); assert len(den) == 1 and reason in den[0]["payload"]["reasons"]
    assert not (svc.outbox_dir / handoff.REQUESTS).exists() and "porada.requested" not in types(svc)

def test_per_channel_gap_and_wheel_live_block_the_porada(tmp_path):
    svc, clk = mk(tmp_path, {"dry_run": False}); clk.t = MON10 + 5
    svc.diary.record("agent.posted", "marty", None, {"channel": CH}, source="agent", ts=MON10 - 3600)
    svc.tick(); assert svc.diary.events(types=["budget.denied"])[0]["payload"]["reasons"][0].startswith("per_channel_gap")
    svc2, clk2 = mk(tmp_path / "b", {"dry_run": False}); clk2.t = MON10 + 5
    svc2.diary.ingest({"type": "wheel.live", "source": "wheel", "subject": "wheel:event:e9", "payload": {"key": "disko"}, "ts": MON10 - 60})
    svc2.tick(); assert svc2.diary.events(types=["budget.denied"])[0]["payload"]["reasons"] == ["wheel_live"]

def test_late_start_skips_once_and_weekend_is_quiet(tmp_path):
    svc, clk = mk(tmp_path); clk.t = MON10 + 45 * 60; svc.tick(); svc.tick()
    assert types(svc).count("schedule.skipped") == 1 and "porada.dry_run" not in types(svc)
    svc2, clk2 = mk(tmp_path / "b"); clk2.t = P(2026, 10, 17, 10, 1); svc2.tick()
    assert "schedule.due" not in [r["type"] for r in svc2.diary.events()]

def test_config_reload_flips_dry_run_without_restart_and_env_override(tmp_path, monkeypatch):
    svc, clk = mk(tmp_path); assert svc.settings()["dry_run"] is True
    import os, time
    svc.config_path.write_text('[world]\ndry_run = false\n', encoding="utf-8"); os.utime(svc.config_path, (time.time() + 5, time.time() + 5))
    assert svc.settings()["dry_run"] is False
    monkeypatch.setenv("OKU_WORLD_DRY_RUN", "1"); svc._cfg = None; assert svc.settings()["dry_run"] is True

def test_topic_is_deterministic_per_slot_and_uses_world_state():
    snap = {"resources": {"dotace": 5000, "kampan": 20, "hranolky": 40, "lajky": 1200}, "blame": {"kalousek": 3}, "metrics_7d": {}, "recent": []}
    a = scheduler.topic_for(snap, "2026-10-12 10:00", {"kalousek": "Kalousek"}); b = scheduler.topic_for(snap, "2026-10-12 10:00", {"kalousek": "Kalousek"})
    assert a == b and a[1] in a[2] and "<@" not in a[2]
    kinds = {scheduler.topic_for(snap, f"2026-10-{d:02d} 10:00")[0] for d in range(1, 29)}
    assert {"blame", "hranolky", "kampan"} <= kinds
    assert scheduler.topic_for({}, "x")[1]                                                  # empty world still has a topic

def test_end_to_end_world_to_bridge_through_files(tmp_path, monkeypatch):
    svc, clk = mk(tmp_path, {"dry_run": False}); clk.t = MON10 + 5; svc.tick()
    monkeypatch.setenv("OKU_MEETING_OUTBOX", str(svc.outbox_dir))
    c, started = coord(monkeypatch)
    inbox = handoff.Inbox(c.start_external, clock=clk); assert inbox.poll()[0][1] == "started"
    clk.t += 15; svc.tick()
    s = svc.diary.events(types=["porada.started"]); assert len(s) == 1 and s[0]["payload"]["thread_ts"] == started[0].ts
    assert c.bridges["babis"].client.posts[0]["channel"] == CH
