"""Storylet calendar (plan 1.4 `schedule` source) + the standalone scheduled porada (independent of the wheel).

Every weekday at porada_schedule (default "mon-fri 10:00", Europe/Prague) the world asks the persona bridge to run
a REAL bot porada (meeting.py) in porada_channel (#oku-porada):
  1. `schedule.due`   claims the slot exactly once (dedupe_key schedule:porada:<date> <time>; restarts are safe);
  2. kill switch / quiet hours / wheel live / budget -> `budget.denied` (with reasons), or
  3. dry_run (default) -> `porada.dry_run` with what it WOULD request; no hand-off line is written, or
  4. live -> one line in <source logs>/outbox/meeting_start.jsonl (handoff.request_world_meeting) +
     `porada.requested` (charges 1 top-level post + porada_llm_estimate LLM calls); the bridge acks in
     meeting_ack.jsonl -> `porada.started` (with thread_ts) or `porada.failed` (dup/rejected/stale/error/no_ack).
A slot that is more than porada_grace_min late (service was down) is recorded once as `schedule.skipped`."""
import hashlib, logging, random
from . import budget, tz
from .. import handoff

log = logging.getLogger("oku_world.scheduler")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PORADA_CAST = ("babis", "alenka", "bourak", "marty", "peta")   # bridge porada participants (kalousek excluded)
PORADA_DEFAULTS = {"porada_schedule": "mon-fri 10:00", "porada_channel": "C0C6W8E6NP9", "porada_grace_min": 30,
                   "porada_llm_estimate": 12, "porada_ack_timeout_s": 120, "porada_enabled": True}

def parse_schedule(spec):
    """'mon-fri 10:00' | 'daily 09:30' | 'mon,wed,fri 10:00,15:00' | 'sat 11:00' -> (set of weekday ints, [minutes])."""
    parts = str(spec or "").strip().lower().split()
    if len(parts) != 2: raise ValueError(f"bad schedule {spec!r}: expected '<days> <HH:MM>'")
    days_s, times_s = parts
    days = set()
    for chunk in days_s.split(","):
        if chunk in ("daily", "*", "all"): days |= set(range(7)); continue
        if chunk in ("weekdays", "workdays"): days |= set(range(5)); continue
        if "-" in chunk:
            a, b = chunk.split("-", 1); ia, ib = DAYS.index(a[:3]), DAYS.index(b[:3])
            days |= set(range(ia, ib + 1)) if ia <= ib else set(range(ia, 7)) | set(range(0, ib + 1))
        else: days.add(DAYS.index(chunk[:3]))
    times = sorted({tz.hm(t) for t in times_s.split(",")})
    return days, times

