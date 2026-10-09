"""World side of P-005: the director picks 2-3 OKÚ personas, a persona channel and a topic from world state / diary
events / storylet templates per chatter slot; budget + quiet hours + kill switch + wheel live; dry run writes only
`chatter.dry_run` (no hand-off); live hands a kind=chatter line to the bridge and records started/ended/failed."""
import json, pytest
from oku_slack import chatter as bridge_chatter, handoff, meeting
from oku_slack.world import chatter, tz
from test_world_porada import mk, P, types, requests

MON1130 = P(2026, 10, 12, 11, 30)
MON1630 = P(2026, 10, 12, 16, 30)
CHANNEL_IDS = set(chatter.CHANNELS.values())

def world(**kw): return dict({"porada_enabled": False}, **kw)
def rows(svc, t): return svc.diary.events(types=[t])

def test_dry_run_writes_one_chatter_row_and_no_handoff(tmp_path):
    svc, clk = mk(tmp_path, world())
    svc.tick(); assert "chatter.dry_run" not in types(svc)                                   # 09:00: nothing due
    clk.t = MON1130 + 20; res = svc.tick()
    assert res["chatter"] == ["schedule.due", "chatter.dry_run"]
    r = rows(svc, "chatter.dry_run")[0]; pl = r["payload"]
    assert r["source"] == "chatter" and r["subject"] == "chatter:2026-10-12 11:30" and r["actor"] == pl["personas"][0]
    assert 2 <= len(pl["personas"]) <= 3 and set(pl["personas"]) <= set(chatter.CAST) and len(set(pl["personas"])) == len(pl["personas"])
    assert pl["channel"] in CHANNEL_IDS and pl["channel"] == chatter.CHANNELS[pl["channel_name"]] and pl["topic"] and pl["opener"]
    assert pl["turns"] in (3, 4) and pl["llm_estimate"] == pl["turns"] - 1 and pl["rng_key"] == "CHATTER:2026-10-12 11:30"
    assert pl["would"]["kind"] == "chatter" and r["causal_parents"] == [rows(svc, "schedule.due")[0]["id"]]
    assert not (svc.outbox_dir / handoff.REQUESTS).exists()
    clk.t += 60; svc.tick(); clk.t += 1200; svc.tick()
    assert types(svc).count("chatter.dry_run") == 1                                           # once per slot, restart-safe
    st = svc.world.state(); assert st["chatter"]["dry_run"] == 1 and st["chatter"]["due"] == 1
    assert any(m["type"] == "chatter.dry_run" for m in st["memory"][pl["personas"][1]])      # participants remember it
    assert svc.budget_view()["usage"]["chatter_today"] == 0                                   # dry runs consume nothing

def test_second_slot_avoids_same_storylet_and_channel(tmp_path):
    svc, clk = mk(tmp_path, world())
    clk.t = MON1130 + 5; svc.tick(); clk.t = MON1630 + 5; svc.tick()
    a, b = [r["payload"] for r in rows(svc, "chatter.dry_run")]
    assert a["storylet"] != b["storylet"] and a["channel"] != b["channel"]

def test_pick_is_deterministic_state_driven_and_never_repeats_speakers():
    ctx, have = chatter.facts({"resources": {"dotace": 5000, "kampan": 20, "hranolky": 40, "lajky": 1200}, "blame": {"icik": 3},
                               "metrics_7d": {"bridge_calls": 12}, "recent": [{"state": "expired", "key": "kantyna"}]},
                              {"icik": "ICIK"}, {"kantyna": "Kantýna"}, porada_topic="Kampaň stojí na 20/100.")
    assert have == {"blame", "hranolky_low", "kampan_low", "missed", "busy", "porada_today"} and ctx["blame_who"] == "ICIK"
    a = chatter.pick("2026-10-12 11:30", ctx, have); assert a == chatter.pick("2026-10-12 11:30", ctx, have)
    picks = [chatter.pick(f"2026-10-{d:02d} 11:30", ctx, have) for d in range(1, 29)]
    keys = {p["storylet"] for p in picks}
    assert {"KALOUSEK_VINA", "HRANOLKY_DOCHAZEJI"} <= keys and all(p["personas"][0] == chatter.BY_KEY[p["storylet"]]["lead"] for p in picks)
    assert all(len(set(p["personas"])) == len(p["personas"]) for p in picks)
    _, none = chatter.facts({"resources": {"kampan": 80, "hranolky": 80}}); assert none == set(); generic = {chatter.pick(f"s{i}", {"dotace": "5 000", "lajky": "1 200"}, none)["storylet"] for i in range(40)}
    assert generic <= {s["key"] for s in chatter.STORYLETS if s["needs"] is None}                 # empty world: templates only
    assert chatter.pick("x", ctx, have, max_turns=2)["turns"] == 2

