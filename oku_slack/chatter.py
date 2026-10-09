"""Autonomous persona-to-persona chatter (ECOSYSTEM-PLAN P-005), bridge side.

The oku_world director asks for a short exchange through the existing file hand-off (logs/outbox/meeting_start.jsonl,
line kind="chatter", source="world:chatter", no thread_ts). Coordinator.start_external -> Coordinator.start_chatter:
  1. the first persona posts the world's template opener TOP-LEVEL in the persona channel (turn 1);
  2. the bridge itself drives turns 2..N (N <= 4) in that thread: each speaker's text is generated with the thread so
     far as context (the meeting.py turn pattern) and posted with that persona's own client;
  3. the Inbox acks "started" with thread_ts at once; when the exchange ends a second ack "done" carries turns and
     the number of LLM calls.
Loop guard: nothing here reacts to Slack events. Persona messages are bot messages that bridge.ignored() drops, the
chatter thread is not a meeting (Coordinator.claim/route_plain never route into it), and only this loop posts turns.
Satire guardrails: CHATTER_RULES in every prompt, mentions stripped, short messages, and a sensitive-topic/quote
filter (health, family, crime, invented quotations) that retries and then falls back to a neutral in-character line."""
import random, re, threading, time
from . import core, usage

HARD_MAX_TURNS = 4          # absolute cap, whatever the request or config says
MAX_CHARS = 280             # one turn
OPENER_MAX = 400
PAUSE_S = (6.0, 12.0)       # between turns
MENTION_RE = re.compile(r"<[@!#][^>]*>")
SENSITIVE_RE = re.compile(
    r"(?i)(?<!\w)(nemoc\w*|nemocnic\w*|rakovin\w*|zdraví|zdravotn\w*|diagnó\w*|infarkt\w*|covid\w*|hospitaliz\w*|"
    r"rodin\w*|manžel\w*|dcer\w*|syn(a|em|ovi)?|dět[ií]\w*|vnuk\w*|vnouč\w*|"
    r"soud(?!ruh)\w*|polici\w*|trestn\w*|obžalob\w*|obvin\w*|stíhán\w*|vězen\w*|kriminál\w*|podvod\w*|úplat\w*)(?!\w)")
QUOTE_RE = re.compile(r"[„\"“»][^„\"“”»«\n]{12,}[“\"”«]")
STOP_RE = re.compile(r"(?i)(?<!\w)(stop|konec)(?!\w)")
TERSE = {"…", "...", ".", "", "-"}
FALLBACK = {"kalousek": "Já za nic nemůžu, to jste si podělali sami.", "babis": "Tohle probereme na poradě, chci čísla.",
            "alenka": "Hlavně ať jsou hranolky, zbytek se uvidí.", "marty": "Z tohohle udělám reels, uvidíte.",
            "peta": "V pátek na disku to vyřešíme.", "bourak": "Já to beru, ale až po svačině."}
GENERIC_FALLBACK = "Tohle probereme u kafe, teď nemám čísla."

CHATTER_RULES = (
    "Toto je krátká neformální debata kolegů z týmu OKÚ ve Slack vlákně (není to porada). Níže je dosavadní vlákno "
    "(jména u replik). Pravidla tvé repliky: zůstaň ve své roli a ve svém stylu; reaguj přímo na poslední repliku a drž "
    "se tématu vlákna; 1-2 krátké věty, nejvýš 200 znaků; kolegy oslovuj jen jménem, bez @ a bez Slack zmínek. "
    "NEVYMÝŠLEJ citáty skutečných lidí a nic nevydávej za skutečný fakt nebo zprávu (nadsázka a vymyšlená KPI jen jako "
    "zjevný vtip týmu OKÚ). Žádná témata zdraví a nemocí, rodiny a dětí, soudů, policie ani trestné činnosti; nikoho "
    "neurážej za vzhled, původ či víru. Nepiš za jiné postavy, nepiš své jméno na začátek, žádné uvozovky kolem repliky.")

def clean(text, limit=MAX_CHARS):
    """No Slack mentions/broadcasts, no '@', no wrapping quotes, one paragraph, <= limit chars (cut at a sentence end)."""
    t = MENTION_RE.sub("", text or "").replace("@", "")
    t = re.sub(r"\s+", " ", t).strip().strip("\"„“”'").strip()
    if len(t) <= limit: return t
    cut = max(t.rfind(p, 0, limit) for p in (". ", "! ", "? "))
    return t[: cut + 1] if cut >= limit // 3 else t[: limit - 1].rstrip() + "…"

def guard_hit(text):
    """Reason string when a generated turn breaks the satire guardrails, else None."""
    m = SENSITIVE_RE.search(text or "")
    if m: return "sensitive:" + m.group(1).lower()
    if QUOTE_RE.search(text or ""): return "quote"
    return None

def speaker_order(personas, turns):
    """Round robin starting with the opener: 2 personas a,b,a,b; 3 personas a,b,c,a. Never the same twice in a row."""
    return [personas[i % len(personas)] for i in range(turns)]

def first_name(cfg, k): return cfg["personas"][k]["name"].split()[0]

