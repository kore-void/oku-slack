"""P0-a wheel side: a porada event going live posts only the opener (thread root), asks the bridge for a REAL meeting
via oku_slack.handoff and drops the scripted beats once acked; scripted beats only as a fallback (no ack in ~30 s,
or dup/stale). Live skit threads are published for the bridge. conftest points the outbox at tmp."""
from oku_slack import handoff
from oku_slack.wheel import scenes, slack_adapter as SA
from test_wheel_v2 import mk, go_live, runner, FakeSlack

def _req():
    reqs, _ = handoff._read(handoff.REQUESTS); return reqs

def test_porada_opener_is_root_then_real_meeting_requested_and_beats_dropped():
    e, clk = mk(); ev = go_live(e, clk, "porada")
    r = runner(e, clk); r.start(ev); clk.adv(3); assert r.step(ev["id"])
    P = r.poster.posts; assert len(P) == 1 and P[0][0] == "babis" and P[0][2] is None       # opener, top-level
    root = P[0][3]; [q] = _req()
    assert (q["channel"], q["thread_ts"], q["event_id"], q["host"]) == (r.poster.channel, root, ev["id"], "babis")
    assert "porada" in q["topic"].lower()
    clk.adv(20); assert r.step(ev["id"]) and len(P) == 1                                    # beats held while waiting (< 30 s)
    lt = handoff.read_live_threads(clock=clk)[root]; assert not lt["skit_running"] and lt["until"] == e.active_event()["end_at"]
    handoff.Inbox(lambda req: "started", clock=clk).poll()                                 # bridge picks it up
    assert not r.step(ev["id"])                                                             # scene over: meeting owns the thread
    st = r.state(ev["id"]); assert st["handoff"]["state"] == "meeting" and st["done"]
    assert all(b.get("skipped") for b in st["beats"][1:]) and len(P) == 1
    clk.adv(400); assert not r.step(ev["id"]) and len(P) == 1 and len(_req()) == 1          # never re-requested
    assert handoff.read_live_threads(clock=clk)[root]["key"] == "porada"                    # still a live wheel thread

def test_porada_without_bridge_ack_falls_back_to_scripted_beats_after_timeout():
    e, clk = mk(); ev = go_live(e, clk, "porada")
    r = runner(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"])
    clk.adv(20); r.step(ev["id"]); assert len(r.poster.posts) == 1                          # waiting (20 s < 30 s)
    clk.adv(11); r.step(ev["id"])                                                           # 31 s: timeout -> fallback
    st = r.state(ev["id"]); assert st["handoff"] == dict(st["handoff"], state="fallback", reason="timeout")
    root = r.poster.posts[0][3]
    assert handoff.read_live_threads(clock=clk)[root]["skit_running"]
    for _ in range(10): clk.adv(20); r.step(ev["id"])
    P = r.poster.posts; assert len(P) == 6 and all(p[2] == root for p in P[1:])            # the rest, in the opener's thread
    assert not handoff.read_live_threads(clock=clk)[root]["skit_running"]

def test_porada_dup_ack_falls_back_immediately_and_no_second_request():
    e, clk = mk(); ev = go_live(e, clk, "porada")
    r = runner(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"])
    handoff.Inbox(lambda req: "dup", clock=clk).poll(); clk.adv(1); r.step(ev["id"])
    assert r.state(ev["id"])["handoff"]["reason"] == "dup"
    clk.adv(2); r.step(ev["id"]); assert len(r.poster.posts) == 2 and len(_req()) == 1

def test_restart_while_waiting_resumes_without_duplicate_opener_or_request():
    e, clk = mk(); ev = go_live(e, clk, "porada")
    r = runner(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"])
    r2 = runner(e, clk, poster=r.poster); r2.start(ev); clk.adv(5); r2.step(ev["id"])
    assert len(r.poster.posts) == 1 and len(_req()) == 1
    handoff.Inbox(lambda req: "started", clock=clk).poll(); assert not r2.step(ev["id"])

def test_non_porada_scene_unchanged_no_request_and_published_as_running_skit():
    e, clk = mk(); ev = go_live(e, clk, "kantyna")
    r = runner(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"]); clk.adv(20); r.step(ev["id"])
    assert len(r.poster.posts) == 2 and _req() == []
    root = r.poster.posts[0][3]; lt = handoff.read_live_threads(clock=clk)[root]
    assert lt["skit_running"] and lt["host"] == "alenka" and lt["channel"] == r.poster.channel

def test_handoff_off_or_titanic_never_requests():
    e, clk = mk(meeting_handoff=False); ev = go_live(e, clk, "porada")
    r = runner(e, clk); r.start(ev); clk.adv(30); r.step(ev["id"]); assert len(r.poster.posts) == 2 and _req() == []
    e, clk = mk(); ev = go_live(e, clk, "snemovna"); r = runner(e, clk)
    assert not r.wants_meeting(ev)

def test_event_end_unpublishes_thread():
    e, clk = mk(); ev = go_live(e, clk, "kantyna"); r = runner(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"])
    root = r.poster.posts[0][3]; assert root in handoff.read_live_threads(clock=clk)
    post = SA.make_poster(e, FakeSlack(), "C1", None, None, r); post("done", ev)
    assert root not in handoff.read_live_threads(clock=clk)
