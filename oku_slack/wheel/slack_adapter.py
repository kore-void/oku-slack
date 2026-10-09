"""Slack adapter for the wheel: /kolo slash command + code-entry modal + channel notifications.
Uses a DEDICATED app (env SLACK_OKU_WHEEL_BOT_TOKEN / SLACK_OKU_WHEEL_APP_TOKEN, manifests/kolo.yaml) so a second
Socket Mode connection never steals events from the live persona bridge. Tokens are read from env, never logged."""
import logging, os, time
from . import canvas, engine, panel, render, personas, scenes as _scenes
from ..world import log as world_log, view as world_view

log = logging.getLogger("oku_wheel.slack")
MODAL_ID = "kolo_confirm"
HOSTS = {"monika": "Monika Babišová", "babis": "Andrej Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa", "kalousek": "Kalousek"}
HELP = ("*/kolo* pošle ovládací panel · */kolo toc* otevře sázky a roztočí kolo (*/kolo toc <klíč>* vynutí událost bez sázek, pokud allow_force) · "
        "*/kolo sazka <klíč> <částka|all>* · */kolo potvrdit* (modál s kódem) · */kolo prikaz* nabitý příkaz (1× za 30 min) · "
        "*/kolo zebricek* · */kolo stav* · */kolo svet* stav světa OKÚ")
PICKS = {}  # slack user -> last segment chosen in the panel select (fallback when the payload has no state)

def env_token(name):
    """Token from process env, else from the user registry (HKCU\\Environment) so a supervisor started
    before setx still works without restarting it. Value is never logged."""
    v = os.environ.get(name)
    if v or os.name != "nt": return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: return winreg.QueryValueEx(k, name)[0] or None
    except OSError: return None

def room_url(): return os.environ.get("OKU_WHEEL_PUBLIC_URL", "http://127.0.0.1:8797/")

def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))

def _pts(n): return panel._pts(n)

def handle_command(eng, user_id, text, pick=None, via="slash"):
    """Returns {"text": ephemeral reply ('' = silent), "open_modal": bool, "spin": event|None, "seq": sequence|None,
    "round": betting round|None, "changed": bool (panel should re-render)}. Diary source = 'slack'."""
    with world_log.source("slack"):
        return _handle_command(eng, user_id, text, pick, via)

