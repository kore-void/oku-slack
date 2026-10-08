"""Pure wheel logic. All time comes from an injectable clock; all randomness from an injectable RNG.
tick() advances the state machine and returns notifications for the Slack adapter / room hub."""
import json, random, string, threading, time, uuid
from . import economy, effects, show
from ..world import log as world_log, state as world_state

# Event states: pending -> (confirm_quorum players confirmed) ready -> (start_at reached) live -> done
#               pending -> (start_at + confirm_wait_s, quorum not reached) expired
#               pending/ready -> (charged veto_respin) vetoed
# settings.confirm_quorum (decision D1, default 1): how many players must confirm; 0 or >= player count = ALL.
# Other players may still confirm (join) while the event is ready. Event start = max(start_at, quorum moment).
# Every state change and player action is also appended to the world diary (oku_slack.world.log, P-001).
# Betting round (kv "round"): Točit opens a bet_window_s window; when it closes the tick spins the wheel and the
# round's bets are settled at reveal. All public mutations hold self.lock (Slack threads + tick loop).
SEQ_STEPS = [(0, "Nabíjím..."), (30, "Čau lidi!"), (60, "Kampaň běží"), (90, "Finále"), (110, "Dojezd")]
CODE_ALPHABET = "ABCDEFHJKLMNPRSTUVXYZ2345678"  # no 0/O/1/I/G/Q/W confusion

class WheelError(Exception):
    def __init__(self, code, msg=""): super().__init__(msg or code); self.code = code

