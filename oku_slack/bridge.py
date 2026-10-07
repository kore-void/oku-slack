"""Slack Socket Mode bridge for the OKĂš satire team. One app, per-persona username/icon via chat:write.customize.
Secrets only from env (SLACK_OKU_BOT_TOKEN, SLACK_OKU_APP_TOKEN, Gemini keys via env or OKU_GEMINI_ENV_FILE); never logged."""
import os, logging, threading, pathlib
from . import core

FALLBACK = "Technika selhala. To je kampaĹ!"

class Bridge:
    def __init__(self, client, cfg, bot_id, gen=core.generate):
        self.client, self.cfg, self.bot, self.gen = client, cfg, bot_id, gen
        self.prompts = {k: core.build_prompt(p) for k, p in cfg["personas"].items()}

    def history(self, event):
        ch = event["channel"]; msgs = [event]
        if event.get("thread_ts"):
            try: msgs = self.client.conversations_replies(channel=ch, ts=event["thread_ts"], limit=self.cfg.get("history_limit", 20))["messages"]
            except Exception: pass
        h = []
        for m in msgs:
            txt = (m.get("text") or "").replace(f"<@{self.bot}>", "").strip()
            who = m.get("username")
            mine = m.get("user") == self.bot or m.get("bot_id")
            h.append({"role": "assistant" if mine else "user", "content": (f"[{who}] " if mine and who else "") + txt})
        return h

    def post(self, ch, ts, key, text):
        p = self.cfg["personas"][key]
        kw = dict(channel=ch, thread_ts=ts, text=text, username=p["name"])
        u = core.icon_url(self.cfg, p)
        if u: kw["icon_url"] = u
        self.client.chat_postMessage(**kw)

    def handle(self, event):
        ch, ts = event["channel"], event.get("thread_ts") or event["ts"]
        text = (event.get("text") or "").replace(f"<@{self.bot}>", "")
        key, blame = core.route(self.cfg, text, ch)
        core.log.info("event ch=%s ts=%s persona=%s blame=%s", ch, event["ts"], key, blame)
        hist = self.history(event)
        try: reply = self.gen(self.prompts[key], hist)
        except Exception as e:
            core.log.error("llm error: %s", type(e).__name__); reply = FALLBACK
        self.post(ch, ts, key, reply)
        if blame and "kalousek" in self.cfg["personas"]:
            try: k = self.gen(self.prompts["kalousek"], hist + [{"role": "user", "content": reply}])
            except Exception as e: core.log.error("llm error: %s", type(e).__name__); k = "â€¦"
            self.post(ch, ts, "kalousek", k)

def main():
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    logs = core.ROOT / "logs"; logs.mkdir(exist_ok=True)
    logging.basicConfig(filename=logs / "oku_slack.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = App(token=os.environ["SLACK_OKU_BOT_TOKEN"])
    b = Bridge(app.client, core.load_config(), app.client.auth_test()["user_id"])
    spawn = lambda ev: threading.Thread(target=b.handle, args=(ev,), daemon=True).start()
    @app.event("app_mention")
    def _m(event): spawn(event)
    @app.event("message")
    def _dm(event):
        if event.get("channel_type") == "im" and not event.get("bot_id") and not event.get("subtype"): spawn(event)
    core.log.info("starting, personas=%s", ",".join(b.prompts))
    SocketModeHandler(app, os.environ["SLACK_OKU_APP_TOKEN"]).start()

if __name__ == "__main__": main()