def _handle_command(eng, user_id, text, pick=None, via="slash"):
    p = eng.player_by_slack(user_id)
    if p is None: return {"text": "Nejsi v seznamu hráčů (players.toml).", "open_modal": False}
    arg = (text or "").strip().lower()
    sub = (arg.split() or ["panel"])[0]
    if sub != "pick": eng.record("ui.kolo", p, None, {"sub": sub[:16], "via": via})  # subcommand name only, never args/text
    titles = {x["key"]: x["title"] for x in eng.cfg["events"]}
    try:
        parts = arg.split()
        if not parts or parts[0] == "panel":
            return {"text": "Ovládací panel kola posílám do kanálu.", "panel": True, "open_modal": False}
        if parts[0] in ("svet", "svět", "world"):
            return {"text": world_view.svet_text(eng, p), "open_modal": False}
        if parts[0] in ("zebricek", "žebříček", "body", "leaderboard"):
            rows = "\n".join(f"{i + 1}. {r['name']} · *{_pts(r['points'])}*" for i, r in enumerate(eng.eco.board()))
            return {"text": f"🏆 *Žebříček OKÚ korun*\n{rows}\n🪙 Ty máš {_pts(eng.eco.balance(p))}.", "open_modal": False}
        if parts[0] == "pick":
            if len(parts) > 1: PICKS[user_id] = parts[1]
            return {"text": "", "open_modal": False}
        if parts[0] in ("bet", "sazka", "sázka"):
            if parts[0] == "bet": key, amount = (pick or PICKS.get(user_id)), (parts[1] if len(parts) > 1 else "")
            else: key, amount = (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else "")
            if not key: return {"text": "Nejdřív vyber v panelu políčko, na které sázíš.", "open_modal": False}
            amount = "all" if amount in ("all", "allin", "vse", "vše") else (int(amount) if amount.isdigit() else 0)
            b = eng.bet(p, key, amount)
            return {"text": f"💰 Vsadil(a) jsi {_pts(b['amount'])} na *{titles.get(key, key)}* (×{b['odds']}). Zůstatek {_pts(eng.eco.balance(p))}.",
                    "open_modal": False, "changed": True}
        if parts[0] == "poll":
            poll = eng.vote(p, int(parts[1]))
            return {"text": f"📊 Hlas pro *{poll['options'][int(parts[1])]}* zapsán.", "open_modal": False, "changed": True}
        if parts[0] == "catch":
            n = eng.catch(p)
            return {"text": f"💸 Dotace je tvoje! +{_pts(n)}", "open_modal": False, "changed": True}
        if parts[0] == "quiz":
            ok = eng.quiz(p, int(parts[1]))
            return {"text": f"🧠 Správně! +{_pts(eng.s['points_quiz'])}" if ok else "🧠 Vedle. Příště!", "open_modal": False, "changed": True}
        if parts[0] in ("toc", "toč", "spin") and len(parts) == 1 and float(eng.s.get("bet_window_s", 0)) > 0:
            r = eng.open_bets(p)
            return {"text": f"💰 Sázky jsou otevřené na {int(eng.s['bet_window_s'])} s! V panelu vyber políčko a částku, pak se kolo roztočí samo.",
                    "round": r, "open_modal": False, "changed": True}
        if parts[0] in ("toc", "toč", "spin"):
            e = eng.spin(p, force=parts[1] if len(parts) > 1 else None)
            return {"text": f"Kolo se točí → *{e['title']}* v {_hm(e['start_at'])}. Tvůj kód: `{e['code']}` (/kolo potvrdit).",
                    "spin": e, "open_modal": False}
        if arg in ("potvrdit", "confirm", "ano"):
            if not eng.active_event(): return {"text": "Není co potvrzovat.", "open_modal": False}
            return {"text": "", "open_modal": True}
        if arg in ("prikaz", "příkaz", "command"):
            q = eng.use_command(p)
            fx = " ".join(r["text"] for r in q.get("effects", []) if r.get("ok"))
            return {"text": f"⚡ *{q['label']}* spuštěno na 2 minuty. {fx}".strip(), "seq": q, "open_modal": False}
        if arg in ("stav", "status"):
            e = eng.active_event(); cd = eng.cooldown_left(p)
            return {"text": status_text(eng, p, e, cd), "open_modal": False}
        return {"text": HELP.format(url=room_url()), "open_modal": False}
    except engine.WheelError as err:
        return {"text": f"✋ {err}", "open_modal": False}

STATE_HUMAN = {"pending": "⏳ čeká na potvrzení", "ready": "✅ připraveno", "live": "🔴 běží",
               "done": "🏁 skončeno", "expired": "💤 propadlo", "vetoed": "🙅 vetováno"}

def status_text(eng, p, e, cd):
    name = lambda k: eng.cfg["players"].get(k, {}).get("name", k)
    cmd = "⚡ Tvůj nabitý příkaz: " + ("připraven" if cd <= 0 else f"nabije se za {max(1, int(cd // 60))} min")
    cmd += f"\n🪙 Tvůj zůstatek: {_pts(eng.eco.balance(p))}"
    r = eng.round()
    if r and not e:
        return f"🎡 *Stav kola:* 💰 běží sázky do {time.strftime('%H:%M:%S', time.localtime(r['closes_at']))}\n" + cmd
    if not e:
        return "🎡 *Stav kola*\nKolo je volné – můžeš točit.\n" + cmd
    lines = [f"🎡 *Stav kola:* {STATE_HUMAN.get(e['state'], e['state'])}", f"*{e['title']}* · start v {_hm(e['start_at'])}"]
    if e["state"] == "pending":
        ok = ", ".join(name(x) for x in e["confirmed"]) or "zatím nikdo"
        need = eng.quorum() - eng.confirmed_count(e)
        lines.append(f"Potvrdili: {ok} · čeká se na: {', '.join(name(x) for x in eng.missing(e))}"
                     + (f" (stačí {need})" if need < len(eng.missing(e)) else ""))
    lines.append(eng.busy_reason(e) if e["state"] in ("pending", "ready", "live") else "Kolo je volné.")
    lines.append(cmd)
    return "\n".join(lines)

