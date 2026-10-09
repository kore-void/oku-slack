"""Meeting mode: ONE coordinator in the process runs a turn-based "porada" across persona bots.
Each turn is generated with the full thread transcript and posted via that persona's own client.
Thread is re-read (conversations_replies) before every turn, so human interjections and
"stop"/"konec" work even without message.channels subscriptions."""
import random, re, threading, time
from collections import OrderedDict
from . import core, usage

STOP_WORDS = ("stop", "konec")
TERSE = {"…", "...", ".", "", "-"}
KALOUSEK_FALLBACK = "Já za nic nemůžu, to jste si podělali sami."
GENERIC_FALLBACK = "Tohle musíme probrat příště, dneska jsem bez čísel."

def _w(text, word): return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", (text or "").lower()) is not None

def is_stop(text): return any(_w(text, w) for w in STOP_WORDS)

def mentioned_personas(text, uid_map):
    """uid_map: {persona: uid}. Ordered by config order."""
    return [k for k, u in uid_map.items() if f"<@{u}>" in (text or "")]

def is_trigger(text, uid_map):
    return len(mentioned_personas(text, uid_map)) >= 2 or _w(text, "porada")

def addressed(text, cfg, uid_map, exclude=None):
    """Personas addressed in text (by <@uid>, @Name or alias), ordered by first occurrence."""
    t = (text or "").lower(); hits = []
    for k, p in cfg["personas"].items():
        if k == exclude: continue
        pos = [t.find(f"<@{uid_map[k].lower()}>")] if k in uid_map else []
        for a in p["aliases"] + [p["name"].lower().split()[0]]:
            m = re.search(r"(?<!\w)@?" + re.escape(a.lower()) + r"(?!\w)", t)
            if m: pos.append(m.start())
        pos = [x for x in pos if x >= 0]
        if pos: hits.append((min(pos), k))
    return [k for _, k in sorted(hits)]

def _last_pos(text, cfg, uid_map, k):
    t = (text or "").lower(); p = cfg["personas"][k]; pos = []
    if k in uid_map: pos.append(t.rfind(f"<@{uid_map[k].lower()}>"))
    for a in p["aliases"] + [p["name"].lower().split()[0]]:
        for m in re.finditer(r"(?<!\w)@?" + re.escape(a.lower()) + r"(?!\w)", t): pos.append(m.start())
    pos = [x for x in pos if x >= 0]
    return max(pos) if pos else -1

def addressed_last(text, cfg, uid_map, exclude=None):
    """Addressed personas ordered by LAST occurrence, latest first (question target is usually last)."""
    hits = [(_last_pos(text, cfg, uid_map, k), k) for k in cfg["personas"] if k != exclude]
    return [k for p, k in sorted(hits, reverse=True) if p >= 0]

def next_speaker(cfg, participants, last_speaker, last_text, rr_index, uid_map=None, blame=False, history=None):
    """Pick next speaker. Prefers the LAST addressed persona (question target), never the current
    speaker, avoids the previous speaker / anyone from the last 2 turns, ensures everyone speaks
    once before anyone's 3rd turn; chair (babis) at most every 3rd turn. Returns (speaker, rr)."""
    uid_map = uid_map or {}; history = list(history or [])
    if last_speaker and (not history or history[-1] != last_speaker): history.append(last_speaker)
    counts = {k: history.count(k) for k in set(participants) | set(history)}
    recent = set(history[-2:])
    def allowed(k):
        if k == last_speaker: return False
        if k == "babis": return "babis" not in recent
        unspoken = [p for p in participants if p != k and p != last_speaker and counts.get(p, 0) == 0]
        return not (counts.get(k, 0) >= 2 and unspoken)
    if blame and "kalousek" in cfg["personas"] and allowed("kalousek") and "kalousek" not in recent:
        return "kalousek", rr_index
    cands = [k for k in addressed_last(last_text, cfg, uid_map, exclude=last_speaker)
             if k in participants or k == "kalousek"]
    for k in cands:
        if allowed(k) and k not in recent: return k, rr_index
    order = [p for p in participants if p != last_speaker] or list(participants)
    if order:
        r = rr_index % len(order); order = order[r:] + order[:r]
    pool = sorted([k for k in order if allowed(k) and k not in recent], key=lambda k: counts.get(k, 0))
    if pool: return pool[0], rr_index + 1
    for k in cands:
        if allowed(k): return k, rr_index
    pool = [k for k in order if k != last_speaker] or order
    return pool[0], rr_index + 1

