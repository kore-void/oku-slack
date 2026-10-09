"""Czech text surfaces for the world state, rendered from a world snapshot (oku_world GET /api/world).
`/kolo svet` in the wheel fetches the snapshot from the world service (loopback, short timeout, no proxies) and
falls back to a friendly line when oku_world is not running: the wheel never computes world state itself.
P-002 moves this to `/oku svet` on its own Slack app."""
import json, os, time, urllib.parse, urllib.request
from .state import REGIMES, RESOURCES

PERSONAS = {"babis": "Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa",
            "kalousek": "Kalousek", "monika": "Monika"}
ACTS = {"missed": "propásl(a) jsi", "confirmed": "potvrdil(a) jsi", "vetoed": "vetoval(a) jsi", "command": "použil(a) jsi příkaz"}
OUTCOME = {"done": "proběhla", "expired": "propadla", "vetoed": "vetována"}
WORLD_URL = "http://127.0.0.1:8798"
DOWN = "🌍 Svět OKÚ teď neodpovídá (služba oku_world neběží). Kolo funguje dál."

def num(n): return f"{int(n):,}".replace(",", "\u00a0")
def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))
def _dm(ts): lt = time.localtime(ts); return f"{lt.tm_mday}.{lt.tm_mon}."

def resources_line(res, sep=" · ", bold=False):
    out = []
    for k, (_, rng, emo, label) in RESOURCES.items():
        v = num(res.get(k, 0)) + (f"/{rng[1]}" if rng and k == "kampan" else "")
        out.append(f"{emo} {label}{':' if bold else ''} {'**' + v + '**' if bold else v}")
    return sep.join(out)

def blame_line(blame, names=None, n=5):
    names = names or {}
    rows = [f"{names.get(k) or PERSONAS.get(k, k)} {v}" for k, v in sorted(blame.items(), key=lambda kv: (-kv[1], kv[0])) if v > 0][:n]
    return " · ".join(rows) or "zatím nikdo"

def act_text(act, ts, titles=None):
    kind, _, rest = act.partition(":"); arg = rest.rsplit(":", 1)[0]
    obj = arg if kind == "command" else (titles or {}).get(arg, arg)
    return f"{ACTS.get(kind, kind)} *{obj}* ({_dm(ts)})"

def svet_text(snap, names=None, titles=None, p=None, now=None, korun=None):
    """Ephemeral world summary from a snapshot dict (pure)."""
    now = time.time() if now is None else now; m = snap.get("metrics_7d") or {}
    rate = f"{round(100 * m.get('live', 0) / m['spun'])} %" if m.get("spun") else "—"
    lt = time.localtime(now); por = snap.get("porada") or {}
    lines = [f"🌍 *Stav světa OKÚ* · {REGIMES.get(snap.get('regime'), snap.get('regime'))} · {lt.tm_mday:02d}.{lt.tm_mon:02d}. {_hm(now)}",
             resources_line(snap.get("resources") or {}),
             f"🫵 Vina: {blame_line(snap.get('blame') or {}, names)}",
             f"📊 Za 7 dní: {m.get('spun', 0)}× točeno, {m.get('live', 0)}× živě ({rate}), {m.get('bets', 0)} sázky, "
             f"{m.get('commands', 0)}× nabitý příkaz, {m.get('human_actions', 0)} akcí hráčů, {m.get('bridge_calls', 0)} odpovědí postav",
             f"🗓️ Porady: {por.get('started', 0)}× proběhla, {por.get('dry_run', 0)}× nanečisto" + (" · _režim nanečisto_" if snap.get("dry_run") else "")]
    if p:
        mine = (snap.get("player") or {}).get("recent_acts") or []
        txt = "; ".join(f"{PERSONAS.get(a['persona'], a['persona'])}: {act_text(a['act'], a['ts'], titles)}" for a in mine[:3])
        lines.append(f"🧾 Tvoje skutky, které si postavy pamatují: {txt or 'zatím žádné'}")
        if korun is not None: lines.append(f"🪙 Tvůj zůstatek: {num(korun)} OKÚ korun")
    lines.append("_Svět zatím jen pozoruje: důsledky přijdou v další fázi._")
    return "\n".join(lines)

def fetch_world(player=None, timeout=2.0, url=None):
    """GET <world>/api/world from the loopback world service. No proxies (env HTTP(S)_PROXY ignored). None on failure."""
    base = (url or os.environ.get("OKU_WORLD_URL") or WORLD_URL).rstrip("/")
    q = f"?player={urllib.parse.quote(player)}" if player else ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/api/world" + q, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def svet_remote(eng, p=None, fetch=None):
    """`/kolo svet` reply: world snapshot from oku_world + the player's korun from the wheel; fallback line if down."""
    snap = (fetch or fetch_world)(p)
    if not isinstance(snap, dict): return DOWN
    names = {k: v.get("name", k) for k, v in eng.cfg["players"].items()}
    titles = {e["key"]: e["title"] for e in eng.cfg["events"]}
    try: korun = eng.eco.balance(p) if p else None
    except Exception: korun = None
    try: return svet_text(snap, names, titles, p, eng.clock(), korun)
    except Exception: return DOWN

