"""Rule-based consequences (ECOSYSTEM-PLAN P-006): diary rows -> `consequence.applied` rows.

Rules live in oku_slack/world/consequences.toml (env OKU_WORLD_CONSEQUENCES overrides; re-read when it changes).
The engine tails the diary (kv `consequence:seq`), matches every non-consequence row against the rules and writes one
`consequence.applied` row per (rule, trigger): source `consequence`, actor None, subject = the trigger's subject,
causal_parents = [trigger id], dedupe_key `consequence:<rule>:<trigger id>`, ts = the trigger's ts, payload
{rule, trigger, trigger_type, dry_run, effects: [{resource, delta} | {blame, n} | {relation: [from, to], delta}]}.
Targets are resolved when the row is written (event host from players.toml etc.), so the projection (state.py) folds
the rows without any config: rebuild == incremental == replay.

The same TOML also holds the projection seeds ([seeds]) and storylet thresholds ([thresholds]). Stdlib only."""
import itertools, logging, os, pathlib, tomllib
from . import state

log = logging.getLogger("oku_world.consequences")
PATH = pathlib.Path(__file__).with_name("consequences.toml")
SELECTORS = ("host", "missing", "actor", "lead", "others", "participants")

def path():
    return pathlib.Path(os.environ.get("OKU_WORLD_CONSEQUENCES") or PATH)

def load(p=None):
    """Parse + sanity-check the rules file. Returns {"rules", "include_dry_run", "seeds", "thresholds"}; raises ValueError."""
    with open(p or path(), "rb") as f: raw = tomllib.load(f)
    rules, ids = [], set()
    for r in raw.get("rule") or []:
        rid = r.get("id")
        if not isinstance(rid, str) or not rid or rid in ids: raise ValueError(f"rule id missing/duplicate: {rid!r}")
        ids.add(rid)
        on = r.get("on"); on = [on] if isinstance(on, str) else on
        if not on or not all(isinstance(t, str) for t in on): raise ValueError(f"{rid}: 'on' must list diary types")
        if r.get("resource") is not None and (r["resource"] not in state.RESOURCES or not isinstance(r.get("delta"), int)):
            raise ValueError(f"{rid}: bad resource/delta")
        rel = r.get("relation")
        if rel is not None and not (isinstance(rel, dict) and isinstance(rel.get("delta"), int) and rel.get("from") and rel.get("to")):
            raise ValueError(f"{rid}: bad relation")
        if r.get("blame") is not None and not isinstance(r["blame"], str): raise ValueError(f"{rid}: bad blame")
        if not (r.get("resource") or rel or r.get("blame")): raise ValueError(f"{rid}: no effect")
        where = r.get("where") or {}
        if not isinstance(where, dict): raise ValueError(f"{rid}: bad where")
        rules.append(dict(r, on=list(on), where={k: (v if isinstance(v, list) else [v]) for k, v in where.items()}))
    seeds = {k: int(v) for k, v in (raw.get("seeds") or {}).items() if k in state.RESOURCES}
    th = {k: float(v) for k, v in (raw.get("thresholds") or {}).items()}
    return {"rules": rules, "include_dry_run": bool(raw.get("include_dry_run", True)), "seeds": seeds, "thresholds": th,
            "version": raw.get("version")}

def _list(v): return [x for x in (v if isinstance(v, list) else [v] if v else []) if isinstance(x, str) and x]

def select(sel, ev, ctx=None):
    """Selector -> list of actor keys for this trigger row."""
    pl = ev.get("payload") or {}; ctx = ctx or {}
    parts = _list(pl.get("participants") or pl.get("personas"))
    lead = (parts[0] if parts else None) or pl.get("persona") or (ev.get("actor") if ev.get("actor") in state.PERSONAS else None)
    if sel == "host":
        h = pl.get("host") or (ctx.get("hosts") or {}).get(pl.get("key"))
        return [h] if h else []
    if sel == "missing": return _list(pl.get("missing"))
    if sel == "actor": return [ev["actor"]] if ev.get("actor") else []
    if sel == "lead": return [lead] if lead else []
    if sel in ("others", "participants"): return [p for p in parts if p != lead] if sel == "others" else parts
    return [sel]

def matches(rule, ev, include_dry_run=True):
    t = ev["type"]
    if t not in rule["on"] or t.startswith("consequence."): return False
    if t.endswith(".dry_run") and not include_dry_run: return False
    pl = ev.get("payload") or {}
    return all(pl.get(k) in allowed for k, allowed in rule["where"].items())

def effects(rule, ev, ctx=None):
    out = []
    if rule.get("resource"): out.append({"resource": rule["resource"], "delta": int(rule["delta"])})
    if rule.get("blame"):
        for who in (select(rule["blame"], ev, ctx) if rule["blame"] in SELECTORS else [rule["blame"]]):
            out.append({"blame": who[:64], "n": int(rule.get("blame_n", 1))})
    rel = rule.get("relation")
    if rel:
        a, b = select(rel["from"], ev, ctx), select(rel["to"], ev, ctx)
        pairs = itertools.permutations(a, 2) if rel["from"] == rel["to"] == "participants" else ((x, y) for x in a for y in b)
        for x, y in pairs:
            if x != y: out.append({"relation": [x[:64], y[:64]], "delta": int(rel["delta"])})
    return out[:40]

def derive(ev, cfg, ctx=None):
    """Consequence drafts for one diary row (pure)."""
    out = []
    for r in cfg["rules"]:
        if not matches(r, ev, cfg["include_dry_run"]): continue
        fx = effects(r, ev, ctx)
        if not fx: continue
        out.append({"type": "consequence.applied", "source": "consequence", "actor": None, "subject": ev.get("subject"),
                    "ts": ev["ts"], "causal_parents": [ev["id"]], "dedupe_key": f"consequence:{r['id']}:{ev['id']}",
                    "payload": {"rule": r["id"], "trigger": ev["id"], "trigger_type": ev["type"],
                                "dry_run": ev["type"].endswith(".dry_run"), "effects": fx}})
    return out

class Engine:
    """Tails the diary and writes consequence rows. `svc` provides diary, settings(), ctx."""
    def __init__(self, svc, rules_path=None):
        self.svc, self.path = svc, pathlib.Path(rules_path) if rules_path else None
        self._cfg, self._mtime, self.error, self.applied = None, None, None, 0

    def cfg(self):
        p = self.path or path()
        try: m = p.stat().st_mtime
        except OSError: m = None
        if self._cfg is None or m != self._mtime:
            try: self._cfg, self.error = load(p), None
            except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
                self.error = f"{type(e).__name__}: {e}"[:200]; log.warning("consequences.toml unusable: %s", self.error)
                self._cfg = self._cfg or {"rules": [], "include_dry_run": True, "seeds": {}, "thresholds": {}}
            self._mtime = m
        return self._cfg

    def tick(self, now=None, limit=5000):
        if not self.svc.settings().get("consequences_enabled", True): return []
        c, d = self.cfg(), self.svc.diary; out = []
        off = int(d.kv_get("consequence:seq", 0) or 0); last = off
        for ev in d.events(after_seq=off, limit=limit):
            last = ev["seq"]
            if ev["type"].startswith("consequence."): continue
            for dr in derive(ev, c, getattr(self.svc, "ctx", None)):
                st, row = d.ingest(dr)
                if st == "ok": out.append(row); self.applied += 1
        if last != off: d.kv_set("consequence:seq", last)
        return out