def test_blocked_and_used_channels_are_avoided_when_possible():
    ctx, have = chatter.facts({"resources": {"dotace": 1, "lajky": 1}})
    vina, kantyna = chatter.CHANNELS["vina"], chatter.CHANNELS["kantyna"]
    for i in range(30):
        p = chatter.pick(f"s{i}", ctx, have, blocked_channels={kantyna, chatter.CHANNELS["socky"]}, avoid_channels=["dotace"])
        assert p["channel"] not in (kantyna, chatter.CHANNELS["socky"], chatter.CHANNELS["dotace"])
    assert chatter.pick("s", ctx, have, channels={"vina": vina}) is None                       # vina storylets need blame/missed
    assert chatter.pick("s", ctx, {"blame"}, channels={"vina": vina})["topic"] == "Jak to dneska v OKÚ vypadá?"  # fact missing: static text
    assert chatter.pick("s", dict(ctx, blame_who="ICIK", blame_n=2), {"blame"}, channels={"vina": vina})["channel"] == vina

def test_templates_follow_the_satire_guardrails():
    ctx = {"dotace": "5 000", "kampan": "20", "hranolky": "40", "lajky": "1 200", "blame_who": "ICIK", "blame_n": 3,
           "missed_title": "Kantýna", "bridge_calls": 12, "porada_topic": "Kampaň stojí na 20/100."}
    for s in chatter.STORYLETS:
        assert s["lead"] in chatter.CAST and set(s["pool"]) <= set(chatter.CAST) and s["lead"] not in s["pool"] and s["channel"] in chatter.CHANNELS
        for t in [s["topic"]] + s["openers"]:
            out = t.format(**ctx)
            assert bridge_chatter.guard_hit(out) is None, (s["key"], out)
            assert "<@" not in out and "@" not in out and len(out) <= 300 and not any(q in out for q in "„“\"")
    assert "NEVYMÝŠLEJ citáty skutečných lidí" in bridge_chatter.CHATTER_RULES and "zdraví" in bridge_chatter.CHATTER_RULES

def test_live_request_then_started_and_done_acks(tmp_path):
    svc, clk = mk(tmp_path, world(dry_run=False)); clk.t = MON1130 + 5; svc.tick()
    req = requests(svc); assert len(req) == 1 and req[0]["kind"] == "chatter" and req[0]["source"] == "world:chatter"
    q = req[0]; r = rows(svc, "chatter.requested")[0]["payload"]
    assert q["personas"] == r["personas"] and q["channel"] == r["channel"] and q["turns"] == r["turns"] and q["thread_ts"] is None
    assert r["request_id"] == q["id"] and r["llm_calls"] == q["turns"] - 1 and r["top_level"] is True
    b = svc.budget_view(); assert b["usage"]["chatter_today"] == 1 and b["usage"]["posts_today"] == 1 and b["chatter_pending"]
    handoff._append(handoff.ACKS, {"id": q["id"], "status": "started", "thread_ts": "1790000001.1", "turns": q["turns"]}, svc.outbox_dir)
    clk.t += 15; svc.tick()
    s = rows(svc, "chatter.started")[0]; assert s["payload"]["thread_ts"] == "1790000001.1" and "chatter.ended" not in types(svc)
    handoff._append(handoff.ACKS, {"id": q["id"], "status": "done", "thread_ts": "1790000001.1", "turns": 3, "llm_calls": 2}, svc.outbox_dir)
    clk.t += 15; svc.tick()
    e = rows(svc, "chatter.ended")[0]
    assert e["payload"]["status"] == "done" and e["payload"]["llm_calls_actual"] == 2 and e["causal_parents"] == [s["id"]]
    assert svc.diary.kv_get("chatter:pending") is None and svc.budget_view()["usage"]["chatter_today"] == 1   # counted once
    assert svc.world.state()["chatter"]["ended"] == 1

def test_failed_rejected_no_ack_and_done_timeout(tmp_path):
    svc, clk = mk(tmp_path / "a", world(dry_run=False)); clk.t = MON1130 + 5; svc.tick()
    handoff._append(handoff.ACKS, {"id": requests(svc)[0]["id"], "status": "rejected", "reason": "persona_offline:peta"}, svc.outbox_dir)
    svc.tick(); f = rows(svc, "chatter.failed")[0]["payload"]; assert f["status"] == "rejected" and f["reason"] == "persona_offline:peta"
    svc, clk = mk(tmp_path / "b", world(dry_run=False)); clk.t = MON1130 + 5; svc.tick(); clk.t += 121; svc.tick()
    assert rows(svc, "chatter.failed")[0]["payload"]["status"] == "no_ack"
    svc, clk = mk(tmp_path / "c", world(dry_run=False)); clk.t = MON1130 + 5; svc.tick()
    handoff._append(handoff.ACKS, {"id": requests(svc)[0]["id"], "status": "started", "thread_ts": "1.1"}, svc.outbox_dir)
    svc.tick(); clk.t += 901; svc.tick()
    assert rows(svc, "chatter.ended")[0]["payload"]["status"] == "timeout" and svc.diary.kv_get("chatter:pending") is None

