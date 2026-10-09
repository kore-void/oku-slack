"""P-004 real-life triggers: the podnet inbox (validation, dedupe by URL, podnet.received / podnet.rejected), the
manual CLI, the reaction planner (persona + channel + comment, budget, guardrails, dry run vs live hand-off with a
podnet payload) (bridge side: test_chatter_bridge_podnet.py)."""
import json, pytest
from oku_slack import chatter as bridge_chatter, handoff
from oku_slack.world import chatter as wchatter, guard, podnet, react
from test_world_porada import mk, P, types, requests

MON15 = P(2026, 10, 12, 15)
URL = "https://x.com/AndrejBabis/status/1975000000000000000"

def world(**kw): return dict({"porada_enabled": False, "chatter_enabled": False}, **kw)
def rows(svc, t): return svc.diary.events(types=[t])

def test_normalize_and_dedupe_key():
    a = podnet.normalize_url("https://twitter.com/AndrejBabis/status/123?s=20&utm_source=x#frag")
    assert a == podnet.normalize_url("https://www.x.com/andrejbabis/status/123/") == "https://x.com/andrejbabis/status/123"
    assert podnet.key_for("https://mobile.twitter.com/AndrejBabis/status/123") == podnet.key_for(a)
    assert podnet.normalize_url("https://www.youtube.com/watch?v=abc&feature=share") == "https://youtube.com/watch?v=abc"
    for bad in ("", "ftp://x.com/a", "javascript:alert(1)", "https://localhost/x", "https://user:pw@x.com/a", "https://x.com/a b",
                "https://x.com:8080/a", "x" * 600, None, 5):
        with pytest.raises(podnet.Invalid): podnet.normalize_url(bad)

def test_validate_fields():
    v = podnet.validate({"url": URL, "kind": "x_video", "author": "@AndrejBabis", "text": "Krátký titulek", "source": "grokbot", "test": True}, now=MON15)
    assert v["title"] == "Krátký titulek" and v["observed_at"] == MON15 and v["source"] == "grokbot" and v["test"] is True and v["host"] == "x.com"
    for bad, why in (({"url": URL, "kind": "tiktok"}, "bad_kind"), ({"url": URL, "title": "x" * 281}, "title_too_long"),
                     ({"url": URL, "source": "Bad Source!"}, "bad_source"), ({"url": URL, "observed_at": "nope"}, "bad_observed_at"),
                     ({"url": URL, "test": "yes"}, "bad_test"), ({"url": URL, "observed_at": MON15 + 3 * 86400}, "observed_at_out_of_range")):
        with pytest.raises(podnet.Invalid) as e: podnet.validate(bad, now=MON15)
        assert str(e.value) == why

def test_inbox_tail_received_dedupe_and_rejected(tmp_path):
    svc, clk = mk(tmp_path, world(dry_run=True)); clk.t = MON15
    inbox = svc.inbox_path; assert inbox == tmp_path / "logs" / "inbox" / "podnety.jsonl"
    inbox.parent.mkdir(parents=True)
    with open(inbox, "w", encoding="utf-8") as f:
        f.write(json.dumps({"url": URL, "kind": "x_video", "author": "@AndrejBabis", "title": "Video", "source": "manual"}) + "\n")
        f.write(json.dumps({"url": URL.replace("x.com", "twitter.com") + "?s=20", "kind": "x_video"}) + "\n")   # same URL -> dup
        f.write("{not json\n")
        f.write(json.dumps({"url": "ftp://nope", "kind": "news"}) + "\n")
        f.write(json.dumps({"url": "https://example.org/a", "kind": "news"}))                                 # half line waits
    svc.tick()
    rec = rows(svc, "podnet.received"); rej = rows(svc, "podnet.rejected")
    assert len(rec) == 1 and sorted(r["payload"]["reason"] for r in rej) == ["bad_json", "bad_url"]
    r = rec[0]; pl = r["payload"]
    assert r["source"] == "podnet" and r["dedupe_key"] == podnet.key_for(URL) and r["actor"] == "ext:andrejbabis"
    assert pl["url"] == "https://x.com/andrejbabis/status/1975000000000000000" and pl["title"] == "Video" and "text" not in pl
    with open(inbox, "a", encoding="utf-8") as f: f.write("\n")
    svc.tick(); assert len(rows(svc, "podnet.received")) == 2                      # the completed line arrives
    svc.tick(); assert len(rows(svc, "podnet.received")) == 2 and len(rows(svc, "podnet.rejected")) == 2   # offsets: no replay

def test_cli_appends_and_rejects(tmp_path, capsys):
    inbox = tmp_path / "in.jsonl"
    assert podnet.main([URL, "--kind", "x_video", "--text", "test podnet", "--test", "--inbox", str(inbox)]) == 0
    line = json.loads(inbox.read_text(encoding="utf-8").strip())
    assert line["url"] == podnet.normalize_url(URL) and line["kind"] == "x_video" and line["test"] is True and line["source"] == "manual"
    assert podnet.main(["ftp://bad", "--inbox", str(inbox)]) == 2 and "podnet rejected: bad_url" in capsys.readouterr().err
    assert len(inbox.read_text(encoding="utf-8").splitlines()) == 1

