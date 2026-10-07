"""Babiš moderation of private channels (config [moderation]): invite/kick on command from the owner only.
Needs bot scope groups:write (conversations.invite / conversations.kick on private channels)."""
import re, unicodedata
from . import core

KICK = ("vyhoď", "vyhod", "vykopni", "vyraz", "kickni", "vyhoďte", "vyhodte")
INVITE = ("pozvi", "přidej", "pridej", "pozvěte", "invite")
ALIASES = ("andreji", "andrej", "babiši", "babisi", "babiš", "babis")
MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

REPLY = {
    "kick": "Vyřízeno, {who} letí. Já jsem to nebyl, to je kampaň.",
    "invite": "{who} je uvnitř. Makáme, ne jako ti politici.",
    "kick_fail": "Nejde to vyhodit, {err}. Sabotáž, jasně!",
    "invite_fail": "Nejde to pozvat, {err}. Zase nějaký úředník!",
    "protected": "To ne. Sebe ani šéfa vyhazovat nebudu, nejsem blázen.",
    "unknown": "Koho? Nevím, kdo to je. Napište @jméno.",
    "deny": "Vy mi nebudete rozkazovat. Tady rozhoduje šéf, ne vy.",
}

def _fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))

def _has(text, words):
    t = _fold(text)
    return next((w for w in words if re.search(r"(?<!\w)" + re.escape(_fold(w)) + r"(?!\w)", t)), None)

def settings(cfg):
    m = cfg.get("moderation") or {}
    if not m.get("enabled", False): return None
    return {"channels": set(m.get("allowed_channels") or []), "owner": m.get("owner_user_id")}

def parse(text, bot_id):
    """Return (action, [user_ids], plain_name) or None. Command must address Babiš (mention or alias)."""
    t = text or ""
    addressed = f"<@{bot_id}>" in t or re.match(r"\s*(" + "|".join(ALIASES) + r")\b", _fold(t)) is not None
    if not addressed: return None
    act = "kick" if _has(t, KICK) else "invite" if _has(t, INVITE) else None
    if not act: return None
    ids = [u for u in MENTION.findall(t) if u != bot_id]
    verb = _has(t, KICK if act == "kick" else INVITE)
    m = re.search(r"(?<!\w)" + re.escape(_fold(verb)) + r"(?!\w)\s*(.+)$", _fold(MENTION.sub(" ", t)))
    name = (m.group(1).strip(" .,!?:;") if m else "") or None
    return act, ids, (None if ids else name)

def resolve(client, name):
    """Plain name -> user id via users.list (users:read). Exact match on name/display/real name, else unique prefix."""
    n = _fold(name)
    try: members = client.users_list(limit=500).get("members", [])
    except Exception as e: core.log.warning("users_list failed: %s", type(e).__name__); return None
    def names(u):
        p = u.get("profile") or {}
        return {_fold(x) for x in (u.get("name"), u.get("real_name"), p.get("display_name"), p.get("real_name")) if x}
    live = [u for u in members if not u.get("deleted") and not u.get("is_bot")]
    exact = [u["id"] for u in live if n in names(u)]
    if len(exact) == 1: return exact[0]
    pref = [u["id"] for u in live if any(x.startswith(n) or x.split(" ")[0] == n for x in names(u))]
    return pref[0] if len(pref) == 1 else None

def _err(e):
    r = getattr(e, "response", None)
    try: return (r.data or {}).get("error") or type(e).__name__
    except Exception: return type(e).__name__

def handle(client, cfg, bot_id, event):
    """Return True if the event was a moderation command in an allowed channel (handled, incl. deny)."""
    s = settings(cfg)
    if not s or event.get("channel") not in s["channels"]: return False
    p = parse(event.get("text"), bot_id)
    if not p: return False
    ch, ts = event["channel"], event.get("thread_ts") or event["ts"]
    say = lambda k, **kw: client.chat_postMessage(channel=ch, thread_ts=ts, text=REPLY[k].format(**kw))
    if not s["owner"] or event.get("user") != s["owner"]:
        core.log.info("moderation denied user=%s ch=%s", event.get("user"), ch)
        say("deny"); return True
    act, ids, name = p
    if not ids and name:
        u = resolve(client, name); ids = [u] if u else []
    if not ids: say("unknown"); return True
    if act == "kick" and any(u in (s["owner"], bot_id) for u in ids): say("protected"); return True
    try:
        if act == "kick":
            for u in ids: client.conversations_kick(channel=ch, user=u)
        else: client.conversations_invite(channel=ch, users=",".join(ids))
    except Exception as e:
        err = _err(e); core.log.warning("moderation %s failed: %s", act, err)
        say(act + "_fail", err=err); return True
    core.log.info("moderation %s ok ch=%s users=%s", act, ch, ",".join(ids))
    say(act, who=", ".join(f"<@{u}>" for u in ids)); return True