def strip_reply_mention(text, cfg):
    """Leading '@Name' (reply-to) -> plain 'Name' when another @mention follows (the question target)."""
    m = re.match(r"\s*@(\w+)", text or "")
    if not m or "@" not in text[m.end():]: return text
    w = m.group(1).lower()
    for p in cfg["personas"].values():
        if w in [a.lower() for a in p["aliases"]] + [p["name"].lower().split()[0]]:
            return text[:m.start(1) - 1] + text[m.start(1):]
    return text

MEETING_RULES = (
    "Probíhá PORADA týmu OKÚ ve Slack vlákně. Níže je celý přepis (jména u replik). "
    "Pravidla tvé repliky: reaguj PŘÍMO na posledního mluvčího a oslov ho jménem ve tvaru @Jméno; "
    "pokud ti někdo položil otázku, odpověz na ni; polož přesně jednu otázku konkrétnímu kolegovi (@Jméno); "
    "1-3 věty; kde se hodí, přidej suchý řádek s ‚ziskem‘/KPI (vymyšlená čísla, korporátní tón). "
    "Nepiš za jiné postavy, nepiš své jméno na začátek, žádné uvozovky kolem celé repliky.")
ROLE = {
    "babis": "Jsi předsedající porady. Vyvoláváš lidi jménem, chceš ČÍSLA a zisk, nic tě nezajímá víc než výsledky.",
    "kalousek": "Mluvíš hlavně když jde o vinu nebo tě někdo vyvolá. Klidně stručně, ale vždy aspoň jedna jedovatá, kousavá věta.",
}

