"""The main Slack experience: ONE persistent interactive Block Kit 'show' message in the wheel channel.
Stored as kv panel_channel/panel_ts in SQLite; re-rendered (chat.update, >= 1 s apart) on every state change.
Spin choreography (by server time since spin_at): drums -> spin GIF -> 'A je to...' -> result card.
Static images: https://www.itzkore.cz/oku/kolo/img/ (deploy-wheel-room.ps1). The result card with the start
time is rendered per event and uploaded via files_upload_v2 (slack_file image block); static card fallback."""
import logging, os, re, threading, time

log = logging.getLogger("oku_wheel.panel")
IMG_BASE = os.environ.get("OKU_WHEEL_IMG_BASE", "https://www.itzkore.cz/oku/kolo/img/")
DRUMS_S, SPIN_END_S, ALMOST_END_S = 1.2, 4.7, 6.2
SOFT = {"not_in_channel", "channel_not_found", "missing_scope", "ratelimited", "is_archived", "restricted_action"}
HOST_NAMES = {"babis": "Andrej Babiš", "alenka": "Alenka Hranolka", "bourak": "Filip Bourák Turek", "marty": "Marty Prchal",
              "peta": "Peťa Maci", "kalousek": "Kalousek", "monika": "Monika Babišová"}
STATE_CZ = {"pending": "⏳ čeká na potvrzení", "ready": "✅ připraveno", "live": "🔴 běží", "done": "🏁 skončeno", "expired": "💤 propadlo"}

def _err(e):
    r = getattr(e, "response", None)
    try: return r["error"]
    except Exception: return type(e).__name__

def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))
def _date(ts, fmt="{time}"): return f"<!date^{int(ts)}^{fmt}|{_hm(ts)}>"
def _t(s, n=2900): return s if len(s) <= n else s[: n - 1] + "…"

def phase(e, now):
    if not e or e["state"] in ("done", "expired"): return "idle"
    if e["state"] == "live": return "legend" if e.get("legendary") else "live"
    dt = now - e["spin_at"]
    if dt < DRUMS_S: return "drums"
    if dt < SPIN_END_S: return "spin"
    if dt < ALMOST_END_S: return "almost"
    return "result"

def who(eng, k):
    sid = eng.cfg["players"][k].get("slack_id", "")
    return f"<@{sid}>" if re.fullmatch(r"[UW][A-Z0-9]{6,}", sid or "") else eng.cfg["players"][k]["name"]

def image_block(e, ph, base=IMG_BASE, card=None):
    if ph in ("idle", "drums"): url, title = base + "kolo.png", ("🥁 Bubny…" if ph == "drums" else "Kolo štěstí OKÚ")
    elif ph in ("spin", "almost"): url, title = f"{base}kolo-spin-{e['key']}.gif?v={e['id']}", ("🎡 Točí se…" if ph == "spin" else "A je to…")
    elif ph == "legend": url, title = base + "titanic.png", "🔴 PŘÍMÝ PŘENOS"
    else:
        title = e["title"]
        if card: return {"type": "image", "slack_file": {"id": card}, "alt_text": title, "title": {"type": "plain_text", "text": title[:2000]}}
        url = f"{base}card-{e['key']}.png?v={e['id']}"
    return {"type": "image", "image_url": url, "alt_text": title, "title": {"type": "plain_text", "text": title[:2000]}}

