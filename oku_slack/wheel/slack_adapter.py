"""Slack adapter for the wheel: /kolo slash command + code-entry modal + channel notifications.
Uses a DEDICATED app (env SLACK_OKU_WHEEL_BOT_TOKEN / SLACK_OKU_WHEEL_APP_TOKEN, manifests/kolo.yaml) so a second
Socket Mode connection never steals events from the live persona bridge. Tokens are read from env, never logged."""
import logging, os, time
from . import engine, render

log = logging.getLogger("oku_wheel.slack")
MODAL_ID = "kolo_confirm"
HOSTS = {"monika": "Monika Babišová", "babis": "Andrej Babiš", "alenka": "Alenka", "bourak": "Bourák", "marty": "Marty", "peta": "Peťa", "kalousek": "Kalousek"}
HELP = ("*/kolo* roztočí kolo (*/kolo toc <klíč>* vynutí událost, pokud allow_force) · */kolo potvrdit* (modál s kódem) · */kolo prikaz* nabitý příkaz (1× za 30 min) · "
        "*/kolo stav* · animace a chat v roomce: {url}")

def room_url(): return os.environ.get("OKU_WHEEL_PUBLIC_URL", "http://127.0.0.1:8787/")

def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))

def handle_command(eng, user_id, text):
    """Returns {"text": ephemeral reply, "open_modal": bool, "spin": event|None, "seq": sequence|None}."""
    p = eng.player_by_slack(user_id)
    if p is None: return {"text": "Nejsi v seznamu hráčů (players.toml).", "open_modal": False}
    arg = (text or "").strip().lower()
    try:
        parts = arg.split()
        if not parts or parts[0] in ("toc", "toč", "spin"):
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

def start(eng):
    """Connect the dedicated Kolo app over Socket Mode. Returns notify(kind, obj) for the room server."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    bot, apptok = os.environ.get("SLACK_OKU_WHEEL_BOT_TOKEN"), os.environ.get("SLACK_OKU_WHEEL_APP_TOKEN")
    if not (bot and apptok): log.warning("Slack adapter disabled: SLACK_OKU_WHEEL_* not set"); return None
    app = App(token=bot); channel = eng.cfg["settings"].get("slack_channel")

    def post(kind, obj):
        text = notification(eng, kind, obj)
        if not (text and channel): return
        if kind == "reveal":
            img = render.png(eng.snapshot()["wheel"], obj["target_angle"], title=obj["title"])
            app.client.files_upload_v2(channel=channel, content=img, filename="kolo.png", title=obj["title"], initial_comment=text)
        elif kind == "live" and obj.get("legendary"):
            img = render.titanic_poster(eng.cfg["scripts"].get(obj.get("script")))
            app.client.files_upload_v2(channel=channel, content=img, filename="titanic.png", title=obj["title"], initial_comment=text)
        else: app.client.chat_postMessage(channel=channel, text=text)

    @app.command("/kolo")
    def _kolo(ack, body, client):
        r = handle_command(eng, body["user_id"], body.get("text"))
        if r["open_modal"]:
            ack(); client.views_open(trigger_id=body["trigger_id"], view=modal_view(eng.active_event())); return
        ack(text=r["text"])
        for k in ("spin", "seq"):
            if r.get(k): post("spin" if k == "spin" else "seq_start", r[k])

    @app.view(MODAL_ID)
    def _modal(ack, body, view):
        code = view["state"]["values"]["code"]["v"]["value"]
        errs = handle_modal(eng, body["user"]["id"], view["private_metadata"], code)
        ack(response_action="errors", errors=errs) if errs else ack()

    SocketModeHandler(app, apptok).connect()
    log.info("Slack adapter connected")
    return post