@pytest.mark.parametrize("cfg,setup,reason", [
    ({"quiet_hours": ["11:00", "12:00"]}, None, "quiet_hours"),
    ({"kill_switch": True}, None, "kill_switch:config"),
    ({}, "killfile", "kill_switch:file"),
    ({"chatter_threads_per_day": 0}, None, "chatter_threads_per_day 0/0"),
    ({"llm_calls_per_day": 1}, None, "llm_calls_per_day"),
    ({"posts_per_day": 0}, None, "posts_per_day 0/0"),
    ({}, "wheel_live", "wheel_live"),
])
def test_denied_by_budget_quiet_hours_kill_switch_or_wheel(tmp_path, cfg, setup, reason):
    svc, clk = mk(tmp_path, world(dry_run=False, **cfg)); clk.t = MON1130 + 5
    if setup == "killfile": (svc.logs_dir / "world.kill").write_text("")
    if setup == "wheel_live": svc.diary.ingest({"type": "wheel.live", "source": "wheel", "subject": "wheel:event:e1", "payload": {"key": "disko"}, "ts": clk.t - 60})
    svc.tick()
    den = rows(svc, "budget.denied"); assert len(den) == 1 and any(x.startswith(reason) for x in den[0]["payload"]["reasons"])
    assert den[0]["payload"]["storylet"] == "CHATTER" and den[0]["payload"]["pick"] and den[0]["source"] == "chatter"
    assert not (svc.outbox_dir / handoff.REQUESTS).exists() and "chatter.requested" not in types(svc)
    assert svc.world.state()["chatter"]["denied"] == 1

def test_all_channels_inside_gap_denies_with_per_channel_gap(tmp_path):
    svc, clk = mk(tmp_path, world(dry_run=False)); clk.t = MON1130 + 5
    for ch in chatter.CHANNELS.values(): svc.diary.record("agent.posted", "marty", None, {"channel": ch}, source="agent", ts=clk.t - 600)
    svc.tick(); assert rows(svc, "budget.denied")[0]["payload"]["reasons"][0].startswith("per_channel_gap")

def test_no_storylet_for_configured_channels_is_recorded(tmp_path):
    svc, clk = mk(tmp_path, world()); clk.t = MON1130 + 5
    svc.chatter.cfg = lambda: dict(chatter.ChatterDirector.cfg(svc.chatter), chatter_channels={"vina": chatter.CHANNELS["vina"]})
    svc.tick(); d = rows(svc, "budget.denied")[0]["payload"]; assert d["reasons"] == ["no_storylet"] and d["channels"] == ["vina"]

def test_porada_today_feeds_the_topic_and_late_or_disabled_slots(tmp_path):
    svc, clk = mk(tmp_path, world())
    svc.diary.record("porada.dry_run", "babis", "schedule:porada:x", {"topic": "Kampaň stojí na 20/100.", "channel": "C0C6W8E6NP9"},
                     source="schedule", ts=P(2026, 10, 12, 10, 0))
    clk.t = MON1130 + 5
    snap = svc.world.snapshot(players=False); _, have = chatter.facts(snap, porada_topic="Kampaň stojí na 20/100.")
    assert "porada_today" in have
    svc2, clk2 = mk(tmp_path / "late", world()); clk2.t = MON1130 + 45 * 60; svc2.tick(); svc2.tick()
    assert types(svc2).count("schedule.skipped") == 1 and "chatter.dry_run" not in types(svc2)
    svc3, clk3 = mk(tmp_path / "off", world(chatter_enabled=False)); clk3.t = MON1130 + 5; svc3.tick()
    assert "schedule.due" not in types(svc3)
    svc4, clk4 = mk(tmp_path / "sat", world()); clk4.t = P(2026, 10, 17, 11, 31); svc4.tick(); assert "schedule.due" not in types(svc4)

def test_end_to_end_world_to_bridge_through_files(tmp_path, monkeypatch):
    svc, clk = mk(tmp_path, world(dry_run=False)); clk.t = MON1130 + 5; svc.tick()
    monkeypatch.setenv("OKU_MEETING_OUTBOX", str(svc.outbox_dir))
    from test_chatter_bridge import mk as bridge_mk
    names = {"babis": "Andrej Babiš", "alenka": "Alenka Hranolka", "bourak": "Filip Bourák Turek", "marty": "Marty Prchal",
             "peta": "Peťa Maci", "kalousek": "Kalousek"}
    cfg = {"personas": {k: {"name": n, "aliases": [k]} for k, n in names.items()}, "world": {"chatter_channels": dict(chatter.CHANNELS)}}
    c, slack, seen = bridge_mk(cfg=cfg)
    inbox = handoff.Inbox(lambda q: c.start_external(q) if q.get("kind") != "chatter" else c.start_chatter(q, sleep=lambda d: None), clock=clk)
    assert inbox.poll()[0][1] == "started"; c.chatter_thread.join(5)
    clk.t += 15; svc.tick()
    req = requests(svc)[0]
    assert len(slack.posts) == req["turns"] and slack.posts[0]["channel"] == req["channel"] and "thread_ts" not in slack.posts[0]
    s, e = rows(svc, "chatter.started")[0], rows(svc, "chatter.ended")[0]
    assert s["payload"]["thread_ts"] == slack.posts[0]["ts"] and e["payload"]["turns"] == req["turns"] and e["payload"]["llm_calls_actual"] == req["turns"] - 1
