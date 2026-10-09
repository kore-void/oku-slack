"""World projection: a pure, rebuildable fold over the diary (plan 1.3). Read-only: it never writes rows.

    state = initial(ctx); for ev in diary: apply(state, ev, ctx)   ==   project(diary, ctx)

Covers resources (Dotace/Kampaň/Hranolky/Lajky; they move only through `world.delta`/`consequence.delta` rows,
which nothing writes before P-006), blame with evidence, actors (players, personas, external), per-player stats
and witnessed acts, per-persona memory (the <= 20 newest rows a persona witnessed), bridge activity, the scheduled
porada, per-source counters and the wheel fold (spun/live/missed/blame; any wheel row is optional input).
`ctx` carries config facts (event hosts, players' command personas, seeds) so the fold stays pure."""
import copy

REGIMES = {"A_scarce": "režim A (vzácné koruny)", "B_abundant": "režim B (hojné koruny)", "C_no_currency": "režim C (bez měny)"}
RESOURCES = {  # key: (seed, range or None, emoji, Czech label)
    "dotace": (5000, None, "💶", "Dotace"),
    "kampan": (35, (0, 100), "📣", "Kampaň"),
    "hranolky": (80, (0, 200), "🍟", "Hranolky"),
    "lajky": (1200, None, "👍", "Lajky"),
}
PERSONAS = ("babis", "alenka", "bourak", "marty", "peta", "kalousek", "monika")
HUMAN_VIA = {"slack", "room"}
PLAYER_STATS = ("spins", "confirms", "confirms_on_time", "missed", "bets", "bets_won", "bets_lost", "wagered", "won",
                "commands", "votes", "catches", "quiz_right", "quiz_wrong", "reactions", "ui")
MAX_ACTS = 20    # witnessed acts kept per player per persona (newest last)
MAX_MEMORY = 20  # memory rows kept per persona (newest last)
WHEEL_STATES = ("live", "done", "expired", "vetoed")

def ctx_from_cfg(cfg):
    """Config facts for the fold from a wheel config dict (players.toml): event hosts/titles, players, seeds."""
    cfg = cfg or {}
    return {"hosts": {e["key"]: e.get("host", "") for e in cfg.get("events", [])},
            "titles": {e["key"]: e.get("title", e["key"]) for e in cfg.get("events", [])},
            "players": {k: {"name": v.get("name", k), "persona": v.get("command_persona"), "effects": list(v.get("command_effects", []))}
                        for k, v in cfg.get("players", {}).items()},
            "seeds": {k: cfg.get("settings", {}).get(f"world_{k}", v[0]) for k, v in RESOURCES.items()}}

def _new_player(): return {"stats": dict.fromkeys(PLAYER_STATS, 0), "witnessed_acts": {}, "last_action": None}

def initial(ctx=None):
    ctx = ctx or {}; seeds = ctx.get("seeds") or {}
    return {"as_of_seq": 0, "as_of_ts": None, "regime": None,
            "resources": {k: seeds.get(k, v[0]) for k, v in RESOURCES.items()},
            "blame": {}, "blame_evidence": {},
            "players": {p: _new_player() for p in (ctx.get("players") or {})},
            "personas": {},  # host persona -> wheel outcome counters {"spun", "live", "done", "expired", "vetoed"}
            "actors": {},    # actor -> {"kind", "events", "last_ts", "by_source"}
            "memory": {},    # persona -> [{"we", "type", "ts", "about", "note"}] newest last
            "bridge": {"calls": 0, "by_persona": {}, "by_kind": {}, "last_ts": None},
            "porada": {"due": 0, "dry_run": 0, "requested": 0, "started": 0, "failed": 0, "denied": 0, "last": None},
            "sources": {},   # source -> {"events", "last_ts"}
            "totals": {"spun": 0, "live": 0, "done": 0, "expired": 0, "vetoed": 0, "bets": 0, "commands": 0, "reactions": 0, "human_actions": 0},
            "events": {},    # wheel event id -> {"key", "state", "ts"}
            "recent": []}    # last wheel outcomes [{"we", "key", "state", "ts"}]