def blocks(eng, now=None, base=IMG_BASE, card=None):
    """Pure: Block Kit for the panel (<= 50 blocks, never the event code). Returns (blocks, phase)."""
    snap = eng.snapshot(); now = snap["now"] if now is None else now
    e, players = snap["event"], eng.cfg["players"]; ph = phase(e, now)
    h = (e or {}).get("host_say") or {}
    if ph == "drums": line = "Bubny, prosím! Kolotoč se roztáčí…"
    elif ph in ("spin", "almost"): line = "Točí se, točí… držte si klobouky!" if ph == "spin" else "A je to… a je to…"
    elif ph == "idle" and (not e or e["state"] == "done"): line = "Nastupovat, kolotoč čeká! Kdo roztočí první?"
    else: line = h.get("text") or "Nastupovat, kolotoč čeká!"
    b = [{"type": "header", "text": {"type": "plain_text", "text": "🎡 OKÚ KOLO ŠTĚSTÍ"}},
         {"type": "context", "elements": [{"type": "mrkdwn", "text": _t(f"🎠 *Monika Babišová* · _{line}_", 2900)}]},
         image_block(e, ph, base, card)]
    if ph == "legend":
        cin = e.get("cinematic") or {}
        if cin.get("data"):
            d = cin["data"]; cast = (e.get("script_data") or {}).get("cast", {})
            q = [f"> *{d['caption']}*", f"> _{d['direction']}_"] + [f"> *{cast.get(sp, sp)}:* {tx}" for sp, tx in d.get("lines", [])]
            b.append({"type": "section", "text": {"type": "mrkdwn", "text": _t("\n".join(q))}})
    if e and ph in ("result", "live", "legend") or (e and e["state"] == "expired"):
        start = f"{_date(e['start_at'])} · {_date(e['start_at'], '{ago}')}" if e["state"] in ("pending", "ready") else STATE_CZ.get(e["state"], e["state"])
        conf = "\n".join(f"{'✅' if k in e['confirmed'] else '⏳'} {who(eng, k)}" for k in players)
        cds, seqs = [], []
        for k, p in snap["players"].items():
            cds.append(f"⚡ {p['name']} připraven" if p["cooldown_left"] <= 0 else f"🔋 {p['name']} do {_date(now + p['cooldown_left'])}")
        for q in snap["sequences"]:
            st = [s for s in q["steps"] if now >= s["at"]]
            seqs.append(f"🔥 *{q['label']}* · {st[-1]['text'] if st else ''}")
        b.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": _t(f"*Start*\n{start}", 1900)},
            {"type": "mrkdwn", "text": _t(f"*Potvrzení*\n{conf}", 1900)},
            {"type": "mrkdwn", "text": _t("*Nabité příkazy*\n" + "\n".join(cds), 1900)},
            {"type": "mrkdwn", "text": _t("*Sekvence*\n" + ("\n".join(seqs) or "—"), 1900)}]})
    elif not e or ph == "idle":
        cds = " · ".join((f"⚡ {p['name']}" if p["cooldown_left"] <= 0 else f"🔋 {p['name']} do {_date(now + p['cooldown_left'])}") for p in snap["players"].values())
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Nabité příkazy: {cds}"}]})
    b.append({"type": "divider"})
    btns = [{"type": "button", "action_id": "kolo_spin", "style": "primary", "text": {"type": "plain_text", "text": "🎡 Točit"}}]
    if e and e["state"] == "pending" and ph == "result":
        btns.append({"type": "button", "action_id": "kolo_confirm", "style": "primary", "text": {"type": "plain_text", "text": "✅ Potvrdit účast"}})
    btns.append({"type": "button", "action_id": "kolo_command", "text": {"type": "plain_text", "text": "⚡ Nabitý příkaz"}})
    btns.append({"type": "overflow", "action_id": "kolo_more", "options": [{"text": {"type": "plain_text", "text": "ℹ️ Stav"}, "value": "stav"}]})
    b.append({"type": "actions", "block_id": "kolo_actions", "elements": btns})
    b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"OKÚ Kolo · aktualizováno {_date(now)}"}]})
    return b, ph

ACTIONS = {"kolo_spin": "toc", "kolo_confirm": "potvrdit", "kolo_command": "prikaz", "kolo_status": "stav"}

def action_arg(action):
    aid = action.get("action_id")
    if aid == "kolo_more": return (action.get("selected_option") or {}).get("value")
    return ACTIONS.get(aid)

def fallback_text(eng):
    e = eng.active_event()
    return f"OKÚ Kolo štěstí: {e['title']}" if e else "OKÚ Kolo štěstí"

