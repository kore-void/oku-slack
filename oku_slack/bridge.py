"""Slack Socket Mode bridge for the OKÚ satire team: solo bots, one Slack app per persona.
Tokens per persona from env: SLACK_OKU_<KEY>_BOT_TOKEN / SLACK_OKU_<KEY>_APP_TOKEN
(babis falls back to SLACK_OKU_BOT_TOKEN / SLACK_OKU_APP_TOKEN). Gemini keys via env or
OKU_GEMINI_ENV_FILE. Secrets are never logged; only variable names / present-missing."""
import os, sys, logging, threading
from . import core, meeting, usage, followup, moderation, handoff

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
        self.reporter = None  # usage.Reporter (DM to Kore); failures never affect replies
        self.followup = None  # followup.Followup (Babiš only); cancelled by target user's message
        fc = cfg.get("capak_followup") or {}
        self.capak = fc.get("channel"); self.capak_prompt = None
        if persona == "babis" and self.capak:
            try:
                kb = (core.ROOT / "oku_slack" / "knowledge" / "capak_deepcuts.md").read_text(encoding="utf-8")
                self.capak_prompt = self.prompt + "\n\n" + kb + "\n\nPokud se tě někdo ptá na API, data nebo trading, smíš do této odpovědi vplést NEJVÝŠ JEDEN tip z Deep cuts (svým stylem). Jinak žádný."
            except OSError as e: core.log.warning("deepcuts unreadable: %s", type(e).__name__)

    def prompt_for(self, ch):
        return self.capak_prompt if (self.capak_prompt and ch == self.capak) else self.prompt

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
        if self.persona == "babis" and self.moderate(event): return
        ch, ts = event["channel"], event.get("thread_ts") or event["ts"]
        text = (event.get("text") or "").replace(f"<@{self.bot}>", "")
        blame = self.cfg.get("blame_followup", False) and self.persona == "babis" and core.is_blame(self.cfg, text)
        core.log.info("event ch=%s ts=%s persona=%s", ch, event["ts"], self.persona)
        hist = self.history(event)
        usage.set_context(persona=self.persona, channel=ch, thread_ts=ts, kind="solo"); usage.take_last()
        try: reply = (self.gen(self.prompt_for(ch), hist) or "").strip()
        except Exception as e:
            core.log.error("llm error: %s", type(e).__name__); reply = FALLBACK
        if reply in meeting.TERSE:
            reply = meeting.KALOUSEK_FALLBACK if self.persona == "kalousek" else FALLBACK
        self.post(ch, ts, reply)
        self.report()
        if blame and "kalousek" in self.cfg["personas"]:  # optional legacy follow-up, off by default
            try: k = self.gen(core.build_prompt(self.cfg["personas"]["kalousek"]), hist + [{"role": "user", "content": reply}])
            except Exception as e: core.log.error("llm error: %s", type(e).__name__); k = meeting.KALOUSEK_FALLBACK
            self.post(ch, ts, k)

    def moderate(self, event):
        """[moderation] invite/kick in allowed private channels (owner only). Never breaks normal replies."""
        try: return moderation.handle(self.client, self.cfg, self.bot, event)
        except Exception as e: core.log.warning("moderation failed: %s", type(e).__name__); return False

    def report(self):
        try:
            e = usage.take_last()
            if self.reporter and e: self.reporter.call(e)
        except Exception as ex: core.log.warning("usage report failed: %s", type(ex).__name__)

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
        f = getattr(b, "followup", None)
        if f is not None:
            try: f.on_message(event)
            except Exception as e: core.log.warning("followup on_message failed: %s", type(e).__name__)
        if event.get("channel_type") == "im" and not ignored(event, b.bot): spawn(event); return
        # "Andreji, vyhoď X" without @mention (mentions go via app_mention); owner only, others silently ignored
        if b.persona == "babis" and not ignored(event, b.bot) and f"<@{b.bot}>" not in (event.get("text") or ""):
            s = moderation.settings(b.cfg)
            if s and event.get("user") == s["owner"]: b.moderate(event)

def start_all(cfg, env=None, app_factory=None, handler_factory=None):
    """Start one Socket Mode app per persona with tokens. Returns {key: bridge}."""
    if app_factory is None:
        from slack_bolt import App as app_factory
    if handler_factory is None:
        from slack_bolt.adapter.socket_mode import SocketModeHandler as handler_factory
    started = {}; coord = meeting.Coordinator(cfg); usage.configure(cfg)
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
    if started:  # wheel -> bridge hand-off: oku_wheel asks for a real porada via logs/outbox (local files only)
        try: coord.inbox = handoff.Inbox(coord.start_external); coord.inbox.run(); core.log.info("meeting inbox: %s", handoff.outbox())
        except Exception as e: core.log.error("meeting inbox failed: %s", type(e).__name__)
    rep = usage.Reporter(started["babis"].client, cfg) if "babis" in started else None  # Babiš app DMs Kore
    coord.reporter = rep
    for b in started.values(): b.reporter = rep
    if "babis" in started:
        try: started["babis"].followup = followup.start(started["babis"].client, cfg, core.ROOT / "logs")
        except Exception as e: core.log.error("followup start failed: %s", type(e).__name__)
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
