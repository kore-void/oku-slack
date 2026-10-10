"""Persona scenes: when an event goes live, the hosting persona(s) act it out in the wheel channel.
Kore's rule: the first beat is a short punchy top-level line in the channel; the rest go into its thread.
Lines are generated with the SAME machinery as the persona bridge (oku_slack.core.build_prompt + core.generate,
i.e. the persona prompt files + Gemini keys shared with Umbra), one call per beat, just in time, with the scene
transcript so far and any running charged-command sequence. LLM unavailable/slow/empty -> templated fallback.
The legendary Titanic scene keeps its fixed script (no LLM). State persists in kv scene:<event_id> (resume after restart).
Porada (scenes.toml meeting = true, settings.meeting_handoff): only the opener is scripted. It becomes the thread root and
the wheel asks the persona bridge (oku_slack.handoff, local files) to run a REAL porada (oku_slack.meeting) in that thread.
The remaining scripted beats are dropped once the bridge acks 'started'; they are only a fallback when the bridge answers
dup/stale/error or does not answer within settings.meeting_handoff_timeout_s (~30 s). Live scene threads are published
to logs/outbox/live_threads.json so the bridge can route plain human replies there."""
import json, logging, re, threading, time
from .. import handoff

log = logging.getLogger("oku_wheel.scenes")
SCENE_RULES = ("Hraješ krátkou scénku ve Slack kanálu #oku-porada; právě běží událost z kola štěstí OKÚ. "
               "Situace: {premise} Tvůj tah: {cue} "
               "Pravidla: {length}, mluv jen za sebe, žádné @zmínky, žádné uvozovky kolem repliky, nepiš své jméno na začátek.")
TITANIC_CAST = {"turek": "bourak", "marty": "marty", "monika": "monika", "macinka": "macinka"}
TERSE = {"…", "...", ".", "", "-"}

def clean(text, name=""):
    t = (text or "").strip().strip('"„“”').strip()
    t = re.sub(r"<[@!#][^>]*>", "", t)
    t = re.sub(r"\(\s*(parodie|satira|parody|satire)\s*\)", "", t, flags=re.I)
    first = (name or "").split(" ")[0]
    if first: t = re.sub(r"^\**\s*" + re.escape(first) + r"[^:\n]{0,25}:\**\s*", "", t)
    return t.strip()[:500]

GAP_S = 20.0  # default gap between scene lines (players.toml settings.scene_gap_s); was ~100 s (spread over the event)

def schedule(n, duration, start=2.0, gap=GAP_S, span=0.85):
    """Beat offsets from live: a fixed short gap (~15-25 s) so the scene reads as a conversation; squeezed only
    when the event is too short to fit (last beat by duration * span)."""
    if n <= 1: return [start]
    gap = min(float(gap), max(1.0, (duration * span - start) / (n - 1)))
    return [round(start + i * gap, 1) for i in range(n)]

def plan(cfg, e):
    """Beats for an event: [{"at": s from live, "persona", "cue", "fallback", "text"(fixed)}]."""
    if e.get("script"):
        sc = cfg.get("scripts", {}).get(e["script"]) or {}
        out = []
        for b in sc.get("beats", []):
            for i, (sp, tx) in enumerate(b.get("lines", [])):
                out.append({"at": float(b["at"]) + 4 * i, "persona": TITANIC_CAST.get(sp, "monika"), "text": tx})
        return out
    sc = cfg.get("scenes", {}).get(e["key"]) or {}
    beats = list(sc.get("beats", []))[:8]
    return [{"at": at, "persona": b["persona"], "cue": b.get("cue", ""), "fallback": b.get("fallback", ""), "premise": sc.get("premise", "")}
            for at, b in zip(schedule(len(beats), float(e["duration_s"]), gap=(cfg.get("settings") or {}).get("scene_gap_s", GAP_S)), beats)]

