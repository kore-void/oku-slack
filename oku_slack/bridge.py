"""Slack Socket Mode bridge for the OKÚ satire team. Solo bots: one Slack app per persona, all in one process.
Each app answers its own @mentions/DMs only as its own persona. Secrets only from env (SLACK_OKU_<KEY>_BOT_TOKEN /
SLACK_OKU_<KEY>_APP_TOKEN, Babiš falls back to SLACK_OKU_BOT_TOKEN / SLACK_OKU_APP_TOKEN; Gemini keys via env or
OKU_GEMINI_ENV_FILE); never logged."""
import sys, logging, threading
from . import core

FALLBACK = "Technika selhala. To je kampaň!"

def wants(event, bot_user):
    """Loop guard: never react to bots (incl. our own other personas), edits/joins/etc. or our own messages."""
    return not (event.get("bot_id") or event.get("subtype") or event.get("user") == bot_user)

class Bridge:
    def __init__(self, client, cfg, key, bot_user, gen=core.generate, peers=None):
        self.client, self.cfg, self.key, self.bot, self.gen = client, cfg, key, bot_user, gen
        self.prompt = core.build_prompt(cfg["personas"][key])
        self.peers = {} if peers is None else peers  # bot user id -> persona name, shared by all apps in this process

    def clean(self, text):
        text = (text or "").replace(f"<@{self.bot}>", "")
        for uid, name in self.peers.items():
            if uid != self.bot: text = text.replace(f"<@{uid}>", f"@{name}")
        return text.strip()

    def history(self, event):
        ch = event["channel"]; msgs = [event]
        if event.get("thread_ts"):
            try: msgs = self.client.conversations_replies(channel=ch, ts=event["thread_ts"], limit=self.cfg.get("history_limit", 20))["messages"]
            except Exception: pass
        h = []
        for m in msgs:
            txt = self.clean(m.get("text"))
            if m.get("user") == self.bot: h.append({"role": "assistant", "content": txt}); continue
            who = (self.peers.get(m.get("user")) or (m.get("bot_profile") or {}).get("name")
                   or m.get("username") or m.get("bot_id")) if m.get("bot_id") else None
            h.append({"role": "user", "content": (f"[{who}] " if who else "") + txt})
        return h

    def post(self, ch, ts, text):
        self.client.chat_postMessage(channel=ch, thread_ts=ts, text=text)

    def handle(self, event):
        ch, ts = event["channel"], event.get("thread_ts") or event["ts"]
        core.log.info("event persona=%s ch=%s ts=%s", self.key, ch, event["ts"])
        hist = self.history(event)
        try: reply = self.gen(self.prompt, hist)
        except Exception as e:
            core.log.error("persona=%s llm error: %s", self.key, type(e).__name__); reply = FALLBACK
        self.post(ch, ts, reply)

def register(app, b, spawn=None):
    spawn = spawn or (lambda ev: threading.Thread(target=b.handle, args=(ev,), daemon=True).start())
    @app.event("app_mention")
    def _m(event):
        if wants(event, b.bot): spawn(event)
    @app.event("message")
    def _dm(event):
        if event.get("channel_type") == "im" and wants(event, b.bot): spawn(event)

def _why(e):
    """Exception type + Slack error code (e.g. invalid_auth), never the message (could echo request data)."""
    err = None
    for x in (e, e.__cause__, e.__context__):
        try: err = err or x.response.get("error")
        except Exception: pass
    return f"{type(e).__name__}({err})" if err else type(e).__name__

def start(cfg, get=core.env, make_app=None, make_handler=None):
    """Connect one Slack app per persona that has both tokens. Returns [(key, handler)] of connected personas."""
    if make_app is None: from slack_bolt import App as make_app
    if make_handler is None: from slack_bolt.adapter.socket_mode import SocketModeHandler as make_handler
    peers, seen, up = {}, {}, []
    for key, p in cfg["personas"].items():
        bot, app_tok, names = core.tokens(key, get)
        missing = [n for n, v in zip(names, (bot, app_tok)) if not v]
        if missing:
            core.log.warning("persona=%s skipped: missing %s", key, "/".join(missing)); continue
        try:
            app = make_app(token=bot); uid = app.client.auth_test()["user_id"]
            if uid in seen:  # same xoxb pasted twice -> two personas would answer every mention
                core.log.error("persona=%s skipped: %s is the same Slack app as persona=%s", key, names[0], seen[uid]); continue
            register(app, Bridge(app.client, cfg, key, uid, peers=peers))
            h = make_handler(app, app_tok); h.connect()
            seen[uid] = key; peers[uid] = p["name"]; up.append((key, h))
            core.log.info("persona=%s connected user=%s", key, uid)
        except Exception as e:
            core.log.error("persona=%s failed to start: %s", key, _why(e))
    return up

def main():
    logs = core.ROOT / "logs"; logs.mkdir(exist_ok=True)
    logging.basicConfig(filename=logs / "oku_slack.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    up = start(core.load_config())
    if not up:
        core.log.error("no persona connected (no Slack tokens set?), exiting"); sys.exit(1)
    core.log.info("running personas=%s", ",".join(k for k, _ in up))
    threading.Event().wait()

if __name__ == "__main__": main()
