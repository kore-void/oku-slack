"""Director + autonomous persona-to-persona chatter (ECOSYSTEM-PLAN P-005), world side.

On each due chatter slot (chatter_schedule, default "mon-fri 11:30,16:30" Europe/Prague) the director:
  1. claims the slot once: `schedule.due` (storylet CHATTER; a slot later than chatter_grace_min -> `schedule.skipped`);
  2. picks a storylet from world state and diary events (top blame, a missed wheel event, low Hranolky/Kampaň, today's
     porada topic, last week's Slack activity) or a generic template, 2-3 OKÚ personas (lead + 1-2 partners), a
     persona channel and 3-4 turns, with a keyed RNG (`CHATTER:<slot>`) so every pick is reproducible;
     storylets/channels already used today and channels inside the per-channel gap are avoided when possible;
  3. budget (chatter threads/day, posts/day, per-channel gap, LLM cap, quiet hours, wheel live, kill switch)
     -> `budget.denied`, or
  4. dry_run (default) -> `chatter.dry_run` with the personas, channel, topic and opener it WOULD use; NO hand-off, or
  5. live -> one kind=chatter line in <source logs>/outbox/meeting_start.jsonl (handoff.request_chatter) +
     `chatter.requested` (1 top-level post + turns-1 LLM calls); acks -> `chatter.started` (thread_ts) and
     `chatter.ended` (done/stopped/error/timeout, actual turns + LLM calls) or `chatter.failed` (rejected/dup/stale/no_ack).
The bridge drives every turn itself (oku_slack/chatter.py); personas never answer each other through Slack events.

Satire guardrails for the templates below: no quotes attributed to real people, nothing presented as real news or
fact, no health/family/crime topics, in-character, short. tests/test_world_chatter.py lints every template.
Stdlib only (the service runs in .venv or .venv-wheel)."""
import hashlib, json, logging, random
from . import budget, scheduler, state, tz
from .. import handoff

log = logging.getLogger("oku_world.chatter")
CHANNELS = {"kantyna": "C0C7BF296P4", "dotace": "C0C75CZ2UCT", "socky": "C0C76ATANTX", "disko": "C0C75CZPRUK", "vina": "C0C76ATGLAH"}
CAST = ("babis", "alenka", "bourak", "marty", "peta", "kalousek")   # Peťa also speaks for Macinka
CHATTER_DEFAULTS = {"chatter_enabled": True, "chatter_schedule": "mon-fri 11:30,16:30", "chatter_grace_min": 30,
                    "chatter_ack_timeout_s": 120, "chatter_done_timeout_s": 900, "chatter_channels": dict(CHANNELS)}
TERMINAL = ("done", "stopped", "error")

