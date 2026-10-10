"""Podnet reaction (ECOSYSTEM-PLAN P-004 rules REPOST_VIDEO / STREAM_ANNOUNCE), planned by the world director.

For each `podnet.received` (oldest first) the reactor decides once:
- guardrails: a title touching health/family/crime or carrying a quotation -> `podnet.skipped` reason guard:<..>;
  the comment never repeats the title or says anything about what the linked post contains;
- persona by kind (keyed RNG `PODNET:<podnet key>`): x_video/x_post -> Marty or Babiš (#oku-socky / #oku-dotace-desk),
  stream -> Peťa/Macinka (#oku-disko), video -> Marty, news -> Babiš or Kalousek, other -> Marty; optionally ONE reply
  persona (reply_rate, default 0.5) who answers once in the thread;
- budget: posts/day, per-channel gap, LLM cap (a reply = 1 call), quiet hours, wheel live, kill switch, plus
  podnet_reactions_per_day (default 2). Transient denials (quiet hours, wheel live, channel gap) wait and retry until
  podnet_max_age_h (default 6) -> `podnet.skipped` reason stale; the others -> `budget.denied` (storylet PODNET);
- dry_run -> `podnet.dry_run` (persona, reply persona, channel, opener = URL + comment, why) and NO hand-off;
- live -> one kind=chatter line (source world:chatter, `podnet` payload, turns 1-2) in meeting_start.jsonl +
  `podnet.requested` (counts as 1 top-level post); acks -> `podnet.started` / `podnet.ended` / `podnet.failed`.
  Test podnets (payload test=true) are never handed off: in live mode they end as `podnet.skipped` reason test."""
import hashlib, json, logging, random
from . import budget, chatter as wchatter, guard, state, tz
from .. import handoff

log = logging.getLogger("oku_world.react")
DEFAULTS = {"podnet_react": True, "podnet_reactions_per_day": 2, "podnet_reply_rate": 0.5, "podnet_max_age_h": 6,
            "podnet_ack_timeout_s": 120, "podnet_done_timeout_s": 600}
LEADS = {"x_video": ["marty", "babis"], "x_post": ["marty", "babis"], "video": ["marty"], "stream": ["peta"],
         "news": ["babis", "kalousek"], "other": ["marty"]}
HOME = {"marty": "socky", "babis": "dotace", "peta": "disko", "kalousek": "vina", "alenka": "kantyna", "bourak": "kantyna"}
REPLIERS = {"marty": ["babis", "peta"], "babis": ["marty", "kalousek"], "peta": ["marty", "bourak"], "kalousek": ["babis", "bourak"]}
TRANSIENT = ("quiet_hours", "wheel_live", "per_channel_gap")
# <= 2 sentences, no quotes, nothing about what the post says or about the real person: the persona only shares.
COMMENTS = {
    "marty": {"video": ["Nové video je venku, tohle stříhám rovnou na reels. Kdo dá první lajk?",
                        "Tohle jde hned do našeho feedu. Algoritmus, připrav se na OKÚ."],
              "post": ["Sdílím dál, ať to vidí celé OKÚ. Lajky se samy nenasbírají.",
                       "Tohle hodím do našich sociálních sítí. Kdo dá lajk jako první?"]},
    "babis": {"video": ["Nové video, kolegové. Sdílet, lajkovat, makáme!",
                        "Tohle si pusťte všichni. Pak chci vidět lajky v tabulce."],
              "post": ["Kolegové, sdílet a lajkovat. Chci vidět čísla.",
                       "Tohle ať vidí každý v OKÚ. Lajky na stůl, makáme."],
              "news": ["Tohle si přečtěte, kolegové. Na poradě chci k tomu vaše čísla."]},
    "peta": {"stream": ["Stream je na programu, disko se přesouvá k obrazovce. Kdo donese hranolky?",
                        "Dneska se kouká na stream. Playlist počká, hranolky ne."]},
    "kalousek": {"news": ["Čtu to a hned říkám: já za to nemůžu.", "Přikládám odkaz. Kdyby se něco pokazilo, já to nebyl."]},
}
GROUP = {"x_video": "video", "video": "video", "x_post": "post", "stream": "stream", "news": "news", "other": "post"}

def comment(persona, kind, rng):
    c = COMMENTS.get(persona, {}); g = GROUP.get(kind, "post")
    opts = c.get(g) or c.get("post") or c.get("video") or next(iter(c.values()), ["Sdílím, kolegové."])
    return rng.choice(opts)

