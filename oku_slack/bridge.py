"""Slack Socket Mode bridge for the OKÚ satire team: solo bots, one Slack app per persona.
Tokens per persona from env: SLACK_OKU_<KEY>_BOT_TOKEN / SLACK_OKU_<KEY>_APP_TOKEN
(babis falls back to SLACK_OKU_BOT_TOKEN / SLACK_OKU_APP_TOKEN). Gemini keys via env or
OKU_GEMINI_ENV_FILE. Secrets are never logged; only variable names / present-missing."""
import os, sys, logging, threading
from . import core, meeting

FALLBACK = "Technika selhala. To je kampaň!"

def tokens(key, env=None):
    env = os.environ if env is None else env
    K = key.upper()
    bot, app = env.get(f"SLACK_OKU_{K}_BOT_TOKEN"), env.get(f"SLACK_OKU_{K}_APP_TOKEN")
    if key == "babis":
        bot = bot or env.get("SLACK_OKU_BOT_TOKEN")
        app = app or env.get("SLACK_OKU_APP_TOKEN")
    return bot or None, app or None

def ignored(event, bot_id):
    """Loop guard: never react to bots, edits/subtypes, or our own messages."""
    return bool(event.get("bot_id") or event.get("subtype") or event.get("user") == bot_id)

class Bridge:
    def __init__(self, client, cfg, bot_id, persona="babis", gen=core.generate):
        self.client, self.cfg, self.bot, self.persona, self.gen = client, cfg, bot_id, persona, gen
        self.prompt = core.build_prompt(cfg["personas"][persona])
        self.prompts = {persona: self.prompt}

    def history(self, event):
        ch = event["channel"]; msgs = [event]
        if event.get("thread_ts"):
            try: msgs = self.client.conversations_replies(channel=ch, ts=event["thread_ts"], limit=self.cfg.get("history_limit", 20))["messages"]
            except Exception: pass
        h = []
        for m in msgs:
            txt = (m.get("text") or "").replace(f"<@{self.bot}>", "").strip()
            if m.get("user") == self.bot:
                h.append({"role": "assistant", "content": txt})
            else:
                who = m.get("username") or m.get("bot_id") if m.get("bot_id") else None
                h.append({"role": "user", "content": (f"[{who}] " if who else "") + txt})
        return h

    def post(self, ch, ts, text):
        self.client.chat_postMessage(channel=ch, thread_ts=ts, text=text)

    def handle(self, event):
        if ignored(event, self.bot): return
        ch, ts = event["channel"], event.get("thread_ts") or event["ts"]
        text = (event.get("text") or "").replace(f"<@{self.bot}>", "")
        blame = self.cfg.get("blame_followup", False) and self.persona == "babis" and core.is_blame(self.cfg, text)
        core.log.info("event ch=%s ts=%s persona=%s", ch, event["ts"], self.persona)
        hist = self.history(event)
        try: reply = (self.gen(self.prompt, hist) or "").strip()
        except Exception as e:
            core.log.error("llm error: %s", type(e).__name__); reply = FALLBACK
        if reply in meeting.TERSE:
            reply = meeting.KALOUSEK_FALLBACK if self.persona == "kalousek" else FALLBACK
        self.post(ch, ts, reply)
        if blame and "kalousek" in self.cfg["personas"]:  # optional legacy follow-up, off by default
            try: k = self.gen(core.build_prompt(self.cfg["personas"]["kalousek"]), hist + [{"role": "user", "content": reply}])
            except Exception as e: core.log.error("llm error: %s", type(e).__name__); k = meeting.KALOUSEK_FALLBACK
            self.post(ch, ts, k)

def dispatch(b, event):
    """Meeting coordinator gets first claim (dedupe by channel+ts across all persona apps)."""
    c = getattr(b, "coord", None)
    if c is not None:
        r = c.claim(event)
        if isinstance(r, meeting.Meeting): c.start(r); return "meeting"
        if r == "dup": return "dup"
    threading.Thread(target=b.handle, args=(event,), daemon=True).start(); return "solo"

def register(app, b):
    spawn = lambda ev: dispatch(b, ev)
    @app.event("app_mention")
    def _m(event):
        if not ignored(event, b.bot): spawn(event)
    @app.event("message")
    def _dm(event):
        if event.get("channel_type") == "im" and not ignored(event, b.bot): spawn(event)

def start_all(cfg, env=None, app_factory=None, handler_factory=None):
    """Start one Socket Mode app per persona with tokens. Returns {key: bridge}."""
    if app_factory is None:
        from slack_bolt import App as app_factory
    if handler_factory is None:
        from slack_bolt.adapter.socket_mode import SocketModeHandler as handler_factory
    started = {}; coord = meeting.Coordinator(cfg)
    for key in cfg["personas"]:
        bot, apptok = tokens(key, env)
        if not (bot and apptok):
            core.log.info("persona=%s skipped: missing SLACK_OKU_%s_BOT_TOKEN/APP_TOKEN", key, key.upper()); continue
        try:
            app = app_factory(token=bot)
            uid = app.client.auth_test()["user_id"]
            b = Bridge(app.client, cfg, uid, key)
            coord.add(key, b)
            register(app, b)
            handler_factory(app, apptok).connect()
            started[key] = b
            core.log.info("persona=%s connected user=%s", key, uid)
        except Exception as e:
            core.log.error("persona=%s failed to start: %s", key, type(e).__name__)
    return started

def main():
    logs = core.ROOT / "logs"; logs.mkdir(exist_ok=True)
    logging.basicConfig(filename=logs / "oku_slack.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = core.load_config()
    core.log.info("starting solo bots, personas=%s", ",".join(cfg["personas"]))
    started = start_all(cfg)
    if not started:
        core.log.error("no persona connected; exiting"); sys.exit(1)
    threading.Event().wait()

if __name__ == "__main__": main()