class Chatter:
    def __init__(self, coord, req, turns):
        self.c, self.req = coord, req
        self.ch, self.personas, self.n = req["channel"], list(req["personas"]), int(turns)
        self.topic = (req.get("topic") or "").strip()[:300]
        self.ts, self.turns, self.llm_calls, self.stopped, self.guarded, self.fallbacks = None, 0, 0, False, 0, 0

    def open(self):
        """Turn 1: the opener persona posts the world's template opener top-level. Returns the thread ts."""
        b = self.c.bridges[self.personas[0]]
        self.ts = b.client.chat_postMessage(channel=self.ch, text=clean(self.req.get("opener"), OPENER_MAX))["ts"]
        self.turns = 1
        return self.ts

    def transcript(self):
        b = self.c.bridges[self.personas[0]]
        try: return b.client.conversations_replies(channel=self.ch, ts=self.ts, limit=50)["messages"]
        except Exception as e: core.log.warning("chatter transcript failed: %s", type(e).__name__); return []

    def label(self, m):
        k = self.c.uid_to_persona.get(m.get("user"))
        if k: return self.c.cfg["personas"][k]["name"]
        return m.get("username") or "bot" if m.get("bot_id") else "člověk"

    def humanize(self, text):
        for k, u in self.c.uid_map.items(): text = (text or "").replace(f"<@{u}>", first_name(self.c.cfg, k))
        return MENTION_RE.sub("", text or "")

    def run(self, sleep=None, rng=random):
        order = speaker_order(self.personas, self.n)
        for i in range(1, self.n):
            (sleep or time.sleep)(rng.uniform(*PAUSE_S))
            msgs = self.transcript()
            humans = [m for m in msgs if not m.get("bot_id") and m.get("user") not in self.c.uid_to_persona]
            if any(STOP_RE.search(m.get("text") or "") for m in humans):
                core.log.info("chatter stop ch=%s ts=%s turn=%d", self.ch, self.ts, i); self.stopped = True; break
            spk = order[i]
            text = self.say(spk, msgs, final=(i == self.n - 1))
            self.c.bridges[spk].client.chat_postMessage(channel=self.ch, thread_ts=self.ts, text=text)
            self.turns += 1
            core.log.info("chatter turn ch=%s ts=%s persona=%s %d/%d", self.ch, self.ts, spk, i + 1, self.n)
        return self.turns

    def prompt(self, spk, msgs, final):
        cfg = self.c.cfg; b = self.c.bridges[spk]
        others = ", ".join(first_name(cfg, k) for k in self.personas if k != spk)
        system = b.prompt + "\n\n" + CHATTER_RULES + f"\nVe vlákně jsou s tebou: {others}."
        lines = [f"{self.label(m)}: {self.humanize(m.get('text'))}" for m in msgs][-12:]
        extra = "Uzavíráš tuhle krátkou výměnu jednou pointou, bez otázky." if final else "Klidně polož kolegovi jednu krátkou otázku."
        user = (f"TÉMA VLÁKNA: {self.topic}\n" if self.topic else "") + "VLÁKNO:\n" + "\n".join(lines) + \
               f"\n\n{extra}\nTeď píšeš ty ({cfg['personas'][spk]['name']})."
        return system, [{"role": "user", "content": user}]

    def say(self, spk, msgs, final=False, attempts=3, sleep=time.sleep):
        system, hist = self.prompt(spk, msgs, final)
        usage.set_context(persona=spk, channel=self.ch, thread_ts=self.ts, kind="chatter")
        for a in range(attempts):
            self.llm_calls += 1
            try: t = clean(self.c.gen(system, hist))
            except Exception as e: core.log.warning("chatter llm error %s: %s", spk, type(e).__name__); t = ""
            if t in TERSE or len(t.strip(".… ")) < 3:
                core.log.warning("chatter %s terse/empty reply, retry", spk); sleep(2 * (a + 1)); continue
            why = guard_hit(t)
            if why is None: return t
            self.guarded += 1; core.log.warning("chatter %s guardrail hit (%s), retry", spk, why)
        self.fallbacks += 1
        return FALLBACK.get(spk, GENERIC_FALLBACK)

def allowed_channels(cfg):
    """Persona channels the world may use: [world].chatter_channels values, else every channel in channel_defaults."""
    w = (cfg.get("world") or {}).get("chatter_channels")
    if isinstance(w, dict) and w: return set(w.values())
    return set((cfg.get("channel_defaults") or {}).keys())

def validate(cfg, req, bridges):
    """(personas, turns) or raises ValueError(reason). Pure."""
    ch = req.get("channel")
    if not ch or ch not in allowed_channels(cfg): raise ValueError("channel_not_allowed")
    ps = req.get("personas")
    if not isinstance(ps, list) or not 2 <= len(ps) <= 3 or len(set(ps)) != len(ps): raise ValueError("bad_personas")
    missing = [p for p in ps if p not in bridges]
    if missing: raise ValueError("persona_offline:" + ",".join(map(str, missing)))
    if not (req.get("opener") or "").strip() or req.get("thread_ts"): raise ValueError("bad_opener")
    try: turns = int(req.get("turns") or 0)
    except (TypeError, ValueError): raise ValueError("bad_turns")
    cap = min(HARD_MAX_TURNS, int((cfg.get("world") or {}).get("chatter_max_turns", HARD_MAX_TURNS)))
    if turns < 2: raise ValueError("bad_turns")
    return ps, min(turns, cap)
