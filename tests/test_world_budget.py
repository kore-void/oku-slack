"""Budget, quiet hours (Europe/Prague, incl. the built-in DST fallback for Windows without tzdata), kill switch."""
import datetime as dt
from oku_slack.world import budget, tz

def P(y, mo, d, h, mi=0): return tz.to_ts(y, mo, d, h, mi)
MON10 = P(2026, 10, 12, 10)          # Monday 10:00 Prague (CEST)
C = budget.settings({})

def row(t, ts, src="schedule", **pl): return {"type": t, "ts": ts, "source": src, "payload": pl}

def test_defaults_match_decision_d2():
    assert (C["posts_per_day"], C["per_channel_gap_h"], C["chatter_threads_per_day"], C["chatter_max_turns"], C["llm_calls_per_day"]) == (6, 3.0, 2, 4, 40)
    assert C["quiet_hours"] == ["22:00", "08:00"] and C["dry_run"] is True and C["timezone"] == "Europe/Prague"

def test_quiet_hours_wrap_midnight_in_prague_time():
    assert budget.is_quiet(P(2026, 10, 12, 22, 0)) and budget.is_quiet(P(2026, 10, 13, 3, 0)) and budget.is_quiet(P(2026, 10, 13, 7, 59))
    assert not budget.is_quiet(P(2026, 10, 13, 8, 0)) and not budget.is_quiet(MON10) and not budget.is_quiet(P(2026, 10, 12, 21, 59))
    assert budget.is_quiet(MON10, ["09:00", "11:00"]) and not budget.is_quiet(MON10, ["10:00", "10:00"]) and not budget.is_quiet(MON10, [])
    winter = P(2026, 12, 7, 10)                       # CET: 10:00 Prague == 09:00 UTC
    assert dt.datetime.fromtimestamp(winter, dt.timezone.utc).hour == 9 and dt.datetime.fromtimestamp(MON10, dt.timezone.utc).hour == 8

def test_dst_fallback_matches_zoneinfo_when_available():
    for ts in [P(2026, 3, 29, 1, 59), P(2026, 3, 29, 3, 0), P(2026, 10, 25, 1, 0), P(2026, 10, 25, 4, 0), MON10, P(2027, 1, 1, 0)]:
        a, b = tz.local(ts, force_fallback=True), tz.local(ts)
        assert (a.hour, a.minute, a.utcoffset()) == (b.hour, b.minute, b.utcoffset()), ts
    assert tz.to_ts(2026, 10, 12, 10, force_fallback=True) == MON10 and tz.to_ts(2026, 12, 7, 10, force_fallback=True) == P(2026, 12, 7, 10)
    assert tz.prague_offset_s(MON10) == 7200 and tz.prague_offset_s(P(2026, 12, 7, 10)) == 3600

def test_daily_post_cap_counts_only_real_top_level_posts():
    rows = [row("agent.posted", MON10 - 3600 * i, "agent", channel=f"C{i}") for i in range(1, 6)]
    rows += [row("porada.dry_run", MON10 - 60, channel="CX"), row("agent.posted", MON10 - 90, "agent", channel="CY", top_level=False)]
    ok = budget.check("post", rows, MON10, C, channel="CZ"); assert ok["ok"] and ok["usage"]["posts_today"] == 5
    rows.append(row("porada.requested", MON10 - 30, channel="CQ", llm_calls=12))
    no = budget.check("post", rows, MON10, C, channel="CZ"); assert not no["ok"] and no["reasons"] == ["posts_per_day 6/6"]
    yesterday = [row("agent.posted", MON10 - 86400, "agent", channel="C1")] * 10
    assert budget.check("post", yesterday, MON10, C, channel="CZ")["ok"]           # a new Prague day resets the cap

def test_per_channel_gap_three_hours():
    rows = [row("porada.requested", MON10 - 2 * 3600, channel="C0C6W8E6NP9")]
    d = budget.check("post", rows, MON10, C, channel="C0C6W8E6NP9"); assert not d["ok"] and d["reasons"][0].startswith("per_channel_gap C0C6W8E6NP9 120 min")
    assert budget.check("post", rows, MON10, C, channel="C0OTHER")["ok"]
    assert budget.check("post", rows, MON10 + 3600, C, channel="C0C6W8E6NP9")["ok"]

def test_chatter_and_llm_caps():
    rows = [row("chatter.started", MON10 - 100, "chatter", channel="C1"), row("chatter.started", MON10 - 50, "chatter", channel="C2")]
    assert budget.check("chatter", rows, MON10, C)["reasons"] == ["chatter_threads_per_day 2/2"]
    assert budget.check("chatter_turn", [], MON10, C, turns=5)["reasons"] == ["chatter_max_turns 5>4"] and budget.check("chatter_turn", [], MON10, C, turns=4)["ok"]
    llm = [row("porada.requested", MON10 - 4 * 3600, llm_calls=12), row("agent.posted", MON10 - 3600 * 5, "agent", llm_calls=20),
           row("bridge.reply", MON10 - 10, "bridge", llm_calls=1)]               # bridge replies are not world calls
    u = budget.usage(llm, MON10, C); assert u["llm_today"] == 32
    assert budget.check("llm", llm, MON10, C, llm=8)["ok"] and budget.check("llm", llm, MON10, C, llm=9)["reasons"] == ["llm_calls_per_day 32+9>40"]

def test_quiet_hours_wheel_live_and_kill_switch_deny(tmp_path, monkeypatch):
    assert "quiet_hours" in budget.check("post", [], P(2026, 10, 12, 23), C)["reasons"]
    assert budget.check("post", [], MON10, C, wheel_live=True)["reasons"] == ["wheel_live"]
    assert budget.check("post", [], MON10, dict(C, no_posts_while_wheel_live=False), wheel_live=True)["ok"]
    assert budget.killed({"kill_switch": True}) == "kill_switch:config" and budget.killed({}, tmp_path) is None
    (tmp_path / "world.kill").write_text(""); assert budget.killed({}, tmp_path) == "kill_switch:file"
    kv = {"world:silenced_until": str(MON10 + 60)}
    assert budget.killed({}, None, kv.get, MON10) == "kill_switch:silenced" and budget.killed({}, None, kv.get, MON10 + 61) is None
    monkeypatch.setenv("OKU_WORLD_KILL", "1"); assert budget.killed({}) == "kill_switch:env"
    d = budget.check("post", [], MON10, C, kill="kill_switch:file"); assert not d["ok"] and d["reasons"] == ["kill_switch:file"]
