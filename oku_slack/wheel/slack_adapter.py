"""Slack adapter for the wheel: /kolo slash command + code-entry modal + channel notifications.
Uses a DEDICATED app (env SLACK_OKU_WHEEL_BOT_TOKEN / SLACK_OKU_WHEEL_APP_TOKEN, manifests/kolo.yaml) so a second
Socket Mode connection never steals events from the live persona bridge. Tokens are read from env, never logged."""
import logging, os, time
from . import canvas, engine, panel, render

log = logging.getLogger("oku_wheel.slack")
MODAL_ID = "kolo_confirm"
HOSTS = {"monika": "Monika Babišová", "babis": "Andrej Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa", "kalousek": "Kalousek"}
HELP = ("*/kolo* pošle ovládací panel · */kolo toc* roztočí kolo (*/kolo toc <klíč>* vynutí událost, pokud allow_force) · */kolo potvrdit* (modál s kódem) · */kolo prikaz* nabitý příkaz (1× za 30 min) · "
        "*/kolo stav* · animace a chat v roomce: {url}")

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

def handle_command(eng, user_id, text):
    """Returns {"text": ephemeral reply, "open_modal": bool, "spin": event|None, "seq": sequence|None}."""
    p = eng.player_by_slack(user_id)
    if p is None: return {"text": "Nejsi v seznamu hráčů (players.toml).", "open_modal": False}
    arg = (text or "").strip().lower()
    try:
        parts = arg.split()
        if not parts or parts[0] == "panel":
            return {"text": "Ovládací panel kola posílám do kanálu.", "panel": True, "open_modal": False}
        if parts[0] in ("toc", "toč", "spin"):
            e = eng.spin(p, force=parts[1] if len(parts) > 1 else None)
            return {"text": f"Kolo se točí → *{e['title']}* v {_hm(e['start_at'])}. Tvůj kód: `{e['code']}` (/kolo potvrdit).",
                    "spin": e, "open_modal": False}
        if arg in ("potvrdit", "confirm", "ano"):
            if not eng.active_event(): return {"text": "Není co potvrzovat.", "open_modal": False}
            return {"text": "", "open_modal": True}
        if arg in ("prikaz", "příkaz", "command"):
            q = eng.use_command(p)
            return {"text": f"⚡ *{q['label']}* spuštěno na 2 minuty.", "seq": q, "open_modal": False}
        if arg in ("stav", "status"):
            e = eng.active_event(); cd = eng.cooldown_left(p)
            ev = f"*{e['title']}* ({e['state']}) v {_hm(e['start_at'])}, potvrdili: {', '.join(e['confirmed']) or 'nikdo'}" if e else "žádná událost"
            return {"text": f"{ev} · tvůj příkaz: {'připraven' if cd <= 0 else f'za {int(cd // 60)} min'}", "open_modal": False}
        return {"text": HELP.format(url=room_url()), "open_modal": False}
    except engine.WheelError as err:
        return {"text": f"✋ {err}", "open_modal": False}

def modal_view(e):
    return {"type": "modal", "callback_id": MODAL_ID, "private_metadata": e["id"],
            "title": {"type": "plain_text", "text": "Potvrdit účast"}, "submit": {"type": "plain_text", "text": "Potvrdit"},
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{e['title']}* v {_hm(e['start_at'])}\nOpiš kód události (ze zprávy /kolo nebo z roomky)."}},
                       {"type": "input", "block_id": "code", "label": {"type": "plain_text", "text": "Kód"},
                        "element": {"type": "plain_text_input", "action_id": "v", "max_length": 4}}]}

def handle_modal(eng, user_id, event_id, code):
    """Returns None on success, or {block_id: error} for Slack response_action=errors."""
    p = eng.player_by_slack(user_id)
    if p is None: return {"code": "Nejsi hráč."}
    try: eng.confirm_code(event_id, p, code); return None
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
        return f"{_host(obj)}🎡 {tag}Kolo se točí, výsledek v roomce: {room_url()} · potvrzení `/kolo potvrdit` nebo podržením"
    if kind == "reveal": return f"{_host(obj)}🎯 *{obj['title']}* ({host}) v {_hm(obj['start_at'])}."
    if kind == "nag": return f"{_host(obj)}{mention()}"
    if kind == "alarm": return f"{_host(obj)}⏰ {mention()} za 5 minut *{obj['title']}*! Potvrzeno: {', '.join(obj['confirmed']) or 'nikdo'}."
    if kind == "live" and obj.get("legendary"): return f"🔴 PŘÍMÝ PŘENOS: *{obj['title']}*. Titanic scéna právě začíná: {room_url()}"
    if kind == "live": return f"🔴 *{obj['title']}* začíná. Roomka: {room_url()}"
    if kind == "expired": return f"{_host(obj)}💤 *{obj['title']}* propadla, nepotvrdili všichni."
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