class Engine:
    def __init__(self, cfg, store, clock=time.time, rng=None, world_jsonl=None):
        self.cfg, self.s, self.store, self.clock = cfg, cfg["settings"], store, clock
        self.rng = rng or random.SystemRandom()
        self.holds = {}  # (event_id, player) -> start ts (server time)
        self.lock = threading.RLock()
        self.outbox = []  # notifications queued by actions/effects; drained by tick()
        self.eco = economy.Economy(cfg, store, clock)
        self.wlog = world_log.WorldLog(store, clock, regime=self.s.get("world_regime", "A_scarce"),
                                       run_id=self.s.get("world_run_id", "oku-world-1"), jsonl=world_jsonl)
        self.world = world_state.World(self.wlog, cfg, store, clock)

    def record(self, type, actor=None, subject=None, payload=None, **kw):
        """Append to the world diary (never raises)."""
        return self.wlog.record(type, actor, subject, payload, **kw)

    def _erec(self, type, e, actor=None, **payload):
        base = {"key": e["key"], "round_id": e.get("round_id"), "host": e.get("host")}
        base.update(payload); return self.record(type, actor, f"event:{e['id']}", base)

    def emit(self, kind, obj): self.outbox.append((kind, obj))

    def persona_name(self, k):
        return {"babis": "Andrej Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa",
                "kalousek": "Kalousek", "monika": "Monika"}.get(k, k)

    def shielded(self, p):
        v = self.store.kv_get(f"shield:{p}")
        return bool(v) and float(v) > self.clock()

    # ---------- helpers ----------
    def players(self): return list(self.cfg["players"])
    def _player(self, p):
        if p not in self.cfg["players"]: raise WheelError("unknown_player", p)
        return p
    def player_by_slack(self, uid):
        for k, v in self.cfg["players"].items():
            if v.get("slack_id") == uid: return k
        return None
    def _event(self, eid):
        for e in self.store.events():
            if e["id"] == eid: return e
        raise WheelError("unknown_event", eid)
    def active_event(self):
        act = [e for e in self.store.events() if e["state"] in ("pending", "ready", "live")]
        return act[-1] if act else None

    # ---------- wheel ----------
    def host_say(self, kind, e, **fmt):
        """Pick a random host (Monika) line for `kind`, store it on the event, return the text."""
        pool = self.cfg.get("host", {}).get("lines", {}).get(kind) or [""]
        vals = {"title": e.get("title", ""), "time": time.strftime("%H:%M", time.localtime(e.get("start_at", 0))),
                "who": self.cfg["players"].get(e.get("spun_by"), {}).get("name", ""), "missing": ""}
        vals.update(fmt)
        text = self.rng.choice(pool).format(**vals)
        e["host_say"] = {"kind": kind, "text": text, "at": self.clock(), "name": self.cfg.get("host", {}).get("name", "")}
        return text

    # ---------- betting round ----------
    def round(self):
        v = self.store.kv_get("round")
        r = json.loads(v) if v else None
        return r if r and r.get("state") == "open" else None

    def _set_round(self, r): self.store.kv_set("round", json.dumps(r) if r else "")

    def open_bets(self, by):
        """Točit with a betting window. Returns the round; the tick spins when it closes."""
        with self.lock:
            self._player(by); self._check_free()
            now = self.clock()
            r = {"id": uuid.uuid4().hex[:8], "by": by, "opened_at": now, "closes_at": now + float(self.s["bet_window_s"]), "state": "open"}
            self._set_round(r)
            self.record("bets.open", by, f"round:{r['id']}", {"window_s": float(self.s["bet_window_s"])})
            return r

    def bet(self, p, key, amount):
        with self.lock:
            self._player(p)
            try: b = self.eco.place(self.round(), p, key, amount)
            except economy.EconomyError as err: raise WheelError(err.code, str(err))
            self.record("bet.placed", p, f"round:{b['round_id']}", {"key": key, "amount": b["amount"], "odds": b["odds"], "all_in": amount == "all"})
            return b

    def _check_free(self):
        a = self.active_event()
        if a: raise WheelError("event_active", self.busy_reason(a))
        r = self.round()
        if r: raise WheelError("betting", f"Kolo je obsazené: běží sázky do {time.strftime('%H:%M:%S', time.localtime(r['closes_at']))}.")

    def busy_reason(self, e):
        """Czech, human explanation why the wheel is blocked and when it frees up."""
        hm = lambda t: time.strftime("%H:%M", time.localtime(t))
        if e["state"] == "pending":
            names = [self.cfg["players"].get(p, {}).get("name", p) for p in self.missing(e)]
            q = self.quorum(); need = q - self.confirmed_count(e)
            who = ", ".join(names) if need >= len(names) else f"stačí {need} z: {', '.join(names)}"
            return f"Kolo je obsazené: čeká se na potvrzení ({who}) do {hm(e['start_at'] + self.s['confirm_wait_s'])}."
        if e["state"] == "ready":
            return f"Kolo je obsazené: *{e['title']}* začíná v {hm(e['start_at'])}."
        end = e.get("end_at")
        return f"Kolo je obsazené: právě běží *{e['title']}*" + (f" do {hm(end)}." if end else ".")

    def missing(self, e):
        return [p for p in self.players() if p not in e["confirmed"]]

    def quorum(self):
        """Confirmations needed for an event to go ready (settings.confirm_quorum; 0 / > players = all players)."""
        n = len(self.players())
        try: q = int(self.s.get("confirm_quorum", 1) or 0)
        except (TypeError, ValueError): q = 0
        return n if q <= 0 else min(q, n)

    def confirmed_count(self, e): return sum(1 for p in self.players() if p in e["confirmed"])
    def quorum_met(self, e): return self.confirmed_count(e) >= self.quorum()

    def pick(self, force=None):
        evs = self.cfg["events"]
        if force is not None:
            if not self.s.get("allow_force"): raise WheelError("force_disabled", "Vynucení je vypnuté.")
            for i, x in enumerate(evs):
                if x["key"] == force: return i
            raise WheelError("unknown_event_key", force)
        return self.rng.choices(range(len(evs)), weights=[e["weight"] for e in evs])[0]

    def spin(self, by, lead_s=None, force=None, round_id=None, respin_of=None):
      with self.lock:
        self._player(by)
        a = self.active_event()
        if a: raise WheelError("event_active", self.busy_reason(a))
        if round_id is None and self.round(): self._check_free()
        evs = self.cfg["events"]
        idx = self.pick(force)
        n = len(evs); seg = 360.0 / n
        # pointer at top (0 deg); segment i spans [i*seg, (i+1)*seg) clockwise; land inside, away from edges
        target = (idx * seg + seg * (0.2 + 0.6 * self.rng.random())) % 360
        now = self.clock()
        e = {"id": uuid.uuid4().hex[:8], "key": evs[idx]["key"], "index": idx, "title": evs[idx]["title"],
             "host": evs[idx].get("host", ""), "spun_by": by, "spin_at": now, "target_angle": round(target, 3),
             "turns": 5, "seed": self.rng.randrange(1 << 30),
             "start_at": now + (self.s["lead_s"] if lead_s is None else lead_s),
             "duration_s": evs[idx]["duration_s"], "music_url": evs[idx]["music_url"],
             "code": "".join(self.rng.choice(CODE_ALPHABET) for _ in range(4)),
             "confirmed": {}, "state": "pending", "alarm_sent": False, "live_at": None, "end_at": None,
             "legendary": bool(evs[idx].get("legendary")), "script": evs[idx].get("script"),
             "revealed": False, "nagged": False, "beat": None, "forced": force is not None,
             "round_id": round_id, "respin_of": respin_of}
        self.host_say("legendary" if e["legendary"] else "spin", e)
        self.store.put_event(e)
        self._erec("wheel.spin", e, by, forced=e["forced"], legendary=e["legendary"], respin_of=respin_of, start_at=e["start_at"])
        return e

    # ---------- confirmation (deliberate only) ----------
    def _confirm(self, e, p, how):
        """pending: counts toward the quorum; ready: a further player joins. Re-confirming is a no-op (no double points)."""
        if e["state"] not in ("pending", "ready"): raise WheelError("not_confirmable", e["state"])
        if p in e["confirmed"]: return e
        e["confirmed"][p] = {"at": self.clock(), "how": how}
        on_time = self.clock() <= e["start_at"]
        if on_time and self.s.get("points_confirm"):
            self.eco.add(p, int(self.s["points_confirm"]), "potvrzení včas", e["id"]); e["confirmed"][p]["points"] = int(self.s["points_confirm"])
        became_ready = e["state"] == "pending" and self.quorum_met(e)
        if became_ready: e["state"] = "ready"
        self.store.put_event(e)
        self._erec("wheel.confirm", e, p, how=how.split(":")[0], on_time=on_time, quorum=self.quorum(), confirmed=self.confirmed_count(e))
        if became_ready: self._erec("wheel.ready", e, None, confirmed=sorted(e["confirmed"]), quorum=self.quorum())
        return e

    def confirm_code(self, eid, p, code):
      with self.lock:
        self._player(p); e = self._event(eid)
        if (code or "").strip().upper() != e["code"]: raise WheelError("bad_code", "Špatný kód.")
        return self._confirm(e, p, "code")

    def hold_start(self, eid, p):
        self._player(p); self._event(eid)
        self.holds[(eid, p)] = self.clock()
        return self.holds[(eid, p)]

    def hold_end(self, eid, p):
      """Server-side measured hold; client timestamps are ignored."""
      with self.lock:
        self._player(p); e = self._event(eid)
        start = self.holds.pop((eid, p), None)
        if start is None: raise WheelError("no_hold", "Nejdřív podrž tlačítko.")
        held = self.clock() - start
        if held < self.s["hold_min_s"]: raise WheelError("hold_too_short", f"Drženo {held:.1f}s, potřeba {self.s['hold_min_s']}s.")
        return self._confirm(e, p, f"hold:{held:.1f}s")

    # ---------- charged command ----------
    def cooldown_left(self, p):
        last = self.store.last_used(self._player(p))
        return 0.0 if last is None else max(0.0, last + self.s["cooldown_s"] - self.clock())

    def use_command(self, p):
      """Charged command: runs the player's configured effects (effects.REGISTRY), then a 2-min sequence
      whose steps narrate the effects live in the panel. Cooldown is spent only if some effect applied."""
      with self.lock:
        left = self.cooldown_left(p)
        if left > 0: raise WheelError("cooldown", f"Nabíjí se, zbývá {int(left // 60)}:{int(left % 60):02d}.")
        pc = self.cfg["players"][p]; label = pc.get("command", "Příkaz")
        ctx = {"label": label, "persona": pc.get("command_persona")}
        results = []
        for name in pc.get("command_effects", []):
            fn = effects.REGISTRY.get(name)
            if fn is None: continue
            r = dict(fn(self, p, ctx)); r["effect"] = name; results.append(r)
        if pc.get("command_effects") and not any(r["ok"] for r in results):
            raise WheelError("no_effect", " ".join(r["text"] for r in results) or "Příkaz teď nemá na co působit.")
        now = self.clock()
        self.store.set_used(p, now)
        self.record("command.used", p, None, {"label": label, "persona": ctx.get("persona"),
                                              "effects": [{"effect": r["effect"], "ok": bool(r["ok"])} for r in results]})
        texts = [r["text"] for r in results if r["ok"]]
        if texts:
            steps = [(0, f"⚡ {label}!")] + [(4 + 26 * i, t) for i, t in enumerate(texts)]
            steps += [(off, t) for off, t in SEQ_STEPS[3:] if off > steps[-1][0]]
        else:
            steps = SEQ_STEPS
        seq = {"id": uuid.uuid4().hex[:8], "player": p, "label": label, "effects": results,
               "start_at": now, "end_at": now + self.s["sequence_s"], "step": 0, "state": "running",
               "steps": [{"at": now + off, "text": t} for off, t in steps if off < self.s["sequence_s"]]}
        self.store.put_sequence(seq)
        return seq

    # ---------- live show (poll, minigames, hype) ----------
    def _live_event(self):
        e = self.active_event()
        if not e or e["state"] != "live": raise WheelError("not_live", "Teď neběží žádná událost.")
        return e

    def vote(self, p, i):
        with self.lock:
            self._player(p); e = self._live_event()
            try: first = show.vote(e, p, int(i))
            except show.ShowError as err: raise WheelError(err.code, str(err))
            if first and self.s.get("points_vote"): self.eco.add(p, int(self.s["points_vote"]), "hlasování", e["id"])
            self.store.put_event(e); self._erec("show.vote", e, p, option=int(i), first=bool(first)); return e["show"]["poll"]

    def catch(self, p):
        with self.lock:
            self._player(p); e = self._live_event()
            try: show.catch(e, p, self.clock())
            except show.ShowError as err: raise WheelError(err.code, str(err))
            self.eco.add(p, int(self.s["points_catch"]), "chycená dotace", e["id"]); self.store.put_event(e)
            self._erec("show.catch", e, p, points=int(self.s["points_catch"]))
            return int(self.s["points_catch"])

    def quiz(self, p, i):
        with self.lock:
            self._player(p); e = self._live_event()
            try: ok = show.quiz_answer(e, p, int(i), self.clock())
            except show.ShowError as err: raise WheelError(err.code, str(err))
            if ok: self.eco.add(p, int(self.s["points_quiz"]), "kvíz", e["id"])
            self.store.put_event(e); self._erec("show.quiz", e, p, ok=bool(ok)); return ok

    def hype(self, delta):
        with self.lock:
            e = self.active_event()
            if not e or e["state"] != "live" or not show.hype(e, delta): return False
            self.store.put_event(e); return True

    def reaction(self, player, name, delta, target):
        """A reaction on a tracked message (panel / live scene): diary row always, hype only while live.
        player: player key or None (someone else in the channel); name: emoji name (no free text)."""
        with self.lock:
            ok = self.hype(delta); e = self.active_event()
            self.record("reaction", player, f"event:{e['id']}" if e else None,
                        {"reaction": (name or "")[:64], "delta": int(delta), "target": target, "hype": ok})
            return ok

    # ---------- chat ----------
    def chat(self, p, text):
        text = (text or "").strip()[:500]
        if not text: raise WheelError("empty")
        self.store.add_chat(self.clock(), self._player(p), text)
        self.record("room.chat", p, None, {"len": len(text)})  # no text in the diary
        return {"ts": self.clock(), "player": p, "text": text}

    # ---------- clock-driven state machine ----------
    def tick(self):
      with self.lock:
        now = self.clock()
        out, self.outbox = list(self.outbox), []
        r = self.round()
        if r and now >= r["closes_at"]:
            r["state"] = "closed"; self._set_round(r)
            try:
                ev = self.spin(r["by"], round_id=r["id"]); out.append(("spin", ev))
            except WheelError:
                n = self.eco.refund(r["id"])
                self.record("bets.refunded", None, f"round:{r['id']}", {"count": n, "reason": "spin_failed"})
        for e in self.store.events():
            ch = False
            if not e.get("revealed", True) and now >= e["spin_at"] + self.s["spin_ms"] / 1000:
                e["revealed"] = True; ch = True; self.host_say("result", e); out.append(("reveal", e))
                self._erec("wheel.reveal", e, None, title=e["title"])
                if e.get("round_id"):
                    mult = {}
                    bettors = {b["player"] for b in self.store.bets(e["round_id"]) if b["state"] == "open"}
                    for p in self.players():  # B5: double_bet is consumed only by a round the player actually bet in
                        if p in bettors and self.store.kv_get(f"double:{p}"):
                            mult[p] = 2; self.store.kv_set(f"double:{p}", "")
                            self.record("effect.double_used", p, f"round:{e['round_id']}", {"event": e["id"]})
                    res = self.eco.settle(e["round_id"], e["key"], mult)
                    e["bets_result"] = [{"player": b["player"], "key": b["key"], "amount": b["amount"], "state": b["state"], "payout": b["payout"]} for b in res]
                    for b in res:
                        self.record("bet.settled", b["player"], f"round:{e['round_id']}", {"key": b["key"], "result": e["key"], "amount": b["amount"],
                                    "state": b["state"], "payout": b["payout"], "mult": mult.get(b["player"], 1), "event": e["id"]})
                    if res: out.append(("settled", e))
            if e["state"] in ("pending", "ready") and not e["alarm_sent"] and now >= e["start_at"] - self.s["alarm_before_s"]:
                e["alarm_sent"] = True; ch = True; self.host_say("alarm", e); out.append(("alarm", e))
                self._erec("wheel.alarm", e, None, confirmed=sorted(e["confirmed"]))
            if e["state"] == "pending" and not e.get("nagged", True) and now >= e["start_at"] - self.s["nag_before_s"] and self.missing(e):
                e["nagged"] = True; ch = True
                self.host_say("nag", e, missing=", ".join(self.cfg["players"][p]["name"] for p in self.missing(e)))
                out.append(("nag", e)); self._erec("wheel.nag", e, None, missing=self.missing(e))
            if e["state"] == "ready" and now >= e["start_at"]:
                e["state"], e["live_at"] = "live", now
                e["end_at"] = now + e["duration_s"]; ch = True
                show.plan(self.cfg, e, self.rng); out.append(("live", e))
                self._erec("wheel.live", e, None, confirmed=sorted(e["confirmed"]), legendary=e.get("legendary", False))
            elif e["state"] == "pending" and now >= e["start_at"] + self.s["confirm_wait_s"]:
                e["state"] = "expired"; ch = True; self.host_say("expiry", e); out.append(("expired", e))
                self._erec("wheel.expired", e, None, confirmed=sorted(e["confirmed"]), missing=self.missing(e), quorum=self.quorum())
            elif e["state"] == "live" and now >= e["end_at"]:
                e["state"] = "done"; ch = True; out.append(("done", e))
                self._erec("wheel.done", e, None, confirmed=sorted(e["confirmed"]), hype=(e.get("show") or {}).get("hype", 0))
            if e["state"] == "live" and show.tick(e, now): ch = True; out.append(("show", e))
            if e["state"] == "live" and e.get("script"):
                b = self.cinematic(e, now)["beat"]
                if b != e.get("beat"): e["beat"] = b; ch = True; out.append(("beat", e))
            if ch: self.store.put_event(e)
        for q in self.store.sequences():
            if q["state"] != "running": continue
            step = sum(1 for st in q["steps"] if now >= st["at"])
            if now >= q["end_at"]:
                q["state"] = "done"; self.store.put_sequence(q); out.append(("seq_done", q))
            elif step != q["step"]:
                q["step"] = step; self.store.put_sequence(q); out.append(("seq_step", q))
        return out

    def cinematic(self, e, now=None):
        """Server-authoritative cinematic state: which scripted beat is on screen at `now`."""
        sc = self.cfg.get("scripts", {}).get(e.get("script") or "")
        if not sc or e.get("state") != "live" or e.get("live_at") is None:
            return {"active": False, "beat": None}
        t = (self.clock() if now is None else now) - e["live_at"]
        idx = None
        for i, b in enumerate(sc["beats"]):
            if t >= b["at"]: idx = i
        nxt = sc["beats"][idx + 1]["at"] if idx is not None and idx + 1 < len(sc["beats"]) else sc.get("duration_s", e["duration_s"])
        return {"active": True, "beat": idx, "elapsed": t, "beat_at": sc["beats"][idx]["at"] if idx is not None else 0,
                "next_at": nxt, "data": sc["beats"][idx] if idx is not None else None}

    def snapshot(self, viewer=None):
        now = self.clock()
        evs = self.store.events()
        e = self.active_event() or (evs[-1] if evs else None)
        if e is not None:
            e = dict(e)
            if viewer is None: e.pop("code", None)  # code only to authenticated room players / Slack ephemeral
            e["cinematic"] = self.cinematic(e, now)
            sc = self.cfg.get("scripts", {}).get(e.get("script") or "")
            if sc: e["script_data"] = {k: sc[k] for k in ("title", "cast", "beats", "credits", "credits_quote", "duration_s") if k in sc}
        return {"now": now, "event": e, "settings": dict({k: self.s[k] for k in ("spin_ms", "hold_min_s")},
                                                         allow_force=bool(self.s.get("allow_force")), confirm_quorum=self.quorum()),
                "wheel": [{"key": x["key"], "title": x["title"], "color": x.get("color", "#888"), "label": x.get("label")} for x in self.cfg["events"]],
                "players": {k: {"name": v["name"], "cooldown_left": self.cooldown_left(k)} for k, v in self.cfg["players"].items()},
                "sequences": [q for q in self.store.sequences() if q["state"] == "running"],
                "round": self.round(), "bets": self.store.bets(self.round()["id"]) if self.round() else [],
                "board": self.eco.board(), "odds": self.eco.all_odds(),
                "chat": self.store.chat()}
