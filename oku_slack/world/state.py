"""World state v0: a pure, rebuildable fold over the world_events diary (ECOSYSTEM-PLAN.md 3.3, P-001 subset).
No consequences yet (P-002): resources stay at their seeds unless a `world.delta` event exists (none is written in
P-001). Blame and witnessed acts are a read-only interpretation of what the diary says happened, nothing more.

    state = initial(seeds); for ev in diary: apply(state, ev, ctx)   ==   project(diary, ctx)

`ctx` carries config facts the fold needs (event hosts, players' command personas) so the fold stays pure."""
import copy

REGIMES = {"A_scarce": "režim A (vzácné koruny)", "B_abundant": "režim B (hojné koruny)", "C_no_currency": "režim C (bez měny)"}
RESOURCES = {  # key: (seed, range or None, emoji, Czech label)
    "dotace": (5000, None, "💶", "Dotace"),
    "kampan": (35, (0, 100), "📣", "Kampaň"),
    "hranolky": (80, (0, 200), "🍟", "Hranolky"),
    "lajky": (1200, None, "👍", "Lajky"),
}
HUMAN_SOURCES = {"slack", "room"}
PLAYER_STATS = ("spins", "confirms", "confirms_on_time", "missed", "bets", "bets_won", "bets_lost", "wagered", "won",
                "commands", "votes", "catches", "quiz_right", "quiz_wrong", "reactions", "ui")
MAX_ACTS = 20  # witnessed acts kept per player per persona (newest last)

def ctx_from_cfg(cfg):
    """Config facts for the fold: event key -> host persona, player -> command persona/effects, player names."""
    return {"hosts": {e["key"]: e.get("host", "") for e in cfg.get("events", [])},
            "titles": {e["key"]: e.get("title", e["key"]) for e in cfg.get("events", [])},
            "players": {k: {"name": v.get("name", k), "persona": v.get("command_persona"), "effects": list(v.get("command_effects", []))}
                        for k, v in cfg.get("players", {}).items()},
            "seeds": {k: cfg.get("settings", {}).get(f"world_{k}", v[0]) for k, v in RESOURCES.items()}}

def initial(ctx=None):
    ctx = ctx or {}; seeds = ctx.get("seeds") or {}
    return {"as_of_seq": 0, "as_of_ts": None, "regime": None,
            "resources": {k: seeds.get(k, v[0]) for k, v in RESOURCES.items()},
            "blame": {}, "blame_evidence": {},
            "players": {p: {"stats": dict.fromkeys(PLAYER_STATS, 0), "witnessed_acts": {}, "last_action": None} for p in (ctx.get("players") or {})},
            "personas": {},  # host persona -> {"spun", "live", "done", "expired", "vetoed"}
            "totals": {"spun": 0, "live": 0, "done": 0, "expired": 0, "vetoed": 0, "bets": 0, "commands": 0, "reactions": 0, "human_actions": 0},
            "events": {},    # wheel event id -> {"key", "state", "ts"} (outcome per event, for recaps)
            "recent": []}    # last outcomes [{"we", "key", "state", "ts"}]

def _player(st, p):
    if p not in st["players"]: st["players"][p] = {"stats": dict.fromkeys(PLAYER_STATS, 0), "witnessed_acts": {}, "last_action": None}
    return st["players"][p]

def _blame(st, who, n, we):
    st["blame"][who] = st["blame"].get(who, 0) + n
    ev = st["blame_evidence"].setdefault(who, []); ev.append(we); del ev[:-10]

def _witness(st, p, persona, act, ts):
    if not persona: return
    acts = _player(st, p)["witnessed_acts"].setdefault(persona, []); acts.append({"act": act, "ts": ts}); del acts[:-MAX_ACTS]

def _persona(st, k):
    return st["personas"].setdefault(k or "?", {"spun": 0, "live": 0, "done": 0, "expired": 0, "vetoed": 0})