def make_poster(eng, client, channel, sync=None, pnl=None):
    """notify(kind, obj): channel messages + spin GIF in thread + result PNG embedded in the live canvas.
    Every Slack error is logged by code (missing_scope, not_in_channel, ...) and swallowed."""
    def post(kind, obj):
        if sync: sync.request()
        if pnl: pnl.request()
        text = notification(eng, kind, obj)
        if not (text and channel): return
        segs = eng.snapshot()["wheel"]
        try:
            if kind == "spin":
                r = client.chat_postMessage(channel=channel, text=text)
                ts = r.get("ts") if hasattr(r, "get") else None
                gif = render.spin_gif(segs, obj["target_angle"], turns=obj.get("turns", 5), title=obj["title"])
                client.files_upload_v2(channel=channel, thread_ts=ts, content=gif, filename="kolo-spin.gif", title="Kolo se točí")
            elif kind == "reveal":
                img = render.png(segs, obj["target_angle"], title=obj["title"])
                r = client.files_upload_v2(channel=channel, content=img, filename="kolo.png", title=obj["title"], initial_comment=text)
                if sync: sync.set_image(_permalink(r))
            elif kind == "live" and obj.get("legendary"):
                img = render.titanic_poster(eng.cfg["scripts"].get(obj.get("script")))
                client.files_upload_v2(channel=channel, content=img, filename="titanic.png", title=obj["title"], initial_comment=text)
            else: client.chat_postMessage(channel=channel, text=text)
        except Exception as e:
            log.warning("slack post %s failed: %s", kind, _code(e))
    return post

def handle_action(eng, client, body, post=None, pnl=None):
    """Button press on the panel. Replies ephemerally; confirmation opens the code modal."""
    act = (body.get("actions") or [{}])[0].get("action_id")
    user = (body.get("user") or {}).get("id"); ch = (body.get("channel") or {}).get("id") or (pnl.channel if pnl else None)
    arg = panel.ACTIONS.get(act)
    if not arg: return None
    r = handle_command(eng, user, arg)
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
    if pnl: pnl.request()
    return r

def start(eng):
    """Connect the dedicated Kolo app over Socket Mode. Returns notify(kind, obj) for the room server."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    bot, apptok = env_token("SLACK_OKU_WHEEL_BOT_TOKEN"), env_token("SLACK_OKU_WHEEL_APP_TOKEN")
    if not (bot and apptok): log.warning("Slack adapter disabled: SLACK_OKU_WHEEL_* not set"); return None
    app = App(token=bot); channel = eng.cfg["settings"].get("slack_channel")

    sync = canvas.CanvasSync(eng, app.client, channel, eng.store); sync.run()
    pnl = panel.Panel(eng, app.client, channel, eng.store); pnl.run()
    post = make_poster(eng, app.client, channel, sync, pnl)

    import re as _re
    @app.action(_re.compile(r"^kolo_(spin|confirm|command|status)$"))
    def _act(ack, body, client):
        ack(); handle_action(eng, client, body, post, pnl)

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
        for k in ("spin", "seq"):
            if r.get(k): post("spin" if k == "spin" else "seq_start", r[k])

    @app.view(MODAL_ID)
    def _modal(ack, body, view):
        code = view["state"]["values"]["code"]["v"]["value"]
        errs = handle_modal(eng, body["user"]["id"], view["private_metadata"], code)
        ack(response_action="errors", errors=errs) if errs else ack()
        if not errs: post("confirm", {}); pnl.request()

    SocketModeHandler(app, apptok).connect()
    log.info("Slack adapter connected")
    return post
