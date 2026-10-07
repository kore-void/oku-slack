"""Live-event show layer (pure, server-timed): quick poll, hype meter, minigames.
State lives on the event dict under e["show"]; the engine calls plan() at live and tick() every loop.
- poll: one vote per player (points_vote), results shown in the panel.
- catch ("Chyť dotaci"): a button visible for catch_window_s at a random moment; first click wins points_catch.
- quiz: 3 options about OKÚ lore; each player answers once; correct answers win points_quiz.
- hype: reaction count on the panel + scene messages (reaction_added/removed events)."""

def plan(cfg, e, rng):
    s = cfg["settings"]; show_cfg = cfg.get("show", {}); dur = float(e["duration_s"]); t0 = e["live_at"]
    ev = show_cfg.get("events", {}).get(e["key"], {})
    poll = ev.get("poll") or show_cfg.get("poll") or {"q": "Kdo za to může?", "options": ["Kalousek", "Babiš", "Kore", "ICIK"]}
    quizzes = show_cfg.get("quiz") or []
    q = rng.choice(quizzes) if quizzes else None
    catch_at = t0 + dur * rng.uniform(0.25, 0.45)
    quiz_at = t0 + dur * rng.uniform(0.55, 0.7)
    e["show"] = {
        "poll": {"q": poll["q"], "options": list(poll["options"])[:5], "votes": {}},
        "catch": {"at": catch_at, "until": catch_at + float(s["catch_window_s"]), "winner": None, "open": False},
        "quiz": ({"q": q["q"], "options": list(q["options"])[:3], "answer": int(q["answer"]), "at": quiz_at,
                  "until": quiz_at + float(s["quiz_window_s"]), "answers": {}, "open": False} if q else None),
        "hype": 0}
    return e["show"]

def tick(e, now):
    """Open/close minigame windows. Returns True if the panel should re-render."""
    sh = e.get("show")
    if not sh or e["state"] != "live": return False
    ch = False
    for k in ("catch", "quiz"):
        g = sh.get(k)
        if not g: continue
        should = g["at"] <= now < g["until"] and not (k == "catch" and g["winner"])
        if should != g["open"]: g["open"] = should; ch = True
    return ch

class ShowError(Exception):
    def __init__(self, code, msg=""): super().__init__(msg or code); self.code = code

def _live(e):
    if not e or e["state"] != "live" or not e.get("show"): raise ShowError("not_live", "Teď neběží žádná událost.")
    return e["show"]

def vote(e, p, i):
    sh = _live(e); poll = sh["poll"]
    if not 0 <= i < len(poll["options"]): raise ShowError("bad_option", "Neplatná volba.")
    first = p not in poll["votes"]; poll["votes"][p] = i
    return first

def catch(e, p, now):
    g = _live(e)["catch"]
    if g["winner"]: raise ShowError("too_late", "Pozdě! Dotaci už má někdo jiný.")
    if not (g["at"] <= now < g["until"]): raise ShowError("closed", "Dotace teď nelétá.")
    g["winner"] = p; g["open"] = False; g["won_at"] = now
    return True

def quiz_answer(e, p, i, now):
    g = _live(e).get("quiz")
    if not g or not (g["at"] <= now < g["until"]): raise ShowError("closed", "Kvíz teď neběží.")
    if p in g["answers"]: raise ShowError("answered", "Už jsi odpověděl(a).")
    if not 0 <= i < len(g["options"]): raise ShowError("bad_option", "Neplatná volba.")
    g["answers"][p] = i
    return i == g["answer"]

def hype(e, delta):
    sh = e.get("show")
    if not sh: return False
    sh["hype"] = max(0, sh["hype"] + delta); return True

def bar(n, full=10, step=2):
    k = min(full, n // step if step else 0)
    return "▰" * k + "▱" * (full - k)
