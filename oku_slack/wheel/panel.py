"""The main Slack experience: ONE persistent interactive Block Kit 'control panel' message in the wheel
channel. Stored as kv panel_channel/panel_ts in SQLite; re-rendered (chat.update) on every state change,
throttled. Images are static assets on https://www.itzkore.cz/oku/kolo/img/ (deploy-wheel-room.ps1):
kolo.png (idle), kolo-spin-<key>.gif (spin, lands in the segment centre), kolo-<key>.png (result)."""
import logging, os, threading, time

log = logging.getLogger("oku_wheel.panel")
IMG_BASE = os.environ.get("OKU_WHEEL_IMG_BASE", "https://www.itzkore.cz/oku/kolo/img/")
SPIN_SHOW_S = 3.5  # spin GIF is shown this long, then the result PNG
ACTIONS = {"kolo_spin": "toc", "kolo_confirm": "potvrdit", "kolo_command": "prikaz", "kolo_status": "stav"}
STATE_CZ = {"pending": "⏳ čeká na potvrzení", "ready": "✅ potvrzeno, čeká na start", "live": "🔴 běží",
            "done": "🏁 skončila", "expired": "💤 propadla"}
SOFT = {"not_in_channel", "channel_not_found", "missing_scope", "ratelimited", "is_archived", "restricted_action"}

def _err(e):
    r = getattr(e, "response", None)
    try: return r["error"]
    except Exception: return type(e).__name__

def _date(ts, fmt="{time}"):
    return f"<!date^{int(ts)}^{fmt}|{time.strftime('%H:%M', time.localtime(ts))}>"

def image_for(e, now, base=IMG_BASE):
    """(url, alt, kind). Spin GIF for SPIN_SHOW_S after spin_at, then the result; idle wheel without an event."""
    if not e or e["state"] in ("done", "expired"): return base + "kolo.png", "Kolo štěstí OKÚ", "idle"
    if e.get("legendary") and e["state"] == "live": return base + "titanic.png", "Titanic scéna", "poster"
    if now < e["spin_at"] + SPIN_SHOW_S: return f"{base}kolo-spin-{e['key']}.gif?v={e['id']}", "Kolo se točí", "spin"
    return f"{base}kolo-{e['key']}.png?v={e['id']}", f"Výsledek: {e['title']}", "result"

def _t(s, n=2900): return s if len(s) <= n else s[: n - 1] + "…"

def blocks(eng, now=None, base=IMG_BASE):
    """Pure: Block Kit for the panel. Never contains the event code."""
    snap = eng.snapshot(); now = snap["now"] if now is None else now
    e, players = snap["event"], eng.cfg["players"]
    url, alt, kind = image_for(e, now, base)
    b = [{"type": "header", "text": {"type": "plain_text", "text": "🎡 OKÚ Kolo štěstí"}},
         {"type": "image", "image_url": url, "alt_text": alt}]
    h = (e or {}).get("host_say") or {}
    host = h.get("text") or "Nastupovat, kolotoč čeká! Kdo roztočí první?"
    b.append({"type": "section", "text": {"type": "mrkdwn", "text": _t(f"🎠 *{h.get('name', 'Monika Babišová')}:* {host}")}})
    if e and kind != "spin":
        tag = "🌟 *LEGENDÁRNÍ* · " if e.get("legendary") else ""
        when = f"start {_date(e['start_at'])}"
        if e["state"] in ("pending", "ready") and e["start_at"] > now: when += f" (za ~{max(1, int((e['start_at'] - now) // 60))} min)"
        b.append({"type": "section", "text": {"type": "mrkdwn", "text": _t(f"{tag}*{e['title']}*\n{STATE_CZ.get(e['state'], e['state'])} · {when}")}})
        conf = " · ".join(f"{p['name']} {'✅' if k in e['confirmed'] else '⏳'}" for k, p in players.items())
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Potvrzení: {conf}"}]})
        cin = e.get("cinematic") or {}
        if cin.get("active") and cin.get("data"):
            d = cin["data"]; cast = (e.get("script_data") or {}).get("cast", {})
            lines = "\n".join(f"*{cast.get(sp, sp)}:* {tx}" for sp, tx in d.get("lines", []))
            b.append({"type": "section", "text": {"type": "mrkdwn", "text": _t(f"📺 *PŘÍMÝ PŘENOS · {d['caption']}*\n_{d['direction']}_\n{lines}")}})
    elif kind == "spin":
        b.append({"type": "section", "text": {"type": "mrkdwn", "text": "🎡 *Kolo se točí…*"}})
    cds = []
    for k, p in snap["players"].items():
        left = p["cooldown_left"]
        cds.append(f"{p['name']} _{players[k].get('command', 'příkaz')}_: " + ("⚡ připraven" if left <= 0 else f"🔋 nabíjí se do {_date(now + left)}"))
    for q in snap["sequences"]:
        st = [s for s in q["steps"] if now >= s["at"]]
        cds.append(f"🔥 *{q['label']}* ({players[q['player']]['name']}): {st[-1]['text'] if st else ''} · do {_date(q['end_at'])}")
    b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _t(" · ".join(cds), 2900)}]})
    can_spin = not e or e["state"] in ("done", "expired")
    btns = []
    if can_spin: btns.append({"type": "button", "action_id": "kolo_spin", "style": "primary", "text": {"type": "plain_text", "text": "🎡 Točit"}})
    if e and e["state"] == "pending":
        btns.append({"type": "button", "action_id": "kolo_confirm", "style": "primary", "text": {"type": "plain_text", "text": "✅ Potvrdit účast"}})
    btns += [{"type": "button", "action_id": "kolo_command", "text": {"type": "plain_text", "text": "⚡ Nabitý příkaz"}},
             {"type": "button", "action_id": "kolo_status", "text": {"type": "plain_text", "text": "ℹ️ Stav"}}]
    b.append({"type": "actions", "block_id": "kolo_actions", "elements": btns})
    b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Aktualizováno {_date(now)} · potvrzení jen kódem události (modál)"}]})
    return b, kind