def apply(st, ev, ctx=None):
    """Fold one diary event into the state (mutates and returns it). Unknown types only advance as_of."""
    ctx = ctx or {}; t, a, pl, ts, we = ev["type"], ev.get("actor"), ev.get("payload") or {}, ev.get("ts"), ev.get("id")
    st["as_of_seq"], st["as_of_ts"], st["regime"] = ev.get("seq", st["as_of_seq"]), ts, ev.get("regime") or st["regime"]
    key = pl.get("key"); host = pl.get("host") or (ctx.get("hosts") or {}).get(key, "")
    eid = (ev.get("subject") or "").split(":", 1)[1] if (ev.get("subject") or "").startswith("event:") else None
    if a and ev.get("source") in HUMAN_SOURCES:
        st["totals"]["human_actions"] += 1; _player(st, a)["last_action"] = ts
    if t == "wheel.spin":
        st["totals"]["spun"] += 1; _persona(st, host)["spun"] += 1
        if a: _player(st, a)["stats"]["spins"] += 1
        if eid: st["events"][eid] = {"key": key, "state": "pending", "ts": ts}
    elif t == "wheel.confirm" and a:
        s = _player(st, a)["stats"]; s["confirms"] += 1; s["confirms_on_time"] += 1 if pl.get("on_time") else 0
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
        s = _player(st, a)["stats"]; s["bets"] += 1; s["wagered"] += int(pl.get("amount", 0)); st["totals"]["bets"] += 1
    elif t == "bet.settled" and a:
        s = _player(st, a)["stats"]
        if pl.get("state") == "won": s["bets_won"] += 1; s["won"] += int(pl.get("payout", 0))
        else: s["bets_lost"] += 1
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
    elif t == "world.delta":  # written by P-002 consequences; nothing writes it in P-001
        k = pl.get("resource")
        if k in st["resources"]:
            v = st["resources"][k] + int(pl.get("delta", 0)); rng = RESOURCES[k][1]
            st["resources"][k] = max(rng[0], min(rng[1], v)) if rng else v
    return st

def project(events, ctx=None, base=None):
    """Fold events onto `base` (a previous state, copied) or onto initial(ctx)."""
    st = copy.deepcopy(base) if base is not None else initial(ctx)
    for ev in events: apply(st, ev, ctx)
    return st

def metrics(events, since_ts):
    """Window counters (e.g. last 7 days) straight from diary rows."""
    m = {"spun": 0, "live": 0, "done": 0, "expired": 0, "bets": 0, "commands": 0, "human_actions": 0, "reactions": 0}
    for ev in events:
        if ev["ts"] < since_ts: continue
        t = ev["type"]
        if t == "wheel.spin": m["spun"] += 1
        elif t in ("wheel.live", "wheel.done", "wheel.expired"): m[t.split(".")[1]] += 1
        elif t == "bet.placed": m["bets"] += 1
        elif t == "command.used": m["commands"] += 1
        elif t == "reaction": m["reactions"] += 1
        if ev.get("actor") and ev.get("source") in HUMAN_SOURCES: m["human_actions"] += 1
    m["live_rate"] = round(m["live"] / m["spun"], 2) if m["spun"] else None
    return m

class World:
    """Diary + cached incremental projection. snapshot() also persists kv world:snapshot (plan section 5)."""
    def __init__(self, wlog, cfg, store, clock):
        self.log, self.cfg, self.store, self.clock = wlog, cfg, store, clock
        self.ctx = ctx_from_cfg(cfg); self._st = initial(self.ctx)

    def state(self):
        new = self.log.events(after_seq=self._st["as_of_seq"])
        if new: self._st = project(new, self.ctx, base=self._st)
        return self._st

    def rebuild(self):
        """Throw the cache away and fold the whole diary from scratch."""
        self._st = project(self.log.events(), self.ctx); return self._st

    def snapshot(self, korun=None, days=7):
        import json
        st = self.state(); now = self.clock()
        snap = {"as_of_seq": st["as_of_seq"], "regime": st["regime"] or self.log.regime, "resources": dict(st["resources"]),
                "blame": dict(sorted(st["blame"].items(), key=lambda kv: (-kv[1], kv[0]))),
                f"metrics_{days}d": metrics(self.log.events(since_ts=now - days * 86400), now - days * 86400),
                "totals": dict(st["totals"]),
                "players": {p: {"stats": dict(v["stats"]), **({"korun": korun[p]} if korun and p in korun else {})} for p, v in st["players"].items()},
                "personas": copy.deepcopy(st["personas"])}
        try: self.store.kv_set("world:snapshot", json.dumps(snap, ensure_ascii=False))
        except Exception: pass
        return snap
