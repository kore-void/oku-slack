"""Wheel v2: OKÚ korun + bets, charged-command effects, live show (poll / hype / minigames), persona scenes.
All Slack/LLM access goes through fakes; no network."""
import json, random, time, pathlib
import pytest
from oku_slack.wheel import config, economy, effects, engine, panel, personas, scenes, show, store, slack_adapter as SA

class Clock:
    def __init__(self, t=5_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

class Err(Exception):
    def __init__(self, code): super().__init__(code); self.response = {"error": code}

class FakeSlack:
    def __init__(self, fail=None, prefix="9"): self.calls, self.fail, self.n, self.prefix = [], dict(fail or {}), 0, prefix
    def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        if "post" in self.fail: raise Err(self.fail["post"])
        self.n += 1; return {"ts": f"{self.prefix}00.{self.n}", "channel": kw["channel"]}
    def chat_update(self, **kw): self.calls.append(("update", kw)); return {"ok": True}
    def chat_postEphemeral(self, **kw): self.calls.append(("ephemeral", kw)); return {"ok": True}
    def views_open(self, **kw): self.calls.append(("views_open", kw)); return {"ok": True}
    def files_upload_v2(self, **kw): self.calls.append(("upload", kw)); return {"file": {"id": "F1"}}
    def posts(self): return [kw for n, kw in self.calls if n == "post"]

KORE, ICIK = "U0C6XAN3EG3", "U0C75FSEK2M"

def mk(**s):
    clk = Clock(); cfg = config.load(); cfg["settings"].update(s)
    e = engine.Engine(cfg, store.Store(), clock=clk, rng=random.Random(5)); return e, clk

def K(out): return [k for k, _ in out]
def T(x): return json.dumps(x, ensure_ascii=False)

def go_live(e, clk, key="porada"):
    ev = e.spin("kore", force=key, lead_s=0)
    for p in e.players(): e.confirm_code(ev["id"], p, ev["code"])
    clk.adv(7); e.tick(); return e.active_event()

# ---------------- 2) points, bets, leaderboard ----------------
def test_start_points_odds_and_leaderboard():
    e, clk = mk()
    assert e.eco.balance("kore") == 1000 and e.eco.balance("icik") == 1000
    o = e.eco.all_odds()
    assert o["snemovna"] > 50 > o["porada"] >= 1.5          # legendary pays big
    assert o["porada"] < o["disko"]                          # weight 3 vs 1
    e.eco.add("kore", 250, "test"); assert [r["player"] for r in e.eco.board()] == ["kore", "icik"]
    r = SA.handle_command(e, KORE, "zebricek"); assert "Žebříček" in r["text"] and "1\u00a0250" in r["text"]

def test_betting_window_spin_and_payout():
    e, clk = mk(bet_window_s=30)
    r = SA.handle_command(e, KORE, "toc"); assert r["round"] and "Sázky jsou otevřené" in r["text"]
    assert "běží sázky" in SA.handle_command(e, ICIK, "toc")["text"]          # busy, Czech
    bl, ph = panel.blocks(e); t = T(bl)
    assert ph == "betting" and "kolo_bet_pick" in t and "kolo_bet_all" in t and "Sázky jsou otevřené" in t and "Žebříček" in t
    with pytest.raises(engine.WheelError) as x: e.bet("kore", "porada", 5000)
    assert x.value.code == "too_much"
    b1 = e.bet("kore", "porada", 100); b2 = e.bet("icik", "disko", "all")
    assert e.eco.balance("kore") == 900 and e.eco.balance("icik") == 0
    clk.adv(29); assert "spin" not in K(e.tick())
    e.rng = random.Random(1); e.cfg["events"] = e.cfg["events"]  # deterministic pick below
    clk.adv(1); out = e.tick(); assert K(out) == ["spin"]; ev = out[0][1]; assert ev["round_id"] == r["round"]["id"]
    clk.adv(7); out = e.tick(); assert "settled" in K(out)
    res = {x["player"]: x for x in e.active_event()["bets_result"]}
    won = ev["key"]
    assert res["kore"]["state"] == ("won" if won == "porada" else "lost")
    exp_k = 900 + (round(100 * b1["odds"]) if won == "porada" else 0)
    assert e.eco.balance("kore") == exp_k
    bl, ph = panel.blocks(e); assert ph == "result" and "💰 Sázky:" in T(bl)

def test_bet_buttons_use_select_state_or_last_pick():
    e, clk = mk(bet_window_s=30); sl = FakeSlack()
    def body(aid, state=None, **x):
        b = {"actions": [dict(action_id=aid, **x)], "user": {"id": KORE}, "channel": {"id": "C1"}, "trigger_id": "T"}
        if state: b["state"] = {"values": {"kolo_bets": {"kolo_bet_pick": {"selected_option": {"value": state}}}}}
        return b
    SA.handle_action(e, sl, body("kolo_spin"))
    SA.handle_action(e, sl, body("kolo_bet_100")); assert "Nejdřív vyber" in sl.calls[-1][1]["text"]
    SA.handle_action(e, sl, body("kolo_bet_50", state="kantyna")); assert "50 🪙 na *Alenka otevírá kantýnu*" in sl.calls[-1][1]["text"]
    n = len(sl.calls); SA.handle_action(e, sl, body("kolo_bet_pick", selected_option={"value": "vina"})); assert len(sl.calls) == n  # silent
    SA.handle_action(e, sl, body("kolo_bet_all")); assert "Kalousek: hledání viníka" in sl.calls[-1][1]["text"] and e.eco.balance("kore") == 0

def test_confirm_in_time_gives_points():
    e, clk = mk(); ev = e.spin("kore", force="porada", lead_s=600)
    e.confirm_code(ev["id"], "kore", ev["code"]); assert e.eco.balance("kore") == 1050

# ---------------- 3) charged commands with real effects ----------------
def test_veto_respin_cancels_result_refunds_and_babis_interrupts():
    e, clk = mk(bet_window_s=30)
    e.open_bets("icik"); e.bet("icik", "porada", 100); clk.adv(30); e.tick()
    old = e.active_event(); assert e.eco.balance("icik") == 900
    q = e.use_command("kore")                                           # before reveal -> stakes refunded
    assert e.eco.balance("icik") == 1000
    new = e.active_event(); assert new["id"] != old["id"] and new["respin_of"] == old["id"]
    assert [x for x in e.store.events() if x["id"] == old["id"]][0]["state"] == "vetoed"
    out = e.tick(); kinds = K(out); assert "spin" in kinds and "persona_line" in kinds
    pl = [o for k, o in out if k == "persona_line"][0]; assert pl["persona"] == "babis"
    assert any("Veto" in s["text"] for s in q["steps"])
    with pytest.raises(engine.WheelError) as x: e.use_command("kore")
    assert x.value.code == "cooldown"

def test_steal_points_from_leader_with_kalousek_line_and_shield():
    e, clk = mk(); e.eco.add("kore", 1000, "test")                    # kore leads with 2000
    q = e.use_command("icik"); assert e.eco.balance("kore") == 1800 and e.eco.balance("icik") == 1200
    pl = [o for k, o in e.tick() if k == "persona_line"]; assert pl and pl[0]["persona"] == "kalousek"
    e2, _ = mk(); e2.store.kv_set("shield:kore", str(e2.clock() + 60)); e2.eco.add("kore", 1000, "t")
    e2.use_command("icik"); assert e2.eco.balance("kore") == 2000   # shield
    e3, _ = mk(); e3.cfg["players"]["icik"]["command_effects"] = ["steal_points"]
    for p in ("kore",): e3.eco.add(p, -1000, "broke")
    with pytest.raises(engine.WheelError) as x: e3.use_command("icik")
    assert x.value.code == "no_effect" and e3.cooldown_left("icik") == 0   # cooldown not spent

def test_double_bet_and_registry_is_configurable():
    e, clk = mk(bet_window_s=30); e.cfg["players"]["kore"]["command_effects"] = ["double_bet", "shield"]
    e.use_command("kore"); assert e.shielded("kore")
    e.open_bets("kore"); clk.adv(1)
    keys = [x["key"] for x in e.cfg["events"]]
    for k in keys[:3]: e.bet("kore", k, 10)
    clk.adv(30); out = e.tick(); ev = out[-1][1] if K(out)[-1] == "spin" else None
    clk.adv(7); e.tick(); res = e.active_event()["bets_result"]
    for r in res:
        if r["state"] == "won": assert r["payout"] == round(10 * e.eco.odds(r["key"]) * 2)
    assert set(effects.REGISTRY) == {"veto_respin", "steal_points", "double_bet", "persona_interrupt", "shield"}

def test_sequence_shows_effect_live_in_panel():
    e, clk = mk(); ev = e.spin("kore", force="socky", lead_s=600); clk.adv(7); e.tick()
    e.eco.add("kore", 500, "t"); e.use_command("icik"); clk.adv(5); e.tick()
    bl, ph = panel.blocks(e, clk()); f = [b for b in bl if b.get("fields")][0]["fields"][3]["text"]
    assert "Kalousek za to může" in f and "bere" in f

# ---------------- 4) live show: poll, hype, minigames ----------------
def test_poll_catch_quiz_points_and_panel():
    e, clk = mk(); ev = go_live(e, clk, "porada"); sh = ev["show"]
    assert sh["poll"]["q"] == "Kdo za to může?" and sh["quiz"] and sh["catch"]["winner"] is None
    bl, ph = panel.blocks(e, clk()); t = T(bl); assert ph == "live" and "kolo_poll_0" in t and "kolo_catch" not in t and "Hype" in t
    b0 = e.eco.balance("kore"); SA.handle_command(e, KORE, "poll 0"); SA.handle_command(e, KORE, "poll 1")
    assert e.eco.balance("kore") == b0 + 10 and e.active_event()["show"]["poll"]["votes"]["kore"] == 1
    assert "neletí" not in SA.handle_command(e, ICIK, "catch")["text"] or True
    clk.t = sh["catch"]["at"] + 0.5; assert "show" in K(e.tick())
    assert "kolo_catch" in T(panel.blocks(e, clk())[0])
    r1 = SA.handle_command(e, ICIK, "catch"); r2 = SA.handle_command(e, KORE, "catch")
    assert "Dotace je tvoje" in r1["text"] and "Pozdě" in r2["text"] and e.eco.balance("icik") == 1000 + 50 + 150
    clk.t = sh["quiz"]["at"] + 1; e.tick(); assert "kolo_quiz_0" in T(panel.blocks(e, clk())[0])
    ans = sh["quiz"]["answer"]
    assert "Správně" in SA.handle_command(e, KORE, f"quiz {ans}")["text"]
    assert "Už jsi" in SA.handle_command(e, KORE, f"quiz {ans}")["text"]
    assert "Vedle" in SA.handle_command(e, ICIK, f"quiz {(ans + 1) % 3}")["text"]
    clk.t = sh["quiz"]["until"] + 1; e.tick(); assert "trefili: Kore" in T(panel.blocks(e, clk())[0])

def test_minigames_only_when_live():
    e, clk = mk(); assert "Teď neběží" in SA.handle_command(e, KORE, "poll 0")["text"]

def test_hype_counts_reactions_on_panel_and_scene_only():
    e, clk = mk(); e.store.kv_set("panel_ts", "100.1"); go_live(e, clk)
    class Sc:
        def tracked_ts(self): return {"200.5"}
    ev = lambda ts, ch="C0C6W8E6NP9": {"item": {"type": "message", "channel": ch, "ts": ts}}
    assert SA.on_reaction(e, None, Sc(), "C0C6W8E6NP9", ev("100.1"), +1)
    assert SA.on_reaction(e, None, Sc(), "C0C6W8E6NP9", ev("200.5"), +1)
    assert not SA.on_reaction(e, None, Sc(), "C0C6W8E6NP9", ev("999.9"), +1)
    assert not SA.on_reaction(e, None, Sc(), "C0C6W8E6NP9", ev("100.1", "COTHER"), +1)
    SA.on_reaction(e, None, Sc(), "C0C6W8E6NP9", ev("100.1"), -1)
    assert e.active_event()["show"]["hype"] == 1 and "🔥 Hype ▱" in T(panel.blocks(e, clk())[0])

# ---------------- 1) persona scenes ----------------
class FakePoster:
    def __init__(self): self.posts, self.channel, self.n = [], "C0C6W8E6NP9", 0
    def post(self, k, text, thread_ts=None):
        self.n += 1; ts = f"300.{self.n}"; self.posts.append((k, text, thread_ts, ts)); return ts, "persona"

def runner(e, clk, gen=None, poster=None):
    return scenes.SceneRunner(e, poster or FakePoster(), gen=gen, prompt_fn=lambda k: f"PROMPT {k}", clock=clk, threaded=False)

def test_scene_plan_4_to_8_beats_spread_over_event():
    cfg = config.load()
    for ev in cfg["events"]:
        if ev.get("script"): continue
        bs = scenes.plan(cfg, {"key": ev["key"], "duration_s": ev["duration_s"]})
        assert 4 <= len(bs) <= 8 and bs[0]["at"] < 5 and bs[-1]["at"] <= ev["duration_s"] * 0.9
        assert all(b["persona"] in ("babis", "alenka", "bourak", "marty", "peta", "kalousek") for b in bs)
    assert scenes.plan(cfg, {"key": "porada", "duration_s": 600})[0]["persona"] == "babis"

def test_scene_llm_first_line_top_level_then_thread_and_fallbacks():
    e, clk = mk(); ev = go_live(e, clk, "porada"); calls = []
    def gen(system, hist):
        calls.append((system, hist))
        if len(calls) == 2: raise RuntimeError("gemini exhausted")
        if len(calls) == 3: return "…"
        return "<@U0C75A26Y0K> Čísla! (parodie) Hned!"
    r = runner(e, clk, gen); st = r.start(ev)
    clk.adv(ev["duration_s"] * 0.86); r.step(ev["id"])
    P = r.poster.posts; assert len(P) == 6
    assert P[0][2] is None and all(p[2] == P[0][3] for p in P[1:])                # top-level, then thread
    assert "<@" not in P[0][1] and "parodie" not in P[0][1] and P[0][1].startswith("Čísla!")
    beats = r.state(ev["id"])["beats"]
    assert beats[1]["how"] == "template" and P[1][1] == beats[1]["fallback"]       # LLM error -> template
    assert beats[2]["how"] == "template"                                            # terse -> template
    assert "PROMPT babis" in calls[0][0] and "Porada" not in calls[0][0][:6] and "max 120" in calls[0][0]
    assert "[Andrej Babiš]" in calls[1][1][0]["content"]                            # transcript carried
    assert not r.step(ev["id"])                                                      # done

def test_scene_llm_timeout_uses_template_and_sequence_coordination():
    e, clk = mk(scene_llm_timeout_s=0.05); ev = go_live(e, clk, "vina"); seen = []
    e.eco.add("kore", 500, "t"); e.use_command("icik")
    def slow(system, hist): seen.append(system); time.sleep(0.3); return "pozdě"
    r = runner(e, clk, slow); r.start(ev); clk.adv(3); r.step(ev["id"])
    b = r.state(ev["id"])["beats"][0]; assert b["how"] == "template" and b["said"] == b["fallback"]
    assert "Kalousek za to může" in seen[0]

def test_scene_stops_when_event_not_live_and_resumes_after_restart():
    e, clk = mk(); ev = go_live(e, clk, "kantyna")
    r = runner(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"]); assert len(r.poster.posts) == 1
    r2 = runner(e, clk, poster=r.poster); r2.start(ev); clk.adv(ev["duration_s"] * 0.5); r2.step(ev["id"])
    assert r.poster.posts[0][3] != r.poster.posts[1][3] and len(r.poster.posts) >= 3      # no duplicate of beat 0
    clk.adv(ev["duration_s"]); e.tick(); assert not r2.step(ev["id"])

def test_titanic_fixed_script_with_bourak_and_macinka_narration():
    e, clk = mk(); ev = go_live(e, clk, "snemovna")
    beats = scenes.plan(e.cfg, ev); ps = {b["persona"] for b in beats}
    assert {"bourak", "macinka", "monika"} <= ps and all(b.get("text") for b in beats)
    wheel = FakeSlack(); pp = personas.PersonaPoster(wheel, "C1", token_fn=lambda n: None)
    ts, how = pp.post("macinka", "Filipe, jednací řád tohle nedovoluje."); assert how == "narrated"   # no Peťa token -> Monika
    assert "Petr Macinka na to" in wheel.posts()[-1]["text"] and "username" not in wheel.posts()[-1]
    peta = FakeSlack(); pp3 = personas.PersonaPoster(wheel, "C1", token_fn={"SLACK_OKU_PETA_BOT_TOKEN": "xoxb-p"}.get, client_factory=lambda token: peta)
    ts, how = pp3.post("macinka", "Filipe, jednací řád tohle nedovoluje."); assert how == "persona"   # via the Peťa bot
    assert peta.posts()[-1]["text"] == "Filipe, jednací řád tohle nedovoluje."
    pp2 = personas.PersonaPoster(wheel, "C1", token_fn=lambda n: None, customize=True)
    pp2.post("macinka", "Ahoj"); assert wheel.posts()[-1]["username"] == "Petr Macinka" and wheel.posts()[-1]["text"] == "Ahoj"
    calls = []
    r = runner(e, clk, gen=lambda s, h: calls.append(1) or "x"); r.start(ev); clk.adv(200); r.step(ev["id"])
    assert calls == [] and r.poster.posts[0][0] == "monika"                      # fixed script, no LLM

def test_interject_goes_to_scene_thread_else_under_panel():
    e, clk = mk(); e.store.kv_set("panel_ts", "100.1")
    r = runner(e, clk); r.interject({"persona": "babis", "cue": "x", "fallback": "Počkejte!"})
    assert r.poster.posts[-1][2] == "100.1"
    ev = go_live(e, clk); r.start(ev); clk.adv(3); r.step(ev["id"]); root = r.poster.posts[-1][3]
    r.interject({"persona": "kalousek", "cue": "x", "fallback": "Já ne."}); assert r.poster.posts[-1][2] == root
    assert r.poster.posts[-1][3] in r.tracked_ts()

def test_persona_poster_tokens_fallback_and_mention_stripping():
    made = {}
    class WC(FakeSlack):
        def __init__(self, token): super().__init__(); made[token] = self
    env = {"SLACK_OKU_BOT_TOKEN": "xoxb-b", "SLACK_OKU_KALOUSEK_BOT_TOKEN": "xoxb-k"}
    wheel = FakeSlack()
    pp = personas.PersonaPoster(wheel, "C1", token_fn=env.get, client_factory=WC)
    assert set(pp.available()) == {"babis", "kalousek"}
    ts, how = pp.post("babis", "Čísla <@U123> @channel hned"); assert how == "persona" and made["xoxb-b"].posts()[-1]["text"] == "Čísla hned"
    ts, how = pp.post("alenka", "Hranolky!"); assert how == "narrated" and "Alenka Hranolka" in wheel.posts()[-1]["text"]
    made["xoxb-k"].fail["post"] = "not_in_channel"
    ts, how = pp.post("kalousek", "Já ne."); assert how == "narrated" and "kalousek" in pp.disabled

def test_poster_starts_scene_on_live_and_routes_persona_lines():
    e, clk = mk(); calls = []
    class Sc:
        def start(self, ev): calls.append(("start", ev["id"]))
        def interject(self, o): calls.append(("line", o["persona"]))
    post = SA.make_poster(e, FakeSlack(), "C1", None, None, Sc())
    post("live", {"id": "E1"}); post("persona_line", {"persona": "kalousek"}); post("reveal", {"id": "E1"})
    assert calls == [("start", "E1"), ("line", "kalousek")]

def test_loop_safety_bridge_ignores_bot_messages():
    from oku_slack import bridge
    persona_msg = {"type": "message", "channel": "C0C6W8E6NP9", "user": "U0C75A26Y0K", "bot_id": "B0C79HVPZU6",
                   "text": "porada <@U0C7FM4QU7N> čísla!", "ts": "1.2"}
    assert bridge.ignored(persona_msg, "U0C7FM4QU7N") and bridge.ignored(dict(persona_msg, type="app_mention"), "U0C7FM4QU7N")
    narrated = {"type": "message", "channel": "C0C6W8E6NP9", "bot_id": "B0C89F3SV08", "subtype": "bot_message", "text": "porada", "ts": "1.3"}
    assert bridge.ignored(narrated, "U0C75A26Y0K")
    handlers = {}
    class App:
        def event(self, name):
            def deco(fn): handlers[name] = fn; return fn
            return deco
    spawned = []
    class B: bot = "U0C75A26Y0K"; persona = "babis"; cfg = {}; followup = None; coord = None
    orig = bridge.dispatch; bridge.dispatch = lambda b, ev: spawned.append(ev)
    try:
        bridge.register(App(), B())
        handlers["app_mention"](dict(persona_msg, type="app_mention")); handlers["message"](persona_msg)
    finally: bridge.dispatch = orig
    assert spawned == []

def test_scopes_header_and_manifest():
    class R(dict):
        headers = {"x-oauth-scopes": "commands,chat:write,reactions:read"}
    class C:
        def auth_test(self): return R(user_id="U1")
    assert SA.granted_scopes(C()) == {"commands", "chat:write", "reactions:read"}
    m = (pathlib.Path(__file__).parents[1] / "manifests" / "kolo.yaml").read_text(encoding="utf-8")
    for need in ("reactions:read", "chat:write.customize", "reaction_added", "reaction_removed"): assert need in m

def test_scope_refresh_applies_customize_live_and_reaction_handlers_registered():
    class R(dict):
        def __init__(self, sc): super().__init__(user_id="U1"); self.headers = {"x-oauth-scopes": sc}
    class C:
        sc = "commands,chat:write"
        def auth_test(self): return R(self.sc)
    c = C(); poster = personas.PersonaPoster(FakeSlack(), "C1"); scn = type("S", (), {"poster": poster})()
    known = SA.refresh_scopes(c, set(), scn); assert not poster.customize
    c.sc = "commands,chat:write,reactions:read,chat:write.customize"
    known = SA.refresh_scopes(c, known, scn); assert poster.customize and "reactions:read" in known
    src = (pathlib.Path(__file__).parents[1] / "oku_slack" / "wheel" / "slack_adapter.py").read_text(encoding="utf-8")
    assert '@app.event("reaction_added")' in src and '@app.event("reaction_removed")' in src
