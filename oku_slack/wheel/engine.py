"""Pure wheel logic. All time comes from an injectable clock; all randomness from an injectable RNG.
tick() advances the state machine and returns notifications for the Slack adapter / room hub."""
import random, string, time, uuid

# Event states: pending -> (all confirmed) ready -> (start_at reached) live -> done
#               pending -> (start_at + confirm_wait_s, not all confirmed) expired
# Event start = max(start_at, moment of last confirmation): it never starts without ALL players.
SEQ_STEPS = [(0, "Nabíjím..."), (30, "Čau lidi!"), (60, "Kampaň běží"), (90, "Finále"), (110, "Dojezd")]
CODE_ALPHABET = "ABCDEFHJKLMNPRSTUVXYZ2345678"  # no 0/O/1/I/G/Q/W confusion

class WheelError(Exception):
    def __init__(self, code, msg=""): super().__init__(msg or code); self.code = code

class Engine:
    def __init__(self, cfg, store, clock=time.time, rng=None):
        self.cfg, self.s, self.store, self.clock = cfg, cfg["settings"], store, clock
        self.rng = rng or random.SystemRandom()
        self.holds = {}  # (event_id, player) -> start ts (server time)

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

    def missing(self, e):
        return [p for p in self.players() if p not in e["confirmed"]]

    def pick(self, force=None):
        evs = self.cfg["events"]
        if force is not None:
            if not self.s.get("allow_force"): raise WheelError("force_disabled", "Vynucení je vypnuté.")
            for i, x in enumerate(evs):
                if x["key"] == force: return i
            raise WheelError("unknown_event_key", force)
        return self.rng.choices(range(len(evs)), weights=[e["weight"] for e in evs])[0]

    def spin(self, by, lead_s=None, force=None):
        self._player(by)
        if self.active_event(): raise WheelError("event_active", "Už běží jiná událost.")
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
             "revealed": False, "nagged": False, "beat": None, "forced": force is not None}
        self.host_say("legendary" if e["legendary"] else "spin", e)
        self.store.put_event(e)
        return e

    # ---------- confirmation (deliberate only) ----------
    def _confirm(self, e, p, how):
        if e["state"] not in ("pending",): raise WheelError("not_confirmable", e["state"])
        e["confirmed"][p] = {"at": self.clock(), "how": how}
        if set(e["confirmed"]) >= set(self.players()): e["state"] = "ready"
        self.store.put_event(e)
        return e

    def confirm_code(self, eid, p, code):
        self._player(p); e = self._event(eid)
        if (code or "").strip().upper() != e["code"]: raise WheelError("bad_code", "Špatný kód.")
        return self._confirm(e, p, "code")

    def hold_start(self, eid, p):
        self._player(p); self._event(eid)
        self.holds[(eid, p)] = self.clock()
        return self.holds[(eid, p)]

    def hold_end(self, eid, p):
        """Server-side measured hold; client timestamps are ignored."""
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
        left = self.cooldown_left(p)
        if left > 0: raise WheelError("cooldown", f"Nabíjí se, zbývá {int(left // 60)}:{int(left % 60):02d}.")
        now = self.clock()
        self.store.set_used(p, now)
        seq = {"id": uuid.uuid4().hex[:8], "player": p, "label": self.cfg["players"][p].get("command", "Příkaz"),
               "start_at": now, "end_at": now + self.s["sequence_s"], "step": 0, "state": "running",
               "steps": [{"at": now + off, "text": t} for off, t in SEQ_STEPS if off < self.s["sequence_s"]]}
        self.store.put_sequence(seq)
        return seq

    # ---------- chat ----------
    def chat(self, p, text):
        text = (text or "").strip()[:500]
        if not text: raise WheelError("empty")
        self.store.add_chat(self.clock(), self._player(p), text)
        return {"ts": self.clock(), "player": p, "text": text}

    # ---------- clock-driven state machine ----------
    def tick(self):
        now, out = self.clock(), []
        for e in self.store.events():
            ch = False
            if not e.get("revealed", True) and now >= e["spin_at"] + self.s["spin_ms"] / 1000:
                e["revealed"] = True; ch = True; self.host_say("result", e); out.append(("reveal", e))
            if e["state"] in ("pending", "ready") and not e["alarm_sent"] and now >= e["start_at"] - self.s["alarm_before_s"]:
                e["alarm_sent"] = True; ch = True; self.host_say("alarm", e); out.append(("alarm", e))
            if e["state"] == "pending" and not e.get("nagged", True) and now >= e["start_at"] - self.s["nag_before_s"] and self.missing(e):
                e["nagged"] = True; ch = True
                self.host_say("nag", e, missing=", ".join(self.cfg["players"][p]["name"] for p in self.missing(e)))
                out.append(("nag", e))
            if e["state"] == "ready" and now >= e["start_at"]:
                e["state"], e["live_at"] = "live", now
                e["end_at"] = now + e["duration_s"]; ch = True; out.append(("live", e))
            elif e["state"] == "pending" and now >= e["start_at"] + self.s["confirm_wait_s"]:
                e["state"] = "expired"; ch = True; self.host_say("expiry", e); out.append(("expired", e))
            elif e["state"] == "live" and now >= e["end_at"]:
                e["state"] = "done"; ch = True; out.append(("done", e))
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
        return {"now": now, "event": e, "settings": {k: self.s[k] for k in ("spin_ms", "hold_min_s")},
                "wheel": [{"key": x["key"], "title": x["title"], "color": x.get("color", "#888")} for x in self.cfg["events"]],
                "players": {k: {"name": v["name"], "cooldown_left": self.cooldown_left(k)} for k, v in self.cfg["players"].items()},
                "sequences": [q for q in self.store.sequences() if q["state"] == "running"],
                "chat": self.store.chat()}