class Meeting:
    def __init__(self, coord, channel, thread_ts, participants, source="human", topic=""):
        self.c, self.ch, self.ts = coord, channel, thread_ts
        self.participants = participants
        self.stopped = False
        self.turns = 0
        self.source, self.topic = source, topic  # source: human (@mention) | wheel (oku_wheel hand-off) | world (scheduled)
        self.wake = threading.Event()  # set by a human reply in the thread: next turn comes sooner

    def transcript(self):
        b = self.c.bridges["babis"] if "babis" in self.c.bridges else next(iter(self.c.bridges.values()))
        try:
            msgs = b.client.conversations_replies(channel=self.ch, ts=self.ts, limit=100)["messages"]
        except Exception as e:
            core.log.warning("meeting transcript failed: %s", type(e).__name__); msgs = []
        return msgs

    def label(self, m):
        k = self.c.uid_to_persona.get(m.get("user"))
        if k: return k, self.c.cfg["personas"][k]["name"]
        if m.get("bot_id"): return None, m.get("username") or "bot"
        return None, "Kore (člověk)"

    def humanize(self, text):
        for k, u in self.c.uid_map.items():
            text = (text or "").replace(f"<@{u}>", "@" + self.c.cfg["personas"][k]["name"].split()[0])
        return text

    def run(self, turns=None, sleep=None, rng=random):
        cfg, n = self.c.cfg, turns or rng.randint(8, 12)
        seen_human = set(); last_speaker, rr = None, 0; history = []
        for i in range(n):
            msgs = self.transcript()
            humans = [m for m in msgs if not m.get("bot_id") and m.get("user") not in self.c.uid_to_persona]
            new_h = [m for m in humans if m.get("ts") not in seen_human]
            if i > 0 and any(is_stop(m.get("text")) for m in new_h):
                core.log.info("meeting stop ch=%s ts=%s turn=%d", self.ch, self.ts, i); self.stopped = True; return i
            seen_human.update(m.get("ts") for m in humans)
            last = msgs[-1] if msgs else {"text": ""}
            last_text = last.get("text") or ""
            interjection = i > 0 and bool(new_h)
            final = i == n - 1
            if i == 0 or final: spk = "babis" if "babis" in self.c.bridges else self.participants[0]
            else:
                spk, rr = next_speaker(cfg, self.participants, last_speaker, last_text, rr, self.c.uid_map,
                                       blame=core.is_blame(cfg, self.humanize(last_text).lower()), history=history)
            if spk not in self.c.bridges: spk = next(p for p in self.participants if p in self.c.bridges)
            extra = []
            if i == 0 and self.source == "world":
                extra.append("Poradu jsi právě svolal svou první zprávou (pravidelná ranní porada OKÚ"
                             + (f", téma: {self.topic}" if self.topic else "") + "). Neopakuj úvod: rovnou vyvolej jménem prvního řečníka s požadavkem na čísla.")
            elif i == 0 and self.source == "wheel":
                extra.append("Poradu jsi právě svolal svou první zprávou (událost z kola štěstí OKÚ"
                             + (f": {self.topic}" if self.topic else "") + "). Neopakuj úvod: rovnou vyvolej jménem prvního řečníka s požadavkem na čísla.")
            elif i == 0: extra.append("Zahajuješ poradu: přivítej, shrň téma z první zprávy a vyvolej jménem prvního řečníka s požadavkem na čísla.")
            if final: extra.append("UZAVÍRÁŠ poradu: krátké shrnutí, kdo co slíbil, a finální (vymyšlený) zisk/KPI. Tentokrát žádnou otázku.")
            if interjection: extra.append("Člověk (Kore) právě vstoupil do porady – reaguj nejdřív přímo na jeho poslední zprávu.")
            text = self.c.say(spk, self.ch, self.ts, msgs, extra)
            last_speaker = spk; history.append(spk); self.turns += 1
            if not final: (sleep or self.pause)(rng.uniform(8, 15))
        return n

    def pause(self, d, grace=3.0):
        """Wait d seconds between turns; a human reply (Coordinator.claim sets wake) cuts it to a short grace."""
        if self.wake.wait(d): self.wake.clear(); time.sleep(min(grace, d))