def modal_view(e):
    return {"type": "modal", "callback_id": MODAL_ID, "private_metadata": e["id"],
            "title": {"type": "plain_text", "text": "Potvrdit účast"}, "submit": {"type": "plain_text", "text": "Potvrdit"},
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{e['title']}* v {_hm(e['start_at'])}\nPotvrzení je úmyslné: opiš kód *{e['code']}*."}},
                       {"type": "input", "block_id": "code", "label": {"type": "plain_text", "text": "Kód"},
                        "element": {"type": "plain_text_input", "action_id": "v", "max_length": 4}}]}

def handle_modal(eng, user_id, event_id, code):
    """Returns None on success, or {block_id: error} for Slack response_action=errors."""
    p = eng.player_by_slack(user_id)
    if p is None: return {"code": "Nejsi hráč."}
    try:
        with world_log.source("slack"): eng.confirm_code(event_id, p, code)
        return None
    except engine.WheelError as err: return {"code": str(err)}

def _host(obj):
    h = obj.get("host_say") or {}
    return f"🎠 *{h.get('name', 'Monika Babišová')}:* {h['text']}\n" if h.get("text") else ""

def notification(eng, kind, obj):
    """Message text for a tick/room notification, or None if Slack stays quiet."""
    mention = lambda: " ".join(f"<@{v['slack_id']}>" for v in eng.cfg["players"].values())
    host = HOSTS.get(obj.get("host", ""), "")
    if kind == "spin":
        tag = "🌟 LEGENDÁRNÍ " if obj.get("legendary") else ""
        return f"{_host(obj)}🎡 {tag}Kolo se točí! Potvrzení: tlačítko ✅ v panelu nebo `/kolo potvrdit`"
    if kind == "reveal": return f"{_host(obj)}🎯 *{obj['title']}* ({host}) v {_hm(obj['start_at'])}."
    if kind == "nag": return f"{_host(obj)}{mention()}"
    if kind == "alarm": return f"{_host(obj)}⏰ {mention()} za 5 minut *{obj['title']}*! Potvrzeno: {', '.join(obj['confirmed']) or 'nikdo'}."
    if kind == "live" and obj.get("legendary"): return f"🔴 PŘÍMÝ PŘENOS: *{obj['title']}*. Titanic scéna právě začíná!"
    if kind == "live": return f"🔴 *{obj['title']}* začíná!"
    if kind == "expired": return f"{_host(obj)}💤 *{obj['title']}* propadla, nepotvrdil dost hráčů."
    if kind == "done": return f"✅ *{obj['title']}* skončila."
    if kind == "seq_start": return f"⚡ {eng.cfg['players'][obj['player']]['name']}: *{obj['label']}* (2 min)."
    return None

def _code(e):
    r = getattr(e, "response", None)
    try: return r["error"]
    except Exception: return type(e).__name__

def _permalink(resp):
    f = (resp.get("file") if hasattr(resp, "get") else None) or ((resp.get("files") or [{}])[0] if hasattr(resp, "get") else {})
    return (f or {}).get("permalink") or (f or {}).get("url_private")

def make_poster(eng, client, channel, sync=None, pnl=None, scenes=None):
    """notify(kind, obj): re-render panel/canvas; the only wheel-bot channel message is the 5-min alarm.
    live -> persona scene starts (scenes.SceneRunner); persona_line -> one persona line (charged-command effect).
    Every Slack error is logged by code (missing_scope, not_in_channel, ...) and swallowed."""
    def post(kind, obj):
        if sync: sync.request()
        if pnl: pnl.request()
        if scenes is not None:
            try:
                if kind == "live": scenes.start(obj)
                elif kind == "persona_line": scenes.interject(obj)
                elif kind in ("done", "vetoed", "expired") and hasattr(scenes, "end"): scenes.end(obj)
            except Exception as e: log.warning("scene %s failed: %s", kind, type(e).__name__)
        if kind != "alarm" or not channel: return
        mentions = " ".join(panel.who(eng, k) for k in eng.cfg["players"])
        try: client.chat_postMessage(channel=channel, text=f"⏰ {mentions} Za 5 minut začíná *{obj['title']}*! 🎡 Panel kola je výš ⬆️")
        except Exception as e: log.warning("slack alarm failed: %s", _code(e))
    return post