class SceneRunner:
    def __init__(self, eng, poster, gen=None, prompt_fn=None, clock=time.time, sleep=time.sleep, threaded=True):
        self.eng, self.poster, self.clock, self.sleep, self.threaded = eng, poster, clock, sleep, threaded
        self.gen, self.prompt_fn = gen, prompt_fn
        self.lock = threading.RLock(); self.running = set()

    # ---------- persistence ----------
    def _key(self, eid): return f"scene:{eid}"
    def state(self, eid):
        v = self.eng.store.kv_get(self._key(eid)); return json.loads(v) if v else None
    def _save(self, st): self.eng.store.kv_set(self._key(st["event_id"]), json.dumps(st, ensure_ascii=False))

    def tracked_ts(self):
        e = self.eng.active_event()
        if not e: return set()
        st = self.state(e["id"]) or {}; v = self.eng.store.kv_get(f"scene_extra:{e['id']}")
        return {b["ts"] for b in st.get("beats", []) + (json.loads(v) if v else []) if b.get("ts")}

    # ---------- generation ----------
    def line(self, persona, cue, fallback, premise="", transcript=(), top=False):
        if not (self.gen and self.prompt_fn and self.eng.s.get("scene_llm", True)) or persona == "monika":
            return fallback, "template"
        from .personas import ALIAS
        try: system = self.prompt_fn(ALIAS.get(persona, persona))
        except Exception as ex: log.warning("persona prompt %s: %s", persona, type(ex).__name__); return fallback, "template"
        if not system: return fallback, "template"
        seq = [q for q in self.eng.store.sequences() if q["state"] == "running"]
        extra = ""
        if seq:
            q = seq[-1]; fx = "; ".join(r["text"] for r in q.get("effects", []) if r.get("ok"))
            extra = f" Zrovna běží nabitý příkaz {q['label']} ({fx or 'efekt'}), můžeš na to krátce narazit."
        system += "\n\n" + SCENE_RULES.format(premise=premise, cue=cue + extra,
                                              length="jedna úderná věta, max 120 znaků" if top else "1-2 krátké věty, max 220 znaků")
        hist = "\n".join(f"[{n}] {t}" for n, t in transcript) or "(scéna začíná)"
        box = {}
        def run():
            try:
                from .. import usage
                usage.set_context(persona=persona, channel=self.poster.channel, kind="wheel_scene")
            except Exception: pass
            try: box["t"] = self.gen(system, [{"role": "user", "content": f"Dosavadní průběh scény:\n{hist}\n\nTeď ty."}])
            except Exception as ex: box["err"] = type(ex).__name__
        th = threading.Thread(target=run, daemon=True); th.start(); th.join(float(self.eng.s.get("scene_llm_timeout_s", 25)))
        from .personas import NAMES
        t = clean(box.get("t"), NAMES.get(persona, ""))
        if th.is_alive() or box.get("err") or t in TERSE:
            log.info("scene line %s: template (%s)", persona, "timeout" if th.is_alive() else box.get("err") or "empty")
            return fallback, "template"
        return t, "llm"

    # ---------- scene lifecycle ----------
    def start(self, e):
        with self.lock:
            st = self.state(e["id"])
            if st is None:
                st = {"event_id": e["id"], "root_ts": None, "beats": plan(self.eng.cfg, e), "done": False,
                      "handoff": {"state": "pending"} if self.wants_meeting(e) else None}
                self._save(st)
            if st["done"] or e["id"] in self.running: return st
            self.running.add(e["id"])
        if self.threaded: threading.Thread(target=self._loop, args=(e["id"],), daemon=True, name=f"scene-{e['id']}").start()
        return st

    def _loop(self, eid):
        try:
            while True:
                if not self.step(eid): break
                self.sleep(1.0)
        except Exception as ex: log.error("scene loop: %s", type(ex).__name__)
        finally: self.running.discard(eid)

    def step(self, eid):
        """Post every due beat. Returns False when the scene is over (all posted/handed off or event not live)."""
        st = self.state(eid)
        if not st or st["done"]: return False
        e = next((x for x in self.eng.store.events() if x["id"] == eid), None)
        if not e or e["state"] != "live":
            st["done"] = True; self._save(st); self._unpublish(st); return False
        now = self.clock() - e["live_at"]
        ho = st.get("handoff")
        if ho and ho["state"] == "requested": self._check_handoff(st, e, now)
        if st["done"]: return False
        for b in st["beats"]:
            if b.get("posted") or b.get("skipped") or b["at"] > now: continue
            if ho and ho["state"] in ("pending", "requested") and st["root_ts"]: break  # porada: hold beats for the bridge
            transcript = [(self._name(x["persona"]), x["said"]) for x in st["beats"] if x.get("said")]
            top = st["root_ts"] is None
            text, how = (b["text"], "script") if b.get("text") else self.line(b["persona"], b["cue"], b["fallback"], b.get("premise", ""), transcript, top)
            ts, via = self.poster.post(b["persona"], text, thread_ts=st["root_ts"])
            b.update(posted=True, said=text, ts=ts, how=how, via=via)
            if top and ts:
                st["root_ts"] = ts
                if ho and ho["state"] == "pending": self._request_meeting(st, e)
                else: self._publish(st, e, skit_running=True)
            self._save(st)
        if all(b.get("posted") or b.get("skipped") for b in st["beats"]):
            st["done"] = True; self._save(st); self._publish(st, e, skit_running=False); return False
        return True

    # ---------- porada -> real bot meeting (oku_slack.handoff) ----------
    def wants_meeting(self, e):
        if e.get("script") or not self.eng.s.get("meeting_handoff", True): return False
        return bool((self.eng.cfg.get("scenes", {}).get(e.get("key")) or {}).get("meeting"))

    def _request_meeting(self, st, e):
        ho = st["handoff"]; sc = self.eng.cfg.get("scenes", {}).get(e["key"]) or {}
        try:
            req = handoff.request_meeting(self.poster.channel, st["root_ts"], event_id=e["id"], topic=sc.get("premise") or e.get("title", ""),
                                          host=e.get("host") or "babis", clock=self.clock)
            ho.update(state="requested", id=req["id"], at=self.clock())
            log.info("porada %s: real meeting requested in thread %s (req %s)", e["id"], st["root_ts"], req["id"])
            self._publish(st, e, skit_running=False)
        except Exception as ex:
            log.warning("porada %s: meeting request failed: %s (scripted fallback)", e["id"], type(ex).__name__)
            self._fallback(st, e, self.clock() - e["live_at"], "request_failed")

    def _check_handoff(self, st, e, now):
        ho = st["handoff"]
        try: a = handoff.ack_for(ho["id"])
        except Exception as ex: log.warning("porada ack read failed: %s", type(ex).__name__); a = None
        if a and a.get("status") == "started":
            ho.update(state="meeting", acked_at=self.clock())
            for b in st["beats"]:
                if not b.get("posted"): b["skipped"] = True
            st["done"] = True; self._save(st); self._publish(st, e, skit_running=False)
            log.info("porada %s: bridge runs the real meeting in %s; scripted beats dropped", e["id"], st["root_ts"])
        elif a: self._fallback(st, e, now, a.get("status") or "rejected")
        elif self.clock() - float(ho.get("at") or 0) > float(self.eng.s.get("meeting_handoff_timeout_s", 30)):
            self._fallback(st, e, now, "timeout")

    def _fallback(self, st, e, now, why):
        """Bridge did not take it: play the remaining scripted beats from now, scene_gap_s apart."""
        st["handoff"].update(state="fallback", reason=why)
        gap = float(self.eng.s.get("scene_gap_s", GAP_S))
        rest = [b for b in st["beats"] if not b.get("posted")]
        for i, b in enumerate(rest): b["at"] = round(now + 1 + i * gap, 1)
        self._save(st); self._publish(st, e, skit_running=True)
        log.info("porada %s: no real meeting (%s); scripted fallback, %d beat(s)", e["id"], why, len(rest))

    def _publish(self, st, e, skit_running):
        if not st.get("root_ts"): return
        try:
            live = handoff.read_live_threads(clock=self.clock)
            live[st["root_ts"]] = {"channel": self.poster.channel, "event_id": e["id"], "key": e.get("key"), "host": e.get("host") or "babis",
                                   "until": e.get("end_at") or (self.clock() + float(e.get("duration_s") or 600)), "skit_running": bool(skit_running)}
            handoff.write_live_threads(live, clock=self.clock)
        except Exception as ex: log.warning("live threads write failed: %s", type(ex).__name__)

    def _unpublish(self, st):
        if not st.get("root_ts"): return
        try:
            live = handoff.read_live_threads(clock=self.clock)
            if live.pop(st["root_ts"], None) is not None: handoff.write_live_threads(live, clock=self.clock)
        except Exception as ex: log.warning("live threads write failed: %s", type(ex).__name__)

    def end(self, e):
        """Event done/vetoed/expired: the thread stops being a live wheel thread for the bridge."""
        st = self.state(e["id"]) if e and e.get("id") else None
        if st: self._unpublish(st)

    def _name(self, k):
        from .personas import NAMES
        return NAMES.get(k, k)

    def interject(self, obj):
        """One persona line now (charged-command effect): into the live scene thread, else under the panel."""
        def run():
            e = self.eng.active_event(); thread = None
            st = self.state(e["id"]) if e and e["state"] == "live" else None
            if st and st.get("root_ts"): thread = st["root_ts"]
            else: thread = self.eng.store.kv_get("panel_ts")
            transcript = [(self._name(x["persona"]), x["said"]) for x in (st or {}).get("beats", []) if x.get("said")][-6:]
            premise = ((self.eng.cfg.get("scenes", {}).get(e["key"]) or {}).get("premise", "") if e else "") or "Kolo štěstí OKÚ."
            text, how = self.line(obj["persona"], obj.get("cue", ""), obj.get("fallback", ""), premise, transcript)
            ts, via = self.poster.post(obj["persona"], text, thread_ts=thread)
            if e and ts:  # separate key: never races with the beat loop's saves
                with self.lock:
                    k = f"scene_extra:{e['id']}"; v = self.eng.store.kv_get(k); xs = json.loads(v) if v else []
                    xs.append({"persona": obj["persona"], "said": text, "ts": ts, "how": how, "via": via, "kind": obj.get("kind")})
                    self.eng.store.kv_set(k, json.dumps(xs, ensure_ascii=False))
            return ts
        if self.threaded: threading.Thread(target=run, daemon=True).start(); return None
        return run()