def _player(st, p):
    if p not in st["players"]: st["players"][p] = _new_player()
    return st["players"][p]

def _blame(st, who, n, we):
    st["blame"][who] = st["blame"].get(who, 0) + n
    ev = st["blame_evidence"].setdefault(who, []); ev.append(we); del ev[:-10]

def _witness(st, p, persona, act, ts):
    if not persona: return
    acts = _player(st, p)["witnessed_acts"].setdefault(persona, []); acts.append({"act": act, "ts": ts}); del acts[:-MAX_ACTS]

def _persona(st, k):
    return st["personas"].setdefault(k or "?", {"spun": 0, "live": 0, "done": 0, "expired": 0, "vetoed": 0})

def _remember(st, persona, ev, note=None):
    if persona not in PERSONAS: return
    mem = st["memory"].setdefault(persona, [])
    if mem and mem[-1]["we"] == ev.get("id"): return
    mem.append({"we": ev.get("id"), "type": ev["type"], "ts": ev.get("ts"), "about": ev.get("actor"), "note": note})
    del mem[:-MAX_MEMORY]

def _actor(st, a, src, ts, ctx):
    if not a: return
    kind = "persona" if a in PERSONAS else "ext" if ":" in a else "player"
    x = st["actors"].setdefault(a, {"kind": kind, "events": 0, "last_ts": None, "by_source": {}})
    x["events"] += 1; x["last_ts"] = ts; x["by_source"][src] = x["by_source"].get(src, 0) + 1

def _wheel_eid(subject):
    s = subject or ""
    if s.startswith("wheel:"): s = s[6:]
    return s.split(":", 1)[1] if s.startswith("event:") else None

def witnesses(ev, ctx=None):
    """Personas that 'saw' this diary row (memory rule, plan 1.7)."""
    ctx = ctx or {}; pl = ev.get("payload") or {}; out = []
    for k in (ev.get("actor"), pl.get("host"), pl.get("persona"), pl.get("chair")):
        if k in PERSONAS and k not in out: out.append(k)
    if not pl.get("host") and pl.get("key"):
        h = (ctx.get("hosts") or {}).get(pl.get("key"))
        if h in PERSONAS and h not in out: out.append(h)
    for k in pl.get("participants") or []:
        if k in PERSONAS and k not in out: out.append(k)
    return out