def card_png(eng, e):
    """Per-event result card: title, host persona (repo avatar asset if present) and start time."""
    from . import config as _cfg
    av = _cfg.ROOT / "assets" / "avatars" / f"{e.get('host', '')}.png"
    color = next((x.get("color", "#1d4f91") for x in eng.cfg["events"] if x["key"] == e["key"]), "#1d4f91")
    return render.result_card(e["title"], panel.HOST_NAMES.get(e.get("host", ""), ""), f"Start {time.strftime('%H:%M', time.localtime(e['start_at']))}",
                              color, av if av.exists() else None)

def handle_action(eng, client, body, post=None, pnl=None):
    """Button press on the panel. Replies ephemerally; confirmation opens the code modal."""
    action = (body.get("actions") or [{}])[0]; act = action.get("action_id")
    user = (body.get("user") or {}).get("id"); ch = (body.get("channel") or {}).get("id") or (pnl.channel if pnl else None)
    arg = panel.action_arg(action)
    if not arg: return None
    pick = None
    try: pick = body["state"]["values"]["kolo_bets"]["kolo_bet_pick"]["selected_option"]["value"]
    except (KeyError, TypeError): pass
    r = handle_command(eng, user, arg, pick=pick, via="button")
    try:
        if r.get("open_modal"):
            client.views_open(trigger_id=body["trigger_id"], view=modal_view(eng.active_event()))
        elif r.get("text") and ch:
            client.chat_postEphemeral(channel=ch, user=user, text=r["text"])
    except Exception as e:
        log.warning("action %s reply failed: %s", act, _code(e))
    if post:
        if r.get("spin"): post("spin", r["spin"])
        if r.get("seq"): post("seq_start", r["seq"])
        if r.get("round"): post("bets_open", r["round"])
    if pnl: pnl.request()
    return r

def on_reaction(eng, pnl, scenes, channel, event, delta):
    """Hype meter: reactions on the panel or on the live scene's messages (needs reactions:read + events)."""
    item = event.get("item") or {}
    if item.get("type") != "message" or item.get("channel") != channel: return False
    ts = item.get("ts"); panel_ts = eng.store.kv_get("panel_ts")
    tracked = {panel_ts} | (scenes.tracked_ts() if scenes else set())
    if ts not in tracked: return False
    with world_log.source("slack"):  # diary row for every tracked reaction; hype only while live
        ok = eng.reaction(eng.player_by_slack(event.get("user")), event.get("reaction"), delta, "panel" if ts == panel_ts else "scene")
    if ok and pnl: pnl.request()
    return ok

def granted_scopes(client):
    """Bot scopes from the x-oauth-scopes header of auth.test (names only)."""
    try:
        r = client.auth_test(); h = getattr(r, "headers", {}) or {}
        raw = h.get("x-oauth-scopes") or h.get("X-OAuth-Scopes") or ""
        return {x.strip() for x in raw.split(",") if x.strip()}
    except Exception as e:
        log.warning("auth.test failed: %s", _code(e)); return set()

def refresh_scopes(client, known, scn=None):
    """Re-read granted scopes (after the app is reinstalled with new ones) and apply them live:
    chat:write.customize -> wheel-bot name override. Reaction events need no restart: the handlers are always
    registered and Slack starts delivering them over the existing Socket Mode connection once subscribed."""
    now = granted_scopes(client)
    if not now: return known
    added, removed = now - known, known - now
    if added or removed: log.info("scopes changed: +%s -%s", ",".join(sorted(added)) or "-", ",".join(sorted(removed)) or "-")
    if scn is not None and getattr(scn, "poster", None) is not None: scn.poster.customize = "chat:write.customize" in now
    return now

