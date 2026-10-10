"""Persona memory briefs (ECOSYSTEM-PLAN P-003): a compact, Czech, <= 600-char summary of what one persona
"remembers" from the diary projection: recent events it took part in, blame (its own and the team's top), last topics,
and relationships with the other personas and with players (from consequence rows).

Used by the world (hand-off lines for chatter/porada/podnet carry `briefs`) and by the bridge for normal replies via
GET /api/world/brief?persona=x (loopback; failure-isolated on the bridge side). Only diary metadata goes in: never
human message text. Pure functions over a projection state."""
from . import state, tz

LIMIT = 600
NAMES = {"babis": "Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa", "kalousek": "Kalousek",
         "monika": "Monika"}
WHEEL = {"wheel.done": "proběhla akce", "wheel.expired": "propadla akce", "wheel.vetoed": "vetována akce", "wheel.live": "živě akce",
         "wheel.spin": "kolo vylosovalo"}

def _d(ts):
    if not ts: return "?"
    d = tz.local(ts); return f"{d.day}.{d.month}."

def _n(k, names): return (names or {}).get(k) or NAMES.get(k) or k

def describe(m, names=None, titles=None):
    """One memory item -> short Czech phrase, or None when not worth a mention."""
    t, note, w = m.get("type", ""), m.get("note"), m.get("with") or []
    who = (" s " + ", ".join(_n(x, names) for x in w)) if w else ""
    ch = f" v #{m['channel']}" if m.get("channel") else ""
    if t in ("porada.started", "porada.dry_run"): return f"porada" + (f" ({m['topic'][:50]})" if m.get("topic") else "")
    if t in ("chatter.started", "chatter.dry_run", "chatter.ended"):
        return "debata" + ch + who + (f" ({m['topic'][:50]})" if m.get("topic") else "")
    if t in ("podnet.dry_run", "podnet.started", "podnet.requested"): return f"sdílený odkaz ({note or 'podnet'})" + ch + who
    if t in WHEEL:
        title = (titles or {}).get(note, note) or "?"
        miss = m.get("missing") or []
        return f"{WHEEL[t]} {title}" + (" (nepřišli: " + ", ".join(_n(x, names) for x in miss) + ")" if miss else "")
    if t == "command.used": return f"nabitý příkaz od {_n(m.get('about'), names)}"
    return None

def _signed(v): return f"+{v}" if v > 0 else f"−{abs(v)}"

def brief(st, persona, names=None, titles=None, limit=LIMIT):
    """<= limit chars, '' for an unknown persona."""
    if persona not in state.PERSONAS: return ""
    parts = []
    seen, recent = set(), []
    for m in reversed(st.get("memory", {}).get(persona) or []):
        if m.get("type", "").endswith((".requested", ".failed", ".skipped")) or m.get("type") in ("schedule.due", "budget.denied"): continue
        s = describe(m, names, titles)
        if not s or s in seen: continue
        seen.add(s); recent.append(f"{_d(m.get('ts'))} {s}")
        if len(recent) >= 4: break
    if recent: parts.append("Nedávno: " + "; ".join(recent) + ".")
    blame = st.get("blame") or {}
    mine = blame.get(persona, 0)
    top = sorted(((v, k) for k, v in blame.items() if v > 0), key=lambda x: (-x[0], x[1]))[:2]
    if mine or top:
        b = f"Vina: ty {mine}" + ("; nejvíc " + ", ".join(f"{_n(k, names)} {v}" for v, k in top) if top else "") + "."
        parts.append(b)
    rel = (st.get("relations") or {}).get(persona) or {}
    pers = sorted(((v, k) for k, v in rel.items() if k in state.PERSONAS and v), key=lambda x: (-abs(x[0]), x[1]))[:4]
    if pers: parts.append("Vztahy: " + ", ".join(f"{_n(k, names)} {_signed(v)}" for v, k in pers) + ".")
    grudges = sorted(((v, k) for k, v in rel.items() if k not in state.PERSONAS and v), key=lambda x: (x[0], x[1]))[:3]
    if grudges: parts.append("Hráči: " + ", ".join(f"{_n(k, names)} {_signed(v)}" for v, k in grudges) + ".")
    against = sorted(((rv.get(persona, 0), k) for k, rv in (st.get("relations") or {}).items() if k != persona and rv.get(persona, 0) < 0))[:2]
    if against: parts.append("Křivdu na tebe má: " + ", ".join(f"{_n(k, names)} {_signed(v)}" for v, k in against) + ".")
    topics = []
    for m in reversed(st.get("memory", {}).get(persona) or []):
        tp = m.get("topic")
        if tp and tp not in topics: topics.append(tp)
        if len(topics) >= 2: break
    if topics: parts.append("Poslední témata: " + " | ".join(t[:70] for t in topics) + ".")
    calls = ((st.get("bridge") or {}).get("by_persona") or {}).get(persona)
    if calls: parts.append(f"Odpovědí na Slacku celkem: {calls}.")
    r = st.get("resources") or {}
    if r: parts.append("Stav OKÚ: Dotace {dotace}, Kampaň {kampan}/100, Hranolky {hranolky}, Lajky {lajky}.".format(
        **{k: int(r.get(k, 0)) for k in ("dotace", "kampan", "hranolky", "lajky")}))
    out = ""
    for p in parts:
        if len(out) + len(p) + 1 > limit: continue
        out = (out + " " + p).strip()
    return out[:limit]

def briefs(st, personas, names=None, titles=None, limit=LIMIT):
    return {p: b for p in personas if (b := brief(st, p, names, titles, limit))}