def test_plan_personas_channels_and_comment_guardrails():
    plans = [react.plan(f"podnet:{i:04x}", "x_video", URL) for i in range(60)]
    assert {p["persona"] for p in plans} == {"marty", "babis"} and {p["reply_persona"] for p in plans} >= {None}
    for p in plans:
        assert p["channel"] == wchatter.CHANNELS[react.HOME[p["persona"]]] and p["opener"].startswith(URL + "\n")
        assert p["turns"] == (2 if p["reply_persona"] else 1) and p["reply_persona"] != p["persona"]
    assert react.plan("k", "x_video", URL) == react.plan("k", "x_video", URL)             # deterministic
    st = react.plan("k2", "stream", "https://twitch.tv/somechannel"); assert st["persona"] == "peta" and st["channel_name"] == "disko"
    for persona, groups in react.COMMENTS.items():
        for txt in sum(groups.values(), []):
            assert guard.guard_hit(txt) is None and txt.count(".") + txt.count("!") + txt.count("?") <= 2 and len(txt) <= 200, txt

def test_guard_patterns_match_the_bridge():
    assert guard.SENSITIVE_RE.pattern == bridge_chatter.SENSITIVE_RE.pattern and guard.QUOTE_RE.pattern == bridge_chatter.QUOTE_RE.pattern

def add(svc, url=URL, **kw):
    return podnet.append(url, kw.pop("kind", "x_video"), kw.pop("title", None), kw.pop("author", "@AndrejBabis"), path=svc.inbox_path, now=svc.clock(), **kw)

def test_dry_run_plans_reaction_no_handoff(tmp_path):
    svc, clk = mk(tmp_path, world(dry_run=True)); clk.t = MON15
    add(svc, test=True, title="test podnet"); res = svc.tick()
    assert res["podnet"] == ["podnet.dry_run"]
    r = rows(svc, "podnet.dry_run")[0]; pl = r["payload"]; rec = rows(svc, "podnet.received")[0]
    assert r["source"] == "director" and r["causal_parents"] == [rec["id"]] and r["subject"] == rec["subject"]
    assert pl["persona"] in ("marty", "babis") and r["actor"] == pl["persona"] and pl["opener"].startswith(pl["url"] + "\n")
    assert pl["test"] is True and pl["would"]["podnet"] is True and pl["channel"] == wchatter.CHANNELS[pl["channel_name"]]
    assert not (svc.outbox_dir / handoff.REQUESTS).exists() and svc.budget_view()["usage"]["posts_today"] == 0
    svc.tick(); assert len(rows(svc, "podnet.dry_run")) == 1                                  # decided once

def test_guard_skip_quiet_hours_wait_then_stale_and_daily_cap(tmp_path):
    svc, clk = mk(tmp_path, world(dry_run=True, podnet_max_age_h=2)); clk.t = MON15
    add(svc, "https://x.com/AndrejBabis/status/1", title="Soud rozhodl"); svc.tick()
    assert rows(svc, "podnet.skipped")[0]["payload"]["reason"].startswith("guard:sensitive")
    clk.t = P(2026, 10, 12, 23)
    add(svc, "https://x.com/AndrejBabis/status/2"); svc.tick(); svc.tick()
    assert len(rows(svc, "podnet.skipped")) == 1                                              # quiet hours: waits, no row
    clk.t += 3 * 3600; svc.tick()
    s = rows(svc, "podnet.skipped")[-1]["payload"]; assert s["reason"] == "stale" and s["reasons"] == ["quiet_hours"]
    clk.t = P(2026, 10, 13, 12)
    for i in range(3, 6): add(svc, f"https://x.com/AndrejBabis/status/{i}")
    svc.tick()
    assert len(rows(svc, "podnet.dry_run")) == 2 and "podnet_reactions_per_day 2/2" in rows(svc, "budget.denied")[-1]["payload"]["reasons"]

def test_live_handoff_with_podnet_payload_and_acks(tmp_path):
    svc, clk = mk(tmp_path, world(dry_run=False)); clk.t = MON15
    add(svc, test=True); svc.tick()
    assert rows(svc, "podnet.skipped")[0]["payload"]["reason"] == "test" and not requests(svc)      # test podnets never go live
    add(svc, "https://x.com/AndrejBabis/status/77"); svc.tick()
    req = requests(svc)[-1]; r = rows(svc, "podnet.requested")[0]; pl = r["payload"]
    assert req["kind"] == "chatter" and req["source"] == "world:chatter" and req["thread_ts"] is None and req["storylet"] == "PODNET"
    assert req["podnet"]["url"] == "https://x.com/andrejbabis/status/77" and req["opener"].startswith(req["podnet"]["url"])
    assert req["personas"] == pl["participants"] and req["turns"] == pl["turns"] and pl["request_id"] == req["id"]
    assert svc.budget_view()["usage"]["posts_today"] == 1                                       # counts as a top-level post
    handoff._append(handoff.ACKS, {"id": req["id"], "status": "started", "thread_ts": "1.2"}, svc.outbox_dir); svc.tick()
    handoff._append(handoff.ACKS, {"id": req["id"], "status": "done", "turns": 2, "llm_calls": 1}, svc.outbox_dir); svc.tick()
    assert [x["type"] for x in svc.diary.events() if x["type"].startswith("podnet.")][-2:] == ["podnet.started", "podnet.ended"]
    assert not svc.diary.kv_get("podnet:pending")
