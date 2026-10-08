"""Live Slack channel canvas 'OKÚ Kolo · živě'. Slack canvases cannot run JS, so the bot rewrites the
whole canvas (canvases.edit replace) on state changes, throttled. Missing scopes / not_in_channel are
logged and retried later; the wheel keeps running."""
import logging, threading, time

log = logging.getLogger("oku_wheel.canvas")
TITLE = "OKÚ Kolo · živě"
KV_KEY = "canvas_id"
SOFT_ERRORS = {"missing_scope", "not_in_channel", "channel_not_found", "not_allowed_token_type", "canvas_disabled_user_team",
               "restricted_action", "access_denied", "free_teams_cannot_create_non_tabbed_canvases", "ratelimited"}
STATE_CZ = {"pending": "čeká na potvrzení", "ready": "potvrzeno, čeká na start", "live": "🔴 běží", "done": "skončila", "expired": "propadla",
            "vetoed": "vetována"}

def _err(e):
    r = getattr(e, "response", None)
    try: return r["error"]
    except Exception: return getattr(getattr(r, "data", None) or {}, "get", lambda k: None)("error") or type(e).__name__

def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))

def _mmss(s):
    s = max(0, int(s)); return f"{s // 60}:{s % 60:02d}"

def markdown(eng, image_url=None, now=None):
    """Pure: canvas markdown for the current engine state."""
    snap = eng.snapshot(); now = snap["now"] if now is None else now
    e, players = snap["event"], eng.cfg["players"]
    out = [f"# 🎡 {TITLE}", ""]
    if e:
        tag = "🌟 LEGENDÁRNÍ · " if e.get("legendary") else ""
        out += [f"## {tag}{e['title']}", f"Stav: **{STATE_CZ.get(e['state'], e['state'])}** · start **{_hm(e['start_at'])}**"
                + (f" (za ~{_mmss(e['start_at'] - now)})" if e["state"] in ("pending", "ready") and e["start_at"] > now else ""), ""]
        out.append("**Potvrzení účasti:**")
        for k, p in players.items():
            c = e["confirmed"].get(k)
            out.append(f"- {p['name']}: " + (f"✅ potvrzeno ({'kód' if c['how'] == 'code' else 'podržení'})" if c else "⏳ čeká"))
        out.append("")
        h = e.get("host_say") or {}
        if h.get("text"): out += [f"> 🎠 **{h.get('name', 'Monika Babišová')}:** {h['text']}", ""]
        cin = e.get("cinematic") or {}
        if cin.get("active") and cin.get("data"):
            b = cin["data"]
            out += ["## 📺 PŘÍMÝ PŘENOS", f"**{b['caption']}**", f"_{b['direction']}_"]
            cast = (e.get("script_data") or {}).get("cast", {})
            out += [f"- **{cast.get(sp, sp)}:** {tx}" for sp, tx in b.get("lines", [])] + [""]
    else:
        out += ["Žádná událost. Roztoč kolo: `/kolo`", ""]
    if image_url: out += [f"![Kolo]({image_url})", ""]
    out.append("## ⚡ Nabité příkazy")
    for k, p in snap["players"].items():
        left = p["cooldown_left"]
        out.append(f"- {p['name']} · _{players[k].get('command', 'příkaz')}_: " + ("✅ připraven" if left <= 0 else f"nabíjí se, připraven v {_hm(now + left)}"))
    for q in snap["sequences"]:
        st = [s for s in q["steps"] if now >= s["at"]]
        out.append(f"- 🔥 Běží: **{q['label']}** ({players[q['player']]['name']}) · {st[-1]['text'] if st else ''} · do {_hm(q['end_at'])}")
    out.append("")
    try:
        from ..world import view as world_view
        out += world_view.canvas_section(eng)
    except Exception as ex: log.warning("canvas world section failed: %s", type(ex).__name__)
    out += ["## 💬 Chat z roomky"]
    chat = snap["chat"][-10:]
    out += [f"- **{players.get(c['player'], {}).get('name', c['player'])}** ({_hm(c['ts'])}): {c['text']}" for c in chat] or ["_zatím ticho_"]
    out += ["", f"_Aktualizováno {_hm(now)}. Animace a hudba v roomce._"]
    return "\n".join(out)

class CanvasSync:
    def __init__(self, eng, client, channel, store, clock=time.time, min_interval=3.0, refresh_s=60.0, retry_s=300.0):
        self.eng, self.client, self.channel, self.store, self.clock = eng, client, channel, store, clock
        self.min_interval, self.refresh_s, self.retry_s = min_interval, refresh_s, retry_s
        self.dirty, self.last_edit, self.blocked_until, self.image_url, self.edits = True, -1e18, 0.0, None, 0
        self.lock = threading.Lock()

    def request(self): self.dirty = True
    def set_image(self, url): self.image_url = url; self.dirty = True

    def _soft(self, where, e):
        code = _err(e)
        if code in SOFT_ERRORS:
            log.warning("canvas %s skipped: %s (retry in %ss)", where, code, int(self.retry_s))
            self.blocked_until = self.clock() + self.retry_s
            return None
        log.error("canvas %s failed: %s", where, code); self.blocked_until = self.clock() + self.min_interval
        return None

    def ensure(self, md):
        cid = self.store.kv_get(KV_KEY)
        if cid: return cid, False
        doc = {"type": "markdown", "markdown": md}
        try:
            cid = self.client.conversations_canvases_create(channel_id=self.channel, document_content=doc)["canvas_id"]
        except Exception as e:
            if _err(e) != "channel_canvas_already_exists": return self._soft("create", e), False
            try:  # channel already has a channel canvas: standalone canvas shared to the channel
                cid = self.client.canvases_create(title=TITLE, document_content=doc)["canvas_id"]
                self.client.canvases_access_set(canvas_id=cid, access_level="read", channel_ids=[self.channel])
            except Exception as e2: return self._soft("create_standalone", e2), False
        self.store.kv_set(KV_KEY, cid); log.info("canvas created")
        return cid, True

    def flush(self, force=False):
        """Edit the canvas if dirty (or periodic refresh) and not throttled. Returns True if Slack was called."""
        with self.lock:
            now = self.clock()
            if now < self.blocked_until and not force: return False
            due = self.dirty or now - self.last_edit >= self.refresh_s
            if not due or (now - self.last_edit < self.min_interval and not force): return False
            md = markdown(self.eng, self.image_url)
            cid, created = self.ensure(md)
            if not cid: return False
            self.dirty, self.last_edit = False, now
            if created: self.edits += 1; return True
            try:
                self.client.canvases_edit(canvas_id=cid, changes=[{"operation": "replace", "document_content": {"type": "markdown", "markdown": md}}])
                self.edits += 1; return True
            except Exception as e:
                if _err(e) in ("canvas_not_found", "invalid_canvas"): self.store.kv_set(KV_KEY, "")
                self.dirty = True; self._soft("edit", e); return False

    def run(self, period=1.0):
        def loop():
            while True:
                try: self.flush()
                except Exception as e: log.error("canvas loop: %s", type(e).__name__)
                time.sleep(period)
        threading.Thread(target=loop, daemon=True, name="kolo-canvas").start()