# needs: which world fact must hold (None = always eligible). weight: state-driven storylets win more often.
STORYLETS = [
    {"key": "KALOUSEK_VINA", "channel": "vina", "lead": "kalousek", "pool": ["babis", "bourak", "alenka"], "needs": "blame", "weight": 3,
     "topic": "Kdo může za to, že {blame_who} má na kontě vinu {blame_n}?",
     "openers": ["Já za nic nemůžu. Vina {blame_n} pro {blame_who}? To jste si spočítali sami, já mám alibi v Excelu.",
                 "Zase se hledá viník. {blame_who} má vinu {blame_n} a já jen upozorňuju, že já to nebyl."]},
    {"key": "PROPADLA_AKCE", "channel": "vina", "lead": "kalousek", "pool": ["babis", "marty"], "needs": "missed", "weight": 3,
     "topic": "Propadla akce {missed_title}. Kdo nepřišel a proč?",
     "openers": ["Akce {missed_title} propadla. Já tam nebyl, takže to logicky nebyla moje chyba.",
                 "Tak {missed_title} jsme prošvihli. Navrhuju najít viníka, ideálně někoho jiného než mě."]},
    {"key": "HRANOLKY_DOCHAZEJI", "channel": "kantyna", "lead": "alenka", "pool": ["babis", "bourak"], "needs": "hranolky_low", "weight": 3,
     "topic": "Hranolky na skladě: {hranolky}. Kantýna hlásí krizi.",
     "openers": ["Kantýna hlásí stav hranolek {hranolky}. Kdo je snědl a kdo je doplní?",
                 "Hranolky: {hranolky}. Tohle je krizový stav, kolegové, a já to nehodlám řešit sama."]},
    {"key": "KANTYNA_MENU", "channel": "kantyna", "lead": "alenka", "pool": ["bourak", "peta", "marty"], "needs": None, "weight": 1,
     "topic": "Menu dne v kantýně: hranolky, nebo zase hranolky?",
     "openers": ["Dnešní menu: hranolky. Alternativa: hranolky s tatarkou. Stížnosti pište sem.",
                 "Kantýna má dnes speciál dne. Hádejte, co to je. Nápověda: začíná to na hranol."]},
    {"key": "PORADA_DOZVUK", "channel": "kantyna", "lead": "bourak", "pool": ["alenka", "kalousek"], "needs": "porada_today", "weight": 2,
     "topic": "Dozvuky ranní porady: {porada_topic}",
     "openers": ["Tak co ta ranní porada? Já si z ní pamatuju hlavně tohle: {porada_topic}",
                 "Po poradě jdu rovnou do kantýny. Téma bylo {porada_topic} a já mám hlad."]},
    {"key": "BABIS_CHCE_CISLA", "channel": "dotace", "lead": "babis", "pool": ["marty", "bourak", "kalousek"], "needs": None, "weight": 2,
     "topic": "Dotace {dotace}: kde jsou čísla a kdo je utratil?",
     "openers": ["Dotace {dotace}. Chci vidět čísla, ne kecy. Kdo mi to vysvětlí?",
                 "Kolegové, dotace stojí na {dotace}. Já makám, ale někdo tady utrácí. Čísla na stůl."]},
    {"key": "KAMPAN_DOLE", "channel": "dotace", "lead": "babis", "pool": ["marty", "peta"], "needs": "kampan_low", "weight": 3,
     "topic": "Kampaň stojí na {kampan}/100. Jak ji zvednout do pátku?",
     "openers": ["Kampaň na {kampan} ze sta. To je katastrofa. Kdo má plán, ať se ozve hned.",
                 "Kampaň {kampan}/100. Já bych to zvedl sám, ale chci slyšet vaše nápady."]},
    {"key": "MARTY_VIRAL", "channel": "socky", "lead": "marty", "pool": ["babis", "peta"], "needs": None, "weight": 2,
     "topic": "Lajky {lajky}. Proč to ještě není milion a co bude příští virál?",
     "openers": ["Máme {lajky} lajků. Mám nápad na virál, ale potřebuju rozpočet a jednoho dobrovolníka.",
                 "Lajky {lajky}. Algoritmus nás nemá rád, takže ho musíme přechytračit. Návrhy?"]},
    {"key": "SLACK_KECY", "channel": "socky", "lead": "marty", "pool": ["babis", "kalousek"], "needs": "busy", "weight": 2,
     "topic": "{bridge_calls} odpovědí na Slacku za týden. Obsah, nebo kecání?",
     "openers": ["Za týden {bridge_calls} odpovědí na Slacku. To je obsah! Nebo kecání, podle toho, kdo se ptá.",
                 "Statistika týdne: {bridge_calls} odpovědí. Chci z toho udělat reels, kdo se přidá?"]},
    {"key": "DISKO_PATEK", "channel": "disko", "lead": "peta", "pool": ["bourak", "marty", "alenka"], "needs": None, "weight": 2,
     "topic": "Páteční disko OKÚ: kdo dělá playlist a kdo platí hranolky?",
     "openers": ["Páteční disko OKÚ se blíží. Playlist mám, chybí mi jen někdo, kdo zaplatí hranolky.",
                 "Disko v pátek! Kdo nepřijde, toho dám do playlistu jako remix."]},
]
BY_KEY = {s["key"]: s for s in STORYLETS}