def plan(key, kind, url, reply_rate=0.5, channels=None):
    """Deterministic reaction plan for a podnet. Pure."""
    channels = channels or wchatter.CHANNELS
    rng = random.Random(int(hashlib.sha256(f"PODNET:{key}".encode()).hexdigest()[:12], 16))
    leads = [p for p in LEADS.get(kind, LEADS["other"]) if HOME.get(p) in channels] or [p for p in HOME if HOME[p] in channels]
    if not leads: return None
    lead = rng.choice(leads)
    reps = [p for p in REPLIERS.get(lead, []) if p != lead]
    reply = rng.choice(reps) if reps and rng.random() < float(reply_rate) else None
    ch_name = HOME[lead]; text = comment(lead, kind, rng)
    return {"persona": lead, "reply_persona": reply, "channel_name": ch_name, "channel": channels[ch_name],
            "comment": text, "opener": f"{url}\n{text}", "turns": 2 if reply else 1, "llm_estimate": 1 if reply else 0,
            "rng_key": f"PODNET:{key}"}

class Reactor:
    """`svc` provides diary, world, settings(), logs_dir, outbox_dir, clock (like ChatterDirector)."""
    def __init__(self, svc): self.svc = svc

    def cfg(self):
        c = dict(DEFAULTS); c.update(self.svc.settings())
        ch = c.get("chatter_channels")
        c["chatter_channels"] = {k: v for k, v in (ch.items() if isinstance(ch, dict) else wchatter.CHANNELS.items()) if k in wchatter.CHANNELS and v}
        return c

    def tick(self, now=None):
        now = self.svc.clock() if now is None else now
        c = self.cfg(); out = []; d = self.svc.diary
        try: self.check_ack(now, c, out)
        except Exception as e: log.warning("podnet ack check failed: %s", type(e).__name__)
        if not c.get("podnet_react", True): return out
        off = int(d.kv_get("podnet:react_seq", 0) or 0)
        for r in d.events(after_seq=off, types=["podnet.received"], limit=50):
            if d.kv_get("podnet:pending"): break           # live: one hand-off at a time
            pl = r["payload"] or {}
            if pl.get("kind") == "news" and pl.get("source") in ("news_rss", "news_pplx"):   # news.py NewsDirector owns these
                d.kv_set("podnet:react_seq", r["seq"]); continue
            res = self.decide(r, now, c)
            if res == "wait": break
            if res: out.append(res)
            d.kv_set("podnet:react_seq", r["seq"])
        return [x for x in out if x]

    def reactions_today(self, rows, day0):
        return sum(1 for r in rows if r["ts"] >= day0 and r["type"] in ("podnet.requested", "podnet.dry_run") and not (r["payload"] or {}).get("test"))

    def decide(self, r, now, c):
        d, pl, key = self.svc.diary, r["payload"] or {}, r["subject"]
        base = {"podnet": key, "url": pl.get("url"), "kind": pl.get("kind"), "feeder": pl.get("source"), "test": bool(pl.get("test"))}
        par = [r["id"]]
        hit = guard.guard_hit(pl.get("title") or "", english=True)
        if hit:
            return d.record("podnet.skipped", None, key, dict(base, reason="guard:" + hit), source="director", parents=par, dedupe_key=f"podnet:decided:{key}")
        p = plan(key, pl.get("kind") or "other", pl.get("url"), float(c["podnet_reply_rate"]), c["chatter_channels"])
        if not p:
            return d.record("podnet.skipped", None, key, dict(base, reason="no_channel"), source="director", parents=par, dedupe_key=f"podnet:decided:{key}")
        zone = c.get("timezone", tz.PRAGUE); day0 = tz.day_start(now, zone)
        rows = d.events(since_ts=min(day0, now - float(c["per_channel_gap_h"]) * 3600) - 1)
        kill = budget.killed(c, self.svc.logs_dir, d.kv_get, now)
        snap_live = state.wheel_live(self.svc.world.state(), now)
        dec = budget.check("post", rows, now, c, channel=p["channel"], llm=p["llm_estimate"], wheel_live=snap_live, kill=kill)
        reasons = list(dec["reasons"]); n = self.reactions_today(rows, day0)
        if n >= int(c["podnet_reactions_per_day"]) and not base["test"]: reasons.append(f"podnet_reactions_per_day {n}/{c['podnet_reactions_per_day']}")
        participants = [x for x in (p["persona"], p["reply_persona"]) if x]
        info = dict(base, persona=p["persona"], reply_persona=p["reply_persona"], participants=participants, channel=p["channel"],
                    channel_name=p["channel_name"], turns=p["turns"], llm_estimate=p["llm_estimate"], rng_key=p["rng_key"], why=dec["why"])
        if reasons:
            if all(x.split(" ")[0] in TRANSIENT for x in reasons) and now - r["ts"] < float(c["podnet_max_age_h"]) * 3600:
                return "wait"
            if all(x.split(" ")[0] in TRANSIENT for x in reasons):
                return d.record("podnet.skipped", p["persona"], key, dict(info, reason="stale", reasons=reasons), source="director",
                                parents=par, dedupe_key=f"podnet:decided:{key}")
            return d.record("budget.denied", p["persona"], key, dict(info, storylet="PODNET", reasons=reasons), source="director",
                            parents=par, dedupe_key=f"podnet:decided:{key}")
        if c.get("dry_run", True):
            log.info("podnet %s DRY RUN: %s in %s (reply %s)", key, p["persona"], p["channel_name"], p["reply_persona"])
            return d.record("podnet.dry_run", p["persona"], key, dict(info, opener=p["opener"], would={"file": "meeting_start.jsonl",
                            "kind": "chatter", "source": "world:chatter", "top_level": True, "podnet": True}), source="director",
                            parents=par, dedupe_key=f"podnet:decided:{key}")
        if base["test"]:
            return d.record("podnet.skipped", p["persona"], key, dict(info, reason="test"), source="director", parents=par, dedupe_key=f"podnet:decided:{key}")
        briefs = getattr(self.svc, "briefs_for", lambda ps: {})(participants)
        req = handoff.request_chatter(p["channel"], participants, "Sdílený odkaz (podnet)", p["opener"], p["turns"], storylet="PODNET",
                                      slot=key, outbox_dir=self.svc.outbox_dir, clock=self.svc.clock,
                                      podnet={"url": pl.get("url"), "kind": pl.get("kind"), "key": key}, briefs=briefs, min_personas=1)
        row = d.record("podnet.requested", p["persona"], key, dict(info, request_id=req["id"], llm_calls=p["llm_estimate"], top_level=True,
                       opener_chars=len(p["opener"])), source="director", parents=par, dedupe_key=f"podnet:decided:{key}")
        d.kv_set("podnet:pending", json.dumps({"rid": req["id"], "key": key, "at": now, "parent": row["id"] if row else None,
                                               "personas": participants, "channel": p["channel"], "started": None}))
        log.info("podnet %s requested (id=%s) %s in %s", key, req["id"], p["persona"], p["channel_name"])
        return row

    def check_ack(self, now, c, out):
        d = self.svc.diary; raw = d.kv_get("podnet:pending")
        if not raw: return
        try: pend = json.loads(raw)
        except ValueError: d.kv_set("podnet:pending", None); return
        rid, key = pend["rid"], pend["key"]; par = [pend["parent"]] if pend.get("parent") else None
        base = {"podnet": key, "request_id": rid, "channel": pend.get("channel"), "participants": pend.get("personas")}
        lead = (pend.get("personas") or [None])[0]
        acks = {a.get("status"): a for a in handoff.acks_for(rid, self.svc.outbox_dir)}
        st = acks.get("started")
        if st and not pend.get("started"):
            r = d.record("podnet.started", lead, key, dict(base, thread_ts=st.get("thread_ts"), status="started"), source="director",
                         parents=par, dedupe_key=f"podnet:started:{rid}")
            out.append(r); pend["started"] = now; pend["started_id"] = r["id"] if r else None; d.kv_set("podnet:pending", json.dumps(pend))
        end = next((acks[s] for s in ("done", "stopped", "error") if s in acks), None)
        par2 = [pend["started_id"]] if pend.get("started_id") else par
        if end:
            t = "podnet.ended" if (st or pend.get("started")) else "podnet.failed"
            out.append(d.record(t, lead, key, dict(base, status=end.get("status"), turns=end.get("turns"), llm_calls_actual=end.get("llm_calls")),
                                source="director", parents=par2, dedupe_key=f"podnet:ended:{rid}")); d.kv_set("podnet:pending", None); return
        bad = next((acks[s] for s in acks if s not in ("done", "stopped", "error", "started")), None)
        if bad and not st:
            out.append(d.record("podnet.failed", lead, key, dict(base, status=bad.get("status"), reason=bad.get("reason")), source="director",
                                parents=par, dedupe_key=f"podnet:ended:{rid}")); d.kv_set("podnet:pending", None); return
        if not st and now - float(pend.get("at") or 0) > float(c["podnet_ack_timeout_s"]):
            out.append(d.record("podnet.failed", lead, key, dict(base, status="no_ack"), source="director", parents=par, dedupe_key=f"podnet:ended:{rid}"))
            d.kv_set("podnet:pending", None)
        elif st and now - float(pend.get("started") or now) > float(c["podnet_done_timeout_s"]):
            out.append(d.record("podnet.ended", lead, key, dict(base, status="timeout"), source="director", parents=par2, dedupe_key=f"podnet:ended:{rid}"))
            d.kv_set("podnet:pending", None)