def watch_scopes(client, scopes, scn, every=300.0):
    import threading
    def loop():
        known = set(scopes)
        while True:
            time.sleep(every)
            try: known = refresh_scopes(client, known, scn)
            except Exception as e: log.warning("scope refresh failed: %s", type(e).__name__)
    threading.Thread(target=loop, daemon=True, name="kolo-scopes").start()

def make_scenes(eng, wheel_client, channel, scopes):
    """Persona poster (solo persona bot tokens, Web API only) + scene runner with the bridge's LLM path."""
    from .. import core
    try: ccfg = core.load_config()
    except Exception as e: log.warning("oku_slack config unreadable: %s", type(e).__name__); ccfg = {"personas": {}}
    cache = {}
    def prompt_fn(k):
        if k not in ccfg.get("personas", {}): return None
        if k not in cache: cache[k] = core.build_prompt(ccfg["personas"][k])
        return cache[k]
    poster = personas.PersonaPoster(wheel_client, channel, token_fn=env_token, customize="chat:write.customize" in scopes,
                                    icon_base=ccfg.get("icon_base_url", ""))
    log.info("persona bots available: %s; wheel name override: %s", ",".join(poster.available()) or "none", poster.customize)
    return _scenes.SceneRunner(eng, poster, gen=core.generate, prompt_fn=prompt_fn)

def start(eng):
    """Connect the dedicated Kolo app over Socket Mode. Returns notify(kind, obj) for the room server."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    bot, apptok = env_token("SLACK_OKU_WHEEL_BOT_TOKEN"), env_token("SLACK_OKU_WHEEL_APP_TOKEN")
    if not (bot and apptok): log.warning("Slack adapter disabled: SLACK_OKU_WHEEL_* not set"); return None
    app = App(token=bot); channel = eng.cfg["settings"].get("slack_channel")

    scopes = granted_scopes(app.client)
    for need in ("reactions:read", "chat:write.customize"):
        if need not in scopes: log.warning("scope %s not granted yet: %s degraded", need, "hype meter" if need.startswith("reactions") else "Macinka/Monika name override")
    sync = canvas.CanvasSync(eng, app.client, channel, eng.store); sync.run()
    pnl = panel.Panel(eng, app.client, channel, eng.store, card_renderer=lambda e: card_png(eng, e)); pnl.run()
    try: scn = make_scenes(eng, app.client, channel, scopes)
    except Exception as e: log.error("scenes disabled: %s", type(e).__name__); scn = None
    post = make_poster(eng, app.client, channel, sync, pnl, scn)
    watch_scopes(app.client, scopes, scn)
    a = eng.active_event()
    if scn is not None and a and a["state"] == "live": scn.start(a)  # resume after restart

    import re as _re
    @app.action(_re.compile(r"^kolo_"))
    def _act(ack, body, client):
        ack(); handle_action(eng, client, body, post, pnl)

    @app.event("reaction_added")
    def _ra(event): on_reaction(eng, pnl, scn, channel, event, +1)

    @app.event("reaction_removed")
    def _rr(event): on_reaction(eng, pnl, scn, channel, event, -1)

    @app.command("/kolo")
    def _kolo(ack, body, client):
        r = handle_command(eng, body["user_id"], body.get("text"))
        if r["open_modal"]:
            ack(); client.views_open(trigger_id=body["trigger_id"], view=modal_view(eng.active_event())); return
        ack(text=r["text"])
        if r.get("panel"):
            if not pnl.post_new(): client.chat_postEphemeral(channel=body["channel_id"], user=body["user_id"],
                                                             text=f"Panel nejde poslat: {pnl.last_error} (je bot v kanálu?)")
            return
        for k, kind in (("spin", "spin"), ("seq", "seq_start"), ("round", "bets_open")):
            if r.get(k): post(kind, r[k])
        if r.get("changed"): pnl.request()

    @app.view(MODAL_ID)
    def _modal(ack, body, view):
        code = view["state"]["values"]["code"]["v"]["value"]
        errs = handle_modal(eng, body["user"]["id"], view["private_metadata"], code)
        ack(response_action="errors", errors=errs) if errs else ack()
        if not errs: post("confirm", {}); pnl.request()

    SocketModeHandler(app, apptok).connect()
    log.info("Slack adapter connected")
    return post