def apply(st, ev, ctx=None):
    """Fold one diary row into the state (mutates and returns it). Unknown types only advance as_of."""
    ctx = ctx or {}; t, a, pl, ts, we = ev["type"], ev.get("actor"), ev.get("payload") or {}, ev.get("ts"), ev.get("id")
    src = ev.get("source") or "?"
    st["as_of_seq"], st["as_of_ts"], st["regime"] = ev.get("seq", st["as_of_seq"]), ts, ev.get("regime") or st["regime"]
    s = st["sources"].setdefault(src, {"events": 0, "last_ts": None}); s["events"] += 1; s["last_ts"] = ts
    _actor(st, a, src, ts, ctx)
    key = pl.get("key"); host = pl.get("host") or (ctx.get("hosts") or {}).get(key, "")
    eid = _wheel_eid(ev.get("subject"))
    via = pl.get("via") or src
    if a and via in HUMAN_VIA and a not in PERSONAS:
        st["totals"]["human_actions"] += 1; _player(st, a)["last_action"] = ts
    for p in witnesses(ev, ctx): _remember(st, p, ev)
    if t == "wheel.spin":
        st["totals"]["spun"] += 1; _persona(st, host)["spun"] += 1
        if a: _player(st, a)["stats"]["spins"] += 1
        if eid: st["events"][eid] = {"key": key, "state": "pending", "ts": ts}
    elif t == "wheel.confirm" and a:
        sp = _player(st, a)["stats"]; sp["confirms"] += 1; sp["confirms_on_time"] += 1 if pl.get("on_time") else 0
        _witness(st, a, host, f"confirmed:{key}:{we}", ts)
    elif t in ("wheel.live", "wheel.done", "wheel.expired", "wheel.vetoed"):
        state = t.split(".", 1)[1]; st["totals"][state] += 1; _persona(st, host)[state] += 1
        if eid: st["events"][eid] = {"key": key, "state": state, "ts": ts}
        if state != "live":
            st["recent"].append({"we": we, "key": key, "state": state, "ts": ts}); del st["recent"][:-10]
        if state == "expired":  # read-only interpretation: whoever did not confirm "missed" it, witnessed by the host
            for p in pl.get("missing") or []:
                _player(st, p)["stats"]["missed"] += 1; _blame(st, p, 1, we); _witness(st, p, host, f"missed:{key}:{we}", ts)
        if state == "vetoed" and a:
            _blame(st, a, 1, we); _witness(st, a, host, f"vetoed:{key}:{we}", ts)
    elif t == "bet.placed" and a:
        sp = _player(st, a)["stats"]; sp["bets"] += 1; sp["wagered"] += int(pl.get("amount", 0) or 0); st["totals"]["bets"] += 1
    elif t == "bet.settled" and a:
        sp = _player(st, a)["stats"]
        if pl.get("state") == "won": sp["bets_won"] += 1; sp["won"] += int(pl.get("payout", 0) or 0)
        else: sp["bets_lost"] += 1
    elif t == "command.used" and a:
        _player(st, a)["stats"]["commands"] += 1; st["totals"]["commands"] += 1
        pc = (ctx.get("players") or {}).get(a, {}); persona = pl.get("persona") or pc.get("persona")
        _witness(st, a, persona, f"command:{pl.get('label', '')}:{we}", ts)
        for r in pl.get("effects") or []:
            if r.get("effect") == "steal_points" and r.get("ok"):  # "Kalousek za to může": the persona takes the blame
                _blame(st, persona or "kalousek", 1, we)
    elif t == "show.vote" and a: _player(st, a)["stats"]["votes"] += 1
    elif t == "show.catch" and a: _player(st, a)["stats"]["catches"] += 1
    elif t == "show.quiz" and a: _player(st, a)["stats"]["quiz_right" if pl.get("ok") else "quiz_wrong"] += 1
    elif t == "reaction":
        st["totals"]["reactions"] += 1
        if a: _player(st, a)["stats"]["reactions"] += 1
    elif t == "ui.kolo" and a: _player(st, a)["stats"]["ui"] += 1
    elif t == "bridge.reply":
        b = st["bridge"]; b["calls"] += 1; b["last_ts"] = ts
        if a: b["by_persona"][a] = b["by_persona"].get(a, 0) + 1
        k = pl.get("kind") or "?"; b["by_kind"][k] = b["by_kind"].get(k, 0) + 1
    elif t.startswith("porada.") or t == "schedule.due" and pl.get("storylet") == "PORADA":
        p = st["porada"]; kind = "due" if t == "schedule.due" else t.split(".", 1)[1]
        if kind in p: p[kind] += 1
        p["last"] = {"we": we, "type": t, "ts": ts, "topic": pl.get("topic"), "status": pl.get("status")}
    elif t == "budget.denied" and pl.get("storylet") == "PORADA":
        st["porada"]["denied"] += 1
        st["porada"]["last"] = {"we": we, "type": t, "ts": ts, "topic": pl.get("topic"), "status": ",".join(pl.get("reasons") or [])}
    elif t in ("world.delta", "consequence.delta"):  # P-006 consequences; nothing writes them yet
        k = pl.get("resource")
        if k in st["resources"]:
            v = st["resources"][k] + int(pl.get("delta", 0) or 0); rng = RESOURCES[k][1]
            st["resources"][k] = max(rng[0], min(rng[1], v)) if rng else v
        if pl.get("blame") and isinstance(pl.get("blame"), str): _blame(st, pl["blame"], int(pl.get("blame_n", 1) or 1), we)
    return st