def fallback_text(eng):
    e = eng.active_event()
    return f"OKÚ Kolo: {e['title']} ({e['state']})" if e else "OKÚ Kolo štěstí"

class Panel:
    def __init__(self, eng, client, channel, store, clock=time.time, min_interval=2.0, refresh_s=60.0, retry_s=120.0, base=IMG_BASE):
        self.eng, self.client, self.channel, self.store, self.clock, self.base = eng, client, channel, store, clock, base
        self.min_interval, self.refresh_s, self.retry_s = min_interval, refresh_s, retry_s
        self.dirty, self.last, self.blocked_until, self.last_kind, self.last_error, self.updates = True, -1e18, 0.0, None, None, 0
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

    def post_new(self):
        """(Re-)post the panel message and remember it. Returns ts or None."""
        with self.lock:
            bl, kind = blocks(self.eng, self.clock(), self.base)
            try: r = self.client.chat_postMessage(channel=self.channel, text=fallback_text(self.eng), blocks=bl)
            except Exception as e: return self._fail("post", e)
            ch, ts = r.get("channel", self.channel), r["ts"]
            self.store.kv_set("panel_channel", ch); self.store.kv_set("panel_ts", ts)
            self.dirty, self.last, self.last_kind, self.last_error = False, self.clock(), kind, None
            self.updates += 1; log.info("panel posted"); return ts

    def flush(self, force=False):
        now = self.clock()
        if now < self.blocked_until and not force: return False
        ch, ts = self.where()
        if not ts: return bool(self.post_new())
        with self.lock:
            e = self.eng.active_event() or None
            want_kind = image_for(self.eng.snapshot()["event"], now, self.base)[2]
            if want_kind != self.last_kind: self.dirty = True  # spin GIF -> result PNG switch
            due = self.dirty or now - self.last >= self.refresh_s
            if not due or (now - self.last < self.min_interval and not force): return False
            bl, kind = blocks(self.eng, now, self.base)
            try: self.client.chat_update(channel=ch, ts=ts, text=fallback_text(self.eng), blocks=bl)
            except Exception as ex:
                if _err(ex) in ("message_not_found", "cant_update_message"): self.store.kv_set("panel_ts", "")
                self.dirty = True; return self._fail("update", ex)
            self.dirty, self.last, self.last_kind, self.last_error = False, now, kind, None
            self.updates += 1; return True

    def run(self, period=0.5):
        def loop():
            while True:
                try: self.flush()
                except Exception as ex: log.error("panel loop: %s", type(ex).__name__)
                time.sleep(period)
        threading.Thread(target=loop, daemon=True, name="kolo-panel").start()