def due_slot(spec, now, zone=tz.PRAGUE, grace_min=30):
    """The slot that is due at `now`: (slot_key, slot_ts, late) or None. late=True when now > slot + grace
    (still the same local day): the caller records it as skipped once."""
    days, times = parse_schedule(spec)
    d = tz.local(now, zone)
    if d.weekday() not in days: return None
    m = d.hour * 60 + d.minute
    past = [t for t in times if t <= m]
    if not past: return None
    t = past[-1]
    slot_ts = tz.to_ts(d.year, d.month, d.day, t // 60, t % 60, zone)
    key = f"{d.year:04d}-{d.month:02d}-{d.day:02d} {t // 60:02d}:{t % 60:02d}"
    return key, slot_ts, now > slot_ts + grace_min * 60

TOPICS = [
    ("blame", "Kdo může za {blame_who}? Vina {blame_n}, chci vysvětlení a čísla."),
    ("hranolky", "Hranolky na skladě: {hranolky}. Kantýna hlásí krizi, kdo to vyřeší?"),
    ("kampan", "Kampaň stojí na {kampan}/100. Jak to dostaneme nahoru do pátku?"),
    ("dotace", "Dotace {dotace}. Kde jsou peníze a kdo je utratil?"),
    ("lajky", "Lajky {lajky}. Marty, proč to není milion?"),
    ("missed", "Propadla nám událost {missed_title}. Kdo nepřišel a proč?"),
    ("busy", "Za poslední týden {bridge_calls} odpovědí na Slacku. Je to produktivita, nebo kecání?"),
    ("news", "Dnešní titulek ({news_outlet}): {news_headline} Co s tím uděláme a jak to otočíme v náš prospěch?"),
    ("generic", "Pravidelná porada: výsledky týdne, KPI a kdo za co může."),
]
OPENERS = [
    "Dobré ráno, kolegové! Pravidelná porada OKÚ. Dnešní téma: {topic} Chci čísla, žádné kecy.",
    "Porada! Všichni sem, hned. Téma: {topic} Kdo nemá čísla, jde do kantýny.",
    "Tak, ranní porada OKÚ začíná. {topic} Makáme, nekrademe, chci výsledky.",
]

def topic_for(snap, slot_key, names=None, titles=None, news=None):
    """Deterministic (slot-keyed RNG) topic + opener from world state; template fallback. Returns (kind, topic, opener).
    news: today's headline about Babiš ({headline, outlet}, news.headline_for) -> the 'news' topic, weighted x3."""
    names = names or {}; titles = titles or {}
    res = snap.get("resources") or {}; blame = snap.get("blame") or {}; m = snap.get("metrics_7d") or {}
    ctx = {k: f"{int(v):,}".replace(",", "\u00a0") for k, v in res.items()}
    cands = []
    top = sorted(((v, k) for k, v in blame.items() if v > 0), key=lambda x: (-x[0], x[1]))
    if top:
        ctx["blame_who"] = names.get(top[0][1], top[0][1]); ctx["blame_n"] = top[0][0]; cands.append("blame")
    from . import state
    th = state.THRESHOLDS
    if res.get("hranolky", 80) < th.get("hranolky_low", 60): cands.append("hranolky")
    if res.get("kampan", 60) < th.get("kampan_low", 40): cands.append("kampan")
    cands += ["dotace", "lajky"]
    exp = [x for x in (snap.get("recent") or []) if x.get("state") == "expired"]
    if exp:
        ctx["missed_title"] = titles.get(exp[-1].get("key"), exp[-1].get("key") or "?"); cands.append("missed")
    if m.get("bridge_calls"): ctx["bridge_calls"] = m["bridge_calls"]; cands.append("busy")
    if news and news.get("headline"):
        h = str(news["headline"]).strip()
        ctx["news_headline"] = h if h.endswith((".", "?", "!", "…")) else h + "."; ctx["news_outlet"] = news.get("outlet") or "tisk"
        cands += ["news"] * 3
    rng = random.Random(int(hashlib.sha256(f"PORADA:{slot_key}".encode()).hexdigest()[:12], 16))
    kind = rng.choice(cands) if cands else "generic"
    tmpl = dict(TOPICS)[kind]
    try: topic = tmpl.format(**ctx)
    except (KeyError, ValueError): kind, topic = "generic", dict(TOPICS)["generic"]
    opener = rng.choice(OPENERS).format(topic=topic)
    return kind, topic, opener

class PoradaScheduler:
    """Owns the porada storylet. `svc` provides: diary, world (projection), settings(), logs_dir, outbox_dir,
    clock, names/titles. All decisions are diary rows, so every fire or skip is explainable."""
    def __init__(self, svc):
        self.svc = svc

    def cfg(self):
        c = dict(PORADA_DEFAULTS); c.update(self.svc.settings()); return c

    def tick(self, now=None):
        now = self.svc.clock() if now is None else now
        c = self.cfg(); out = []
        try: self.check_ack(now, c, out)
        except Exception as e: log.warning("porada ack check failed: %s", type(e).__name__)
        if not c.get("porada_enabled", True): return out
        slot = due_slot(c["porada_schedule"], now, c.get("timezone", tz.PRAGUE), int(c["porada_grace_min"]))
        if not slot: return out
        key, slot_ts, late = slot
        d = self.svc.diary
        dk = f"schedule:porada:{key}"
        if d.by_dedupe(dk): return out
        if late:
            r = d.record("schedule.skipped", None, f"schedule:porada:{key}", {"storylet": "PORADA", "slot": key, "reason": "late",
                         "late_min": int((now - slot_ts) // 60)}, source="schedule", dedupe_key=dk)
            out.append(r); return out
        due = d.record("schedule.due", None, f"schedule:porada:{key}", {"storylet": "PORADA", "slot": key, "channel": c["porada_channel"]},
                       source="schedule", dedupe_key=dk)
        if not due: return out
        out.append(due); out.append(self.decide(due, key, now, c)); return out

    def decide(self, due, key, now, c):
        d, svc = self.svc.diary, self.svc
        snap = svc.world.snapshot(players=False)
        nh = getattr(svc, "news_headline", lambda *a, **k: None)("babis", now)
        kind, topic, opener = topic_for(snap, key, getattr(svc, "names", {}), getattr(svc, "titles", {}), news=nh)
        ch = c["porada_channel"]; est = int(c["porada_llm_estimate"])
        rows = d.events(since_ts=min(tz.day_start(now, c.get("timezone", tz.PRAGUE)), now - float(c["per_channel_gap_h"]) * 3600) - 1)
        kill = budget.killed(c, svc.logs_dir, d.kv_get, now)
        dec = budget.check("post", rows, now, c, channel=ch, llm=est, wheel_live=snap.get("wheel_live"), kill=kill)
        subj = f"schedule:porada:{key}"
        base = {"storylet": "PORADA", "slot": key, "channel": ch, "topic": topic, "topic_kind": kind, "why": dec["why"]}
        if kind == "news" and nh: base["news_url"] = nh.get("url")
        if not dec["ok"]:
            return d.record("budget.denied", "babis", subj, dict(base, reasons=dec["reasons"]), source="schedule", parents=[due["id"]],
                            dedupe_key=f"budget:porada:{key}")
        if c.get("dry_run", True):
            would = {"file": "meeting_start.jsonl", "channel": ch, "source": "world", "host": "babis", "topic": topic,
                     "opener_chars": len(opener), "llm_estimate": est,
                     "brief_chars": {k: len(v) for k, v in getattr(svc, "briefs_for", lambda ps: {})(list(PORADA_CAST)).items()}}
            log.info("porada %s DRY RUN: would request a porada in %s, topic=%r", key, ch, topic)
            return d.record("porada.dry_run", "babis", subj, dict(base, would=would, chair="babis"), source="schedule", parents=[due["id"]],
                            dedupe_key=f"porada:dry_run:{key}")
        req = handoff.request_world_meeting(ch, topic, opener, slot=key, outbox_dir=svc.outbox_dir, clock=svc.clock,
                                            briefs=getattr(svc, "briefs_for", lambda ps: {})(list(PORADA_CAST)))
        row = d.record("porada.requested", "babis", subj, dict(base, request_id=req["id"], llm_calls=est, chair="babis", top_level=True),
                       source="schedule", parents=[due["id"]], dedupe_key=f"porada:requested:{key}")
        d.kv_set("porada:pending", f"{req['id']}|{key}|{now}|{row['id'] if row else ''}")
        log.info("porada %s requested (id=%s) in %s", key, req["id"], ch)
        return row

    def check_ack(self, now, c, out):
        pend = self.svc.diary.kv_get("porada:pending")
        if not pend: return
        rid, key, at, parent = (pend.split("|") + ["", "", "0", ""])[:4]
        a = handoff.ack_for(rid, self.svc.outbox_dir)
        subj = f"schedule:porada:{key}"; par = [parent] if parent else None
        if a:
            st = a.get("status")
            t = "porada.started" if st == "started" else "porada.failed"
            out.append(self.svc.diary.record(t, "babis", subj, {"storylet": "PORADA", "slot": key, "request_id": rid, "status": st,
                       "thread_ts": a.get("thread_ts"), "channel": c["porada_channel"], "chair": "babis"}, source="schedule", parents=par,
                       dedupe_key=f"porada:ack:{rid}"))
            self.svc.diary.kv_set("porada:pending", None)
        elif now - float(at or 0) > float(c["porada_ack_timeout_s"]):
            out.append(self.svc.diary.record("porada.failed", "babis", subj, {"storylet": "PORADA", "slot": key, "request_id": rid,
                       "status": "no_ack", "channel": c["porada_channel"]}, source="schedule", parents=par, dedupe_key=f"porada:ack:{rid}"))
            self.svc.diary.kv_set("porada:pending", None)
