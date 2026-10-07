"""Post as the OKÚ persona bots (Web API chat.postMessage only; no Socket Mode connection here, so the live
persona bridge keeps all its events). Tokens: SLACK_OKU_<KEY>_BOT_TOKEN (babis falls back to SLACK_OKU_BOT_TOKEN),
read from env or HKCU\\Environment like the wheel tokens; values are never logged.
Fallback when a persona has no bot/token or Slack refuses: the wheel bot posts the line, with a name override
(username) if it holds chat:write.customize, otherwise narrated by Monika.
Loop safety: these are bot messages (bot_id set); oku_slack.bridge.ignored() drops every bot_id/subtype event,
so persona bots never answer the wheel's lines; text is stripped of <@...>/<!...> mentions before posting."""
import logging, re

log = logging.getLogger("oku_wheel.personas")
NAMES = {"babis": "Andrej Babiš", "alenka": "Alenka Hranolka", "bourak": "Filip Bourák Turek", "marty": "Marty Prchal",
         "peta": "Peťa Maci", "kalousek": "Kalousek", "monika": "Monika Babišová", "macinka": "Petr Macinka"}
BOTS = ("babis", "alenka", "bourak", "marty", "peta", "kalousek")

def token_names(k):
    return ["SLACK_OKU_BABIS_BOT_TOKEN", "SLACK_OKU_BOT_TOKEN"] if k == "babis" else [f"SLACK_OKU_{k.upper()}_BOT_TOKEN"]

def _err(e):
    r = getattr(e, "response", None)
    try: return r["error"]
    except Exception: return type(e).__name__

def safe(text):
    t = re.sub(r"<[@!#][^>]*>", "", text or "")
    t = re.sub(r"@(here|channel|everyone)\b", "", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()

class PersonaPoster:
    def __init__(self, wheel_client, channel, token_fn=None, client_factory=None, customize=False, icon_base=""):
        self.wheel, self.channel, self.customize, self.icon_base = wheel_client, channel, customize, icon_base
        self.token_fn = token_fn or (lambda name: None)
        self.client_factory = client_factory
        self.clients, self.disabled = {}, set()

    def client(self, k):
        if k not in BOTS or k in self.disabled: return None
        if k not in self.clients:
            tok = next((t for t in (self.token_fn(n) for n in token_names(k)) if t), None)
            if not tok: self.disabled.add(k); log.info("persona %s: no bot token, wheel bot narrates", k); return None
            if self.client_factory is None:
                from slack_sdk import WebClient
                self.client_factory = WebClient
            self.clients[k] = self.client_factory(token=tok)
        return self.clients[k]

    def available(self): return [k for k in BOTS if self.client(k) is not None]

    def post(self, k, text, thread_ts=None):
        """Returns (ts, how) where how is 'persona' | 'customize' | 'narrated' | None (failed)."""
        text = safe(text)
        if not text: return None, None
        c = self.client(k)
        if c is not None:
            try:
                r = c.chat_postMessage(channel=self.channel, text=text, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
                return r["ts"], "persona"
            except Exception as e:
                code = _err(e); log.warning("persona %s post failed: %s (wheel bot narrates)", k, code)
                if code in ("invalid_auth", "account_inactive", "token_revoked", "not_in_channel", "channel_not_found"): self.disabled.add(k)
        name = NAMES.get(k, k)
        kw = {"channel": self.channel, "thread_ts": thread_ts, "unfurl_links": False, "unfurl_media": False}
        if self.customize:
            kw.update(text=text, username=name)
            if self.icon_base and k in BOTS: kw["icon_url"] = f"{self.icon_base.rstrip('/')}/{k}.png"
            how = "customize"
        elif k == "monika":
            kw["text"] = f"🎠 *Monika Babišová:* {text}"; how = "narrated"
        else:
            kw["text"] = f"🎠 *Monika:* A {name} na to: „{text}“"; how = "narrated"
        try:
            r = self.wheel.chat_postMessage(**kw); return r["ts"], how
        except Exception as e:
            log.warning("wheel narration failed: %s", _err(e)); return None, None