class Panel:
    def __init__(self, eng, client, channel, store, clock=time.time, min_interval=1.0, refresh_s=60.0, retry_s=120.0,
                 base=IMG_BASE, card_renderer=None):
        self.eng, self.client, self.channel, self.store, self.clock, self.base = eng, client, channel, store, clock, base
        self.min_interval, self.refresh_s, self.retry_s = min_interval, refresh_s, retry_s
        self.card_renderer = card_renderer  # callable(event) -> PNG bytes; None = static card URL only
        self.dirty, self.last, self.blocked_until, self.last_phase, self.last_error, self.updates = True, -1e18, 0.0, None, None, 0
        self.cards, self.card_failed = {}, set()
        self.lock = threading.Lock()

    def request(self): self.dirty = True

    def where(self):
        ch, ts = self.store.kv_get("panel_channel"), self.store.kv_get("panel_ts")
        return (ch, ts) if ch and ts else (None, None)

    def _fail(self, what, e):
        code = _err(e); self.last_error = code
        log.warning("panel %s failed: %s", what, code)
        self.blocked_until = self.clock() + (self.retry_s if code in SOFT else self.min_interval)
        return None

    def _card(self, e, ph):
        if ph != "result" or not e or not self.card_renderer or e["id"] in self.card_failed: return None
        if e["id"] in self.cards: return self.cards[e["id"]]
        try:
            r = self.client.files_upload_v2(content=self.card_renderer(e), filename=f"kolo-{e['id']}.png", title=e["title"])
            f = r.get("file") or (r.get("files") or [{}])[0]
            self.cards[e["id"]] = f.get("id"); return self.cards[e["id"]]
        except Exception as ex:
            log.warning("result card upload failed: %s (static card)", _err(ex)); self.card_failed.add(e["id"]); return None

    def _render(self, now):
        e = self.eng.snapshot()["event"]; ph = phase(e, now)
        return blocks(self.eng, now, self.base, self._card(e, ph)), e

    def post_new(self):
        """(Re-)post the panel and remember it. Returns ts or None."""
        with self.lock:
            (bl, ph), _ = self._render(self.clock())
            try: r = self.client.chat_postMessage(channel=self.channel, text=fallback_text(self.eng), blocks=bl)
            except Exception as e: return self._fail("post", e)
            self.store.kv_set("panel_channel", r.get("channel", self.channel)); self.store.kv_set("panel_ts", r["ts"])
            self.dirty, self.last, self.last_phase, self.last_error = False, self.clock(), ph, None
            self.updates += 1; log.info("panel posted"); return r["ts"]

    def flush(self, force=False):
        now = self.clock()
        if now < self.blocked_until and not force: return False
        ch, ts = self.where()
        if not ts: return bool(self.post_new())
        with self.lock:
            if phase(self.eng.snapshot()["event"], now) != self.last_phase: self.dirty = True  # choreography step
            due = self.dirty or now - self.last >= self.refresh_s
            if not due or (now - self.last < self.min_interval and not force): return False
            (bl, ph), e = self._render(now)
            try: self.client.chat_update(channel=ch, ts=ts, text=fallback_text(self.eng), blocks=bl)
            except Exception as ex:
                code = _err(ex)
                if code in ("message_not_found", "cant_update_message"): self.store.kv_set("panel_ts", "")
                if code == "invalid_blocks" and e and e["id"] in self.cards:  # slack_file not accepted -> static card
                    self.card_failed.add(e["id"]); self.cards.pop(e["id"], None)
                self.dirty = True; return self._fail("update", ex)
            self.dirty, self.last, self.last_phase, self.last_error = False, now, ph, None
            self.updates += 1; return True

    def run(self, period=0.25):
        def loop():
            while True:
                try: self.flush()
                except Exception as ex: log.error("panel loop: %s", type(ex).__name__)
                time.sleep(period)
        threading.Thread(target=loop, daemon=True, name="kolo-panel").start()
