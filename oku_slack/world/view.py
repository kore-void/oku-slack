"""Czech text surfaces for the world state (no new channel posts): `/kolo svet` reply, one panel context line,
canvas section "Stav světa". Pure functions over a World + the wheel engine."""
import time
from .state import REGIMES, RESOURCES

PERSONAS = {"babis": "Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa",
            "kalousek": "Kalousek", "monika": "Monika"}
ACTS = {"missed": "propásl(a) jsi", "confirmed": "potvrdil(a) jsi", "vetoed": "vetoval(a) jsi", "command": "použil(a) jsi příkaz"}
OUTCOME = {"done": "proběhla", "expired": "propadla", "vetoed": "vetována"}

def num(n): return f"{int(n):,}".replace(",", "\u00a0")
def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))
def _dm(ts): lt = time.localtime(ts); return f"{lt.tm_mday}.{lt.tm_mon}."

def who(eng, k): return eng.cfg["players"].get(k, {}).get("name") or PERSONAS.get(k, k)

def resources_line(res, sep=" · ", bold=False):
    out = []
    for k, (_, rng, emo, label) in RESOURCES.items():
        v = num(res.get(k, 0)) + (f"/{rng[1]}" if rng and k == "kampan" else "")
        out.append(f"{emo} {label}{':' if bold else ''} {'**' + v + '**' if bold else v}")
    return sep.join(out)

def blame_line(eng, blame, n=5):
    rows = [f"{who(eng, k)} {v}" for k, v in sorted(blame.items(), key=lambda kv: (-kv[1], kv[0])) if v > 0][:n]
    return " · ".join(rows) or "zatím nikdo"

def act_text(eng, act, ts):
    kind, _, rest = act.partition(":"); arg = rest.rsplit(":", 1)[0]
    titles = {e["key"]: e["title"] for e in eng.cfg["events"]}
    obj = arg if kind == "command" else titles.get(arg, arg)
    return f"{ACTS.get(kind, kind)} *{obj}* ({_dm(ts)})"

def svet_text(eng, p=None, now=None):
    """Ephemeral `/kolo svet` reply (plan section 5 sample)."""
    w = eng.world; now = eng.clock() if now is None else now
    korun = {k: eng.eco.balance(k) for k in eng.cfg["players"]}
    snap = w.snapshot(korun=korun); m = snap["metrics_7d"]
    rate = f"{round(100 * m['live'] / m['spun'])} %" if m["spun"] else "—"
    lt = time.localtime(now)
    lines = [f"🌍 *Stav světa OKÚ* · {REGIMES.get(snap['regime'], snap['regime'])} · {lt.tm_mday:02d}.{lt.tm_mon:02d}. {_hm(now)}",
             resources_line(snap["resources"]),
             f"🫵 Vina: {blame_line(eng, snap['blame'])}",
             f"📊 Za 7 dní: {m['spun']}× točeno, {m['live']}× živě ({rate}), {m['bets']} sázky, {m['commands']}× nabitý příkaz, {m['human_actions']} akcí hráčů"]
    if p:
        acts = []
        for persona, items in (w.state()["players"].get(p, {}).get("witnessed_acts") or {}).items():
            for it in items: acts.append((it["ts"] or 0, persona, it["act"]))
        acts.sort(reverse=True)
        mine = "; ".join(f"{PERSONAS.get(per, per)}: {act_text(eng, a, ts)}" for ts, per, a in acts[:3])
        lines.append(f"🧾 Tvoje skutky, které si postavy pamatují: {mine or 'zatím žádné'}")
        lines.append(f"🪙 Tvůj zůstatek: {num(korun.get(p, 0))} OKÚ korun")
    lines.append("_Svět zatím jen pozoruje: důsledky přijdou v další fázi._")
    return "\n".join(lines)

def panel_line(eng):
    st = eng.world.state(); r, tot = st["resources"], st["totals"]
    return (f"🌍 Kampaň {num(r['kampan'])} · Hranolky {num(r['hranolky'])} · Dotace {num(r['dotace'])} · "
            f"živých událostí {tot['live']}/{tot['spun']} · /kolo svet")

def canvas_section(eng):
    st = eng.world.state(); titles = {e["key"]: e["title"] for e in eng.cfg["events"]}
    rec = [f"{titles.get(x['key'], x['key'])} {OUTCOME.get(x['state'], x['state'])} ({_hm(x['ts'])})" for x in reversed(st["recent"][-3:]) if x.get("ts")]
    return ["## 🌍 Stav světa", f"- {resources_line(st['resources'], bold=True)}", f"- 🫵 Vina: {blame_line(eng, st['blame'])}",
            f"- Živých událostí: **{st['totals']['live']}/{st['totals']['spun']}** · `/kolo svet`",
            f"- Poslední události: {', '.join(rec) or '—'}", ""]