def facts(snap, names=None, titles=None, porada_topic=None):
    """World facts the storylets may need (from the projection snapshot + diary-derived porada topic)."""
    names = names or {}; titles = titles or {}
    res = snap.get("resources") or {}; blame = snap.get("blame") or {}; m = snap.get("metrics_7d") or {}
    ctx = {k: f"{int(v):,}".replace(",", "\u00a0") for k, v in res.items()}; have = set()
    top = sorted(((v, k) for k, v in blame.items() if v > 0), key=lambda x: (-x[0], x[1]))
    if top: ctx["blame_who"] = names.get(top[0][1], top[0][1]); ctx["blame_n"] = top[0][0]; have.add("blame")
    th = state.THRESHOLDS
    if res.get("hranolky", 80) < th.get("hranolky_low", 60): have.add("hranolky_low")
    if res.get("kampan", 60) < th.get("kampan_low", 40): have.add("kampan_low")
    exp = [x for x in (snap.get("recent") or []) if x.get("state") == "expired"]
    if exp: ctx["missed_title"] = titles.get(exp[-1].get("key"), exp[-1].get("key") or "?"); have.add("missed")
    if m.get("bridge_calls"): ctx["bridge_calls"] = m["bridge_calls"]; have.add("busy")
    if porada_topic: ctx["porada_topic"] = str(porada_topic)[:200]; have.add("porada_today")
    return ctx, have

def pick(slot_key, ctx, have, avoid_storylets=(), avoid_channels=(), blocked_channels=(), max_turns=4, channels=None):
    """Deterministic (keyed RNG CHATTER:<slot>) storylet + cast + turns. Returns a dict; pure."""
    channels = channels or CHANNELS
    rng = random.Random(int(hashlib.sha256(f"CHATTER:{slot_key}".encode()).hexdigest()[:12], 16))
    elig = [s for s in STORYLETS if (s["needs"] is None or s["needs"] in have) and s["channel"] in channels]
    fresh = [s for s in elig if s["key"] not in avoid_storylets and s["channel"] not in avoid_channels and channels[s["channel"]] not in blocked_channels]
    pool = fresh or [s for s in elig if channels[s["channel"]] not in blocked_channels] or elig
    if not pool: return None   # no storylet fits the configured channels: the director records budget.denied no_storylet
    s = rng.choices(pool, weights=[s["weight"] for s in pool])[0]
    partners = rng.sample(s["pool"], min(len(s["pool"]), rng.choice([1, 2])))
    turns = max(2, min(int(max_turns), handoff_max(), rng.choice([3, 4])))
    try: topic, opener = s["topic"].format(**ctx), rng.choice(s["openers"]).format(**ctx)
    except (KeyError, ValueError, IndexError):   # a fact went missing: same storylet/cast, neutral static text
        topic, opener = "Jak to dneska v OKÚ vypadá?", "Tak co, kolegové, jak to dneska vypadá?"
    return {"storylet": s["key"], "needs": s["needs"], "channel_name": s["channel"], "channel": channels[s["channel"]],
            "personas": [s["lead"]] + partners, "turns": turns, "topic": topic, "opener": opener,
            "candidates": [x["key"] for x in pool], "rng_key": f"CHATTER:{slot_key}"}

def handoff_max(): return 4   # the bridge's hard cap (oku_slack/chatter.py HARD_MAX_TURNS)