def project(events, ctx=None, base=None):
    """Fold events onto `base` (a previous state, copied) or onto initial(ctx)."""
    st = copy.deepcopy(base) if base is not None else initial(ctx)
    for ev in events: apply(st, ev, ctx)
    return st

def metrics(events, since_ts):
    """Window counters (e.g. last 7 days) straight from diary rows."""
    m = {"spun": 0, "live": 0, "done": 0, "expired": 0, "bets": 0, "commands": 0, "human_actions": 0, "reactions": 0,
         "bridge_calls": 0, "porady": 0, "dry_runs": 0, "budget_denied": 0, "events": 0}
    for ev in events:
        if ev["ts"] < since_ts: continue
        t = ev["type"]; m["events"] += 1; pl = ev.get("payload") or {}
        if t == "wheel.spin": m["spun"] += 1
        elif t in ("wheel.live", "wheel.done", "wheel.expired"): m[t.split(".")[1]] += 1
        elif t == "bet.placed": m["bets"] += 1
        elif t == "command.used": m["commands"] += 1
        elif t == "reaction": m["reactions"] += 1
        elif t == "bridge.reply": m["bridge_calls"] += 1
        elif t == "porada.started": m["porady"] += 1
        elif t.endswith(".dry_run"): m["dry_runs"] += 1
        elif t == "budget.denied": m["budget_denied"] += 1
        if ev.get("actor") and (pl.get("via") or ev.get("source")) in HUMAN_VIA and ev.get("actor") not in PERSONAS: m["human_actions"] += 1
    m["live_rate"] = round(m["live"] / m["spun"], 2) if m["spun"] else None
    return m

def wheel_live(st, now, max_age_s=3 * 3600):
    """True when the projection says a wheel event is live right now (budget: no autonomous posts during it)."""
    return any(v.get("state") == "live" and (v.get("ts") or 0) >= now - max_age_s for v in st["events"].values())

def player_acts(st, p, n=3):
    acts = []
    for persona, items in (st["players"].get(p, {}).get("witnessed_acts") or {}).items():
        for it in items: acts.append({"ts": it["ts"] or 0, "persona": persona, "act": it["act"]})
    acts.sort(key=lambda x: -x["ts"])
    return acts[:n]

class World:
    """Diary + cached incremental projection."""
    def __init__(self, diary, ctx=None, clock=None):
        import threading, time
        self.log, self.ctx, self.clock = diary, ctx or {}, clock or time.time
        self._st = initial(self.ctx); self.lock = threading.RLock()

    def state(self):
        with self.lock:
            new = self.log.events(after_seq=self._st["as_of_seq"])
            if new: self._st = project(new, self.ctx, base=self._st)
            return self._st

    def rebuild(self):
        """Throw the cache away and fold the whole diary from scratch."""
        with self.lock:
            self._st = project(self.log.events(), self.ctx); return self._st

    def snapshot(self, days=7, players=True):
        st = self.state(); now = self.clock()
        snap = {"as_of_seq": st["as_of_seq"], "regime": st["regime"] or self.log.regime, "resources": dict(st["resources"]),
                "blame": dict(sorted(st["blame"].items(), key=lambda kv: (-kv[1], kv[0]))),
                f"metrics_{days}d": metrics(self.log.events(since_ts=now - days * 86400), now - days * 86400),
                "totals": dict(st["totals"]), "bridge": copy.deepcopy(st["bridge"]), "porada": copy.deepcopy(st["porada"]),
                "sources": copy.deepcopy(st["sources"]), "actors": copy.deepcopy(st["actors"]),
                "memory": {k: v[-5:] for k, v in st["memory"].items()},
                "personas": copy.deepcopy(st["personas"]), "recent": list(st["recent"]),
                "wheel_live": wheel_live(st, now)}
        if players:
            snap["players"] = {p: {"stats": dict(v["stats"]), "recent_acts": player_acts(st, p)} for p, v in st["players"].items()}
        return snap