class Coordinator:
    def __init__(self, cfg, gen=core.generate, max_seen=2000):
        self.cfg, self.gen = cfg, gen
        self.bridges, self.uid_map, self.uid_to_persona = {}, {}, {}
        self.seen = OrderedDict(); self.lock = threading.Lock(); self.max_seen = max_seen
        self.active = {}  # (ch, thread_ts) -> Meeting
        self.reporter = None  # usage.Reporter for end-of-meeting summary DM

    def add(self, key, bridge):
        self.bridges[key] = bridge; self.uid_map[key] = bridge.bot; self.uid_to_persona[bridge.bot] = key
        bridge.coord = self

    def claim(self, event):
        """Return 'meeting' (consumed, start), 'dup' (consumed by meeting already), or None (normal)."""
        if event.get("bot_id") or event.get("user") in self.uid_to_persona: return None
        key = (event.get("channel"), event.get("ts"))
        thread = (event.get("channel"), event.get("thread_ts") or event.get("ts"))
        with self.lock:
            if key in self.seen: return "dup" if self.seen[key] else None
            if thread in self.active:  # mid-meeting human message: meeting loop handles it (re-reads the thread)
                self.seen[key] = True; self._trim(); self.active[thread].wake.set(); return "dup"
            trig = is_trigger(event.get("text"), self.uid_map)
            if trig and self.skit_running(*thread):  # P2: a live wheel skit owns this thread -> plain solo reply
                core.log.info("meeting trigger ignored ch=%s ts=%s: wheel skit running", thread[0], thread[1]); trig = False
            self.seen[key] = trig; self._trim()
            if not trig: return None
            parts = mentioned_personas(event.get("text"), self.uid_map)
            if len(parts) < 2: parts = [k for k in self.uid_map if k != "kalousek" or k in parts] 
            m = self.active[thread] = Meeting(self, thread[0], thread[1], parts)
        core.log.info("meeting start ch=%s ts=%s participants=%s", thread[0], thread[1], ",".join(parts))
        return m

    def skit_running(self, ch, thread_ts):
        """True while a scripted wheel skit is still posting in this thread (logs/outbox/live_threads.json)."""
        try:
            from . import handoff
            t = handoff.read_live_threads().get(thread_ts)
            return bool(t and t.get("channel") == ch and t.get("skit_running"))
        except Exception as e:
            core.log.warning("live threads unreadable: %s", type(e).__name__); return False

    def route_plain(self, event):
        """P0-b: a plain human reply (no persona @mention) in a channel THREAD. Returns 'meeting' when a meeting runs
        in that thread (its loop answers; next turn woken), a persona key when it is a live wheel-skit thread (that
        persona answers solo; the event host), else None (unchanged: ignored). Bots never route (loop guard)."""
        if event.get("bot_id") or event.get("subtype") or event.get("user") in self.uid_to_persona: return None
        th, ch = event.get("thread_ts"), event.get("channel")
        if event.get("channel_type") == "im" or not th or th == event.get("ts") or not ch: return None
        if mentioned_personas(event.get("text"), self.uid_map): return None  # app_mention path owns it
        key, thread = (ch, event.get("ts")), (ch, th)
        with self.lock:
            if key in self.seen: return None
            if thread in self.active:
                self.seen[key] = True; self._trim(); self.active[thread].wake.set(); return "meeting"
        try:
            from . import handoff
            live = handoff.read_live_threads().get(th)
        except Exception as e: core.log.warning("live threads unreadable: %s", type(e).__name__); live = None
        if not live or live.get("channel") != ch or not self.bridges: return None
        with self.lock:
            if key in self.seen: return None
            self.seen[key] = True; self._trim()
        host = live.get("host")
        return host if host in self.bridges else ("babis" if "babis" in self.bridges else next(iter(self.bridges)))

    def busy(self, ch):
        """A meeting is running anywhere in this channel (which includes the given thread)."""
        return any(c == ch for c, _ in self.active)

    def start_external(self, req):
        """File hand-off (oku_slack.handoff): real porada in req's channel/thread. Returns started | dup | rejected.
        Wheel requests carry thread_ts (the wheel posted the opener). World requests (source=world, the oku_world
        scheduler) carry no thread: the chair (Babiš) posts `opener` top-level first and the porada runs in its thread;
        then the return value is ("started", {"thread_ts": ts}) so the ack tells the world where it runs.
        No double porada: refused while any meeting runs in that channel or thread."""
        ch, ts = req.get("channel"), req.get("thread_ts")
        world = req.get("source") == "world"
        if not (ch and self.bridges) or not (ts or (world and (req.get("opener") or "").strip())): return "rejected"
        with self.lock:
            if self.busy(ch):
                core.log.info("%s meeting refused ch=%s ts=%s: meeting already running", "world" if world else "wheel", ch, ts); return "dup"
            if world and not ts:
                chair = self.bridges.get("babis") or next(iter(self.bridges.values()))
                text = re.sub(r"<[@!#][^>]*>", "", req.get("opener") or "").strip()[:600]
                try: ts = chair.client.chat_postMessage(channel=ch, text=text)["ts"]
                except Exception as e:
                    core.log.error("world porada opener failed ch=%s: %s", ch, type(e).__name__); return "error"
            parts = [k for k in self.uid_map if k != "kalousek"] or list(self.uid_map)
            m = self.active[(ch, ts)] = Meeting(self, ch, ts, parts, source="world" if world else "wheel", topic=req.get("topic") or "")
        core.log.info("meeting start (%s %s) ch=%s ts=%s participants=%s", "world" if world else "wheel",
                      req.get("slot") if world else req.get("event_id"), ch, ts, ",".join(parts))
        self.start(m)
        return ("started", {"thread_ts": ts}) if world else "started"

    def _trim(self):
        while len(self.seen) > self.max_seen: self.seen.popitem(last=False)

    def start(self, meeting, **kw):
        def go():
            t0 = time.monotonic(); usage.begin_collect()
            try: meeting.run(**kw)
            except Exception as e: core.log.error("meeting crashed: %s", type(e).__name__)
            finally:
                with self.lock: self.active.pop((meeting.ch, meeting.ts), None)
                core.log.info("meeting end ch=%s ts=%s", meeting.ch, meeting.ts)
                self.report_meeting(meeting, usage.end_collect(), time.monotonic() - t0)
        t = threading.Thread(target=go, daemon=True); t.start(); return t

    def report_meeting(self, meeting, calls, duration_s):
        try:
            s = usage.summarize(calls, meeting.turns, duration_s)
            core.log.info("meeting usage ch=%s ts=%s turns=%s calls=%s in=%s out=%s think=%s est=%s", meeting.ch, meeting.ts,
                          meeting.turns, s["total"]["calls"], s["total"]["in"], s["total"]["out"], s["total"]["think"], s["total"]["usd"])
            if self.reporter: self.reporter.meeting(calls, meeting.turns, duration_s, meeting.ch, meeting.ts)
        except Exception as e: core.log.warning("meeting usage report failed: %s", type(e).__name__)

    def say(self, spk, ch, ts, msgs, extra):
        b = self.bridges[spk]; p = self.cfg["personas"][spk]
        meeting = self.active.get((ch, ts))
        lines = []
        for m in msgs:
            _, name = meeting.label(m) if meeting else (None, "?")
            lines.append(f"{name}: {meeting.humanize(m.get('text')) if meeting else m.get('text')}")
        names = ", ".join("@" + self.cfg["personas"][k]["name"].split()[0] for k in self.bridges if k != spk)
        system = b.prompt + "\n\n" + MEETING_RULES + "\n" + ROLE.get(spk, "") + f"\nKolegové na poradě: {names}."
        user = "PŘEPIS PORADY:\n" + "\n".join(lines[-40:]) + "\n\n" + " ".join(extra) + f"\nTeď mluvíš ty ({p['name']})."
        usage.set_context(persona=spk, channel=ch, thread_ts=ts, kind="meeting")
        text = self.generate_ok(spk, system, [{"role": "user", "content": user}])
        text = self.linkify(strip_reply_mention(text, self.cfg))
        b.client.chat_postMessage(channel=ch, thread_ts=ts, text=text)
        core.log.info("meeting turn ch=%s ts=%s persona=%s", ch, ts, spk)
        return text

    def generate_ok(self, spk, system, hist, attempts=3, sleep=time.sleep):
        for a in range(attempts):
            try:
                t = (self.gen(system, hist) or "").strip()
                if t not in TERSE and len(t.strip(".… ")) >= 3: return t
                core.log.warning("meeting %s terse/empty reply, retry", spk)
            except Exception as e:
                core.log.warning("meeting llm error %s: %s", spk, type(e).__name__)
            sleep(2 * (a + 1))
        return KALOUSEK_FALLBACK if spk == "kalousek" else GENERIC_FALLBACK

    def linkify(self, text):
        """@Name / @Alias -> real <@uid> mention (bot-authored, so loop guard ignores it)."""
        for k, u in self.uid_map.items():
            p = self.cfg["personas"][k]
            for a in sorted(set(p["aliases"] + [p["name"].split()[0]]), key=len, reverse=True):
                text = re.sub(r"@" + re.escape(a) + r"(?!\w)", f"<@{u}>", text, flags=re.I)
        return text