class ChatterDirector:
    """`svc` provides diary, world, settings(), logs_dir, outbox_dir, clock, names, titles (like PoradaScheduler)."""
    def __init__(self, svc): self.svc = svc

    def cfg(self):
        c = dict(CHATTER_DEFAULTS); c.update(self.svc.settings())
        ch = c.get("chatter_channels")
        c["chatter_channels"] = {k: v for k, v in (ch.items() if isinstance(ch, dict) else CHANNELS.items()) if k in CHANNELS and v}
        return c

    def tick(self, now=None):
        now = self.svc.clock() if now is None else now
        c = self.cfg(); out = []
        try: self.check_ack(now, c, out)
        except Exception as e: log.warning("chatter ack check failed: %s", type(e).__name__)
        if not c.get("chatter_enabled", True): return out
        slot = scheduler.due_slot(c["chatter_schedule"], now, c.get("timezone", tz.PRAGUE), int(c["chatter_grace_min"]))
        if not slot: return out
        key, slot_ts, late = slot
        d = self.svc.diary; dk = f"schedule:chatter:{key}"; subj = f"chatter:{key}"
        if d.by_dedupe(dk): return out
        if late:
            out.append(d.record("schedule.skipped", None, subj, {"storylet": "CHATTER", "slot": key, "reason": "late",
                                "late_min": int((now - slot_ts) // 60)}, source="schedule", dedupe_key=dk)); return out
        due = d.record("schedule.due", None, subj, {"storylet": "CHATTER", "slot": key}, source="schedule", dedupe_key=dk)
        if not due: return out
        out.append(due); out.append(self.decide(due, key, now, c)); return out

    def today(self, now, c):
        zone = c.get("timezone", tz.PRAGUE)
        return self.svc.diary.events(since_ts=min(tz.day_start(now, zone), now - float(c["per_channel_gap_h"]) * 3600) - 1)

    def decide(self, due, key, now, c):
        d, svc = self.svc.diary, self.svc
        snap = svc.world.snapshot(players=False)
        rows = self.today(now, c); day0 = tz.day_start(now, c.get("timezone", tz.PRAGUE))
        today = [r for r in rows if r["ts"] >= day0]
        porada = [r for r in today if r["type"] in ("porada.started", "porada.requested", "porada.dry_run") and (r["payload"] or {}).get("topic")]
        ctx, have = facts(snap, getattr(svc, "names", {}), getattr(svc, "titles", {}), porada[-1]["payload"]["topic"] if porada else None)
        used = [r["payload"] for r in today if r["type"] in ("chatter.dry_run", "chatter.requested")]
        u = budget.usage(rows, now, c); gap = float(c["per_channel_gap_h"]) * 3600
        blocked = {ch for ch, t in u["channel_last"].items() if now - t < gap}
        p = pick(key, ctx, have, [x.get("storylet") for x in used], [x.get("channel_name") for x in used], blocked,
                 int(c["chatter_max_turns"]), c["chatter_channels"])
        if p is None:
            return d.record("budget.denied", None, f"chatter:{key}", {"storylet": "CHATTER", "slot": key, "reasons": ["no_storylet"],
                            "channels": sorted(c["chatter_channels"])}, source="chatter", parents=[due["id"]], dedupe_key=f"budget:chatter:{key}")
        est = p["turns"] - 1   # turn 1 is the template opener; the bridge generates turns 2..N
        kill = budget.killed(c, svc.logs_dir, d.kv_get, now)
        dec = budget.check("chatter", rows, now, c, channel=p["channel"], llm=est, turns=p["turns"], wheel_live=snap.get("wheel_live"), kill=kill)
        subj = f"chatter:{key}"
        base = {"storylet": p["storylet"], "slot": key, "channel": p["channel"], "channel_name": p["channel_name"],
                "personas": p["personas"], "participants": p["personas"], "topic": p["topic"], "needs": p["needs"],
                "turns": p["turns"], "llm_estimate": est, "candidates": p["candidates"], "rng_key": p["rng_key"], "why": dec["why"]}
        lead = p["personas"][0]
        if not dec["ok"]:
            return d.record("budget.denied", lead, subj, dict(base, storylet="CHATTER", pick=p["storylet"], reasons=dec["reasons"]),
                            source="chatter", parents=[due["id"]], dedupe_key=f"budget:chatter:{key}")
        if c.get("dry_run", True):
            log.info("chatter %s DRY RUN: %s in %s (%s), personas=%s turns=%d", key, p["storylet"], p["channel_name"], p["channel"],
                     ",".join(p["personas"]), p["turns"])
            bc = {k: len(v) for k, v in getattr(svc, "briefs_for", lambda ps: {})(p["personas"]).items()}
            return d.record("chatter.dry_run", lead, subj, dict(base, opener=p["opener"], would={"file": "meeting_start.jsonl", "kind": "chatter",
                            "source": "world:chatter", "top_level": True, "brief_chars": bc}), source="chatter", parents=[due["id"]],
                            dedupe_key=f"chatter:dry_run:{key}")
        req = handoff.request_chatter(p["channel"], p["personas"], p["topic"], p["opener"], p["turns"], storylet=p["storylet"], slot=key,
                                      outbox_dir=svc.outbox_dir, clock=svc.clock, briefs=getattr(svc, "briefs_for", lambda ps: {})(p["personas"]))
        row = d.record("chatter.requested", lead, subj, dict(base, request_id=req["id"], llm_calls=est, top_level=True, opener_chars=len(p["opener"])),
                       source="chatter", parents=[due["id"]], dedupe_key=f"chatter:requested:{key}")
        d.kv_set("chatter:pending", json.dumps({"rid": req["id"], "slot": key, "at": now, "parent": row["id"] if row else None,
                                                "channel": p["channel"], "personas": p["personas"], "storylet": p["storylet"], "started": None}))
        log.info("chatter %s requested (id=%s) %s in %s", key, req["id"], p["storylet"], p["channel"])
        return row

    def check_ack(self, now, c, out):
        raw = self.svc.diary.kv_get("chatter:pending")
        if not raw: return
        try: pend = json.loads(raw)
        except ValueError: self.svc.diary.kv_set("chatter:pending", None); return
        rid, key, d = pend["rid"], pend["slot"], self.svc.diary
        subj, par = f"chatter:{key}", [pend["parent"]] if pend.get("parent") else None
        base = {"storylet": pend.get("storylet"), "slot": key, "request_id": rid, "channel": pend.get("channel"), "participants": pend.get("personas")}
        acks = {a.get("status"): a for a in handoff.acks_for(rid, self.svc.outbox_dir)}
        st = acks.get("started")
        if st and not pend.get("started"):
            r = d.record("chatter.started", (pend.get("personas") or [None])[0], subj, dict(base, thread_ts=st.get("thread_ts"),
                         turns=st.get("turns"), status="started"), source="chatter", parents=par, dedupe_key=f"chatter:started:{rid}")
            out.append(r); pend["started"] = now; pend["started_id"] = r["id"] if r else None
            self.svc.diary.kv_set("chatter:pending", json.dumps(pend))
        end = next((acks[s] for s in TERMINAL if s in acks), None)
        par2 = [pend["started_id"]] if pend.get("started_id") else par
        if end and not (st or pend.get("started")):
            out.append(d.record("chatter.failed", None, subj, dict(base, status=end.get("status"), reason=end.get("reason")), source="chatter",
                       parents=par, dedupe_key=f"chatter:ended:{rid}"))
            d.kv_set("chatter:pending", None); return
        if end:
            out.append(d.record("chatter.ended", (pend.get("personas") or [None])[0], subj, dict(base, status=end.get("status"),
                       thread_ts=end.get("thread_ts"), turns=end.get("turns"), llm_calls_actual=end.get("llm_calls"),
                       guarded=end.get("guarded"), fallbacks=end.get("fallbacks")), source="chatter", parents=par2, dedupe_key=f"chatter:ended:{rid}"))
            d.kv_set("chatter:pending", None); return
        bad = next((acks[s] for s in acks if s not in TERMINAL and s != "started"), None)
        if bad and not st:
            out.append(d.record("chatter.failed", None, subj, dict(base, status=bad.get("status"), reason=bad.get("reason")), source="chatter",
                       parents=par, dedupe_key=f"chatter:ended:{rid}"))
            d.kv_set("chatter:pending", None); return
        if not st and now - float(pend.get("at") or 0) > float(c["chatter_ack_timeout_s"]):
            out.append(d.record("chatter.failed", None, subj, dict(base, status="no_ack"), source="chatter", parents=par, dedupe_key=f"chatter:ended:{rid}"))
            d.kv_set("chatter:pending", None)
        elif st and now - float(pend.get("started") or now) > float(c["chatter_done_timeout_s"]):
            out.append(d.record("chatter.ended", None, subj, dict(base, status="timeout"), source="chatter", parents=par2, dedupe_key=f"chatter:ended:{rid}"))
            d.kv_set("chatter:pending", None)
