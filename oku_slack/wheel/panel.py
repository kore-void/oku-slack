"""The main Slack experience: ONE persistent interactive Block Kit 'show' message in the wheel channel.
Stored as kv panel_channel/panel_ts in SQLite; re-rendered (chat.update, >= 1 s apart) on every state change.
Spin choreography (by server time since spin_at): drums -> spin GIF -> 'A je to...' -> result card.
Static images: https://www.itzkore.cz/oku/kolo/img/ (deploy-wheel-room.ps1). The result card with the start
time is rendered per event and uploaded via files_upload_v2 (slack_file image block); static card fallback.
Resilience (P-001): soft errors wait retry_s; other errors back off exponentially per repeated code (min_interval *
2^(n-1), capped at backoff_cap_s) and log Slack's response_metadata.messages. After degrade_after consecutive
invalid_blocks the panel renders text-only (no image blocks) for degrade_s. A freshly uploaded result card is only
referenced (slack_file) card_ready_s after the upload; until then the static card URL is shown."""
import logging, os, re, threading, time
from . import show as _show

log = logging.getLogger("oku_wheel.panel")
IMG_BASE = os.environ.get("OKU_WHEEL_IMG_BASE", "https://www.itzkore.cz/oku/kolo/img/")
DRUMS_S, SPIN_END_S, ALMOST_END_S = 1.2, 4.7, 6.2
SOFT = {"not_in_channel", "channel_not_found", "missing_scope", "ratelimited", "is_archived", "restricted_action"}
HOST_NAMES = {"babis": "Andrej Babiš", "alenka": "Alenka Hranolka", "bourak": "Filip Bourák Turek", "marty": "Marty Prchal",
              "peta": "Peťa Maci", "kalousek": "Kalousek", "monika": "Monika Babišová"}
STATE_CZ = {"pending": "⏳ čeká na potvrzení", "ready": "✅ připraveno", "live": "🔴 běží", "done": "🏁 skončeno", "expired": "💤 propadlo",
            "vetoed": "🙅 vetováno"}
BET_AMOUNTS = (50, 100, 250)
BETTING_REFRESH_S = 5.0
def _pts(n): return f"{int(n):,}".replace(",", "\u00a0") + " 🪙"

def _err(e):
    r = getattr(e, "response", None)
    try: return r["error"]
    except Exception: return type(e).__name__

def _meta(e, n=600):
    """Slack's response_metadata.messages (block validation details; never contains tokens), joined and truncated."""
    r = getattr(e, "response", None)
    try: msgs = (r.get("response_metadata") or {}).get("messages") or []
    except Exception:
        try: msgs = r["response_metadata"]["messages"]
        except Exception: msgs = []
    txt = " | ".join(str(m) for m in msgs)
    return txt if len(txt) <= n else txt[: n - 1] + "…"

def _hm(ts): return time.strftime("%H:%M", time.localtime(ts))
def _date(ts, fmt="{time}"): return f"<!date^{int(ts)}^{fmt}|{_hm(ts)}>"
def _t(s, n=2900): return s if len(s) <= n else s[: n - 1] + "…"

def phase(e, now, rnd=None):
    if not e or e["state"] in ("done", "expired", "vetoed"): return "betting" if rnd else "idle"
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
    if ph == "betting": url, title = base + "kolo.png", "💰 Sázky běží"
    elif ph in ("idle", "drums"): url, title = base + "kolo.png", ("🥁 Bubny…" if ph == "drums" else "Kolo štěstí OKÚ")
    elif ph in ("spin", "almost"): url, title = f"{base}kolo-spin-{e['key']}.gif?v={e['id']}", ("🎡 Točí se…" if ph == "spin" else "A je to…")
    elif ph == "legend": url, title = base + "titanic.png", "🔴 PŘÍMÝ PŘENOS"
    else:
        title = e["title"]
        if card: return {"type": "image", "slack_file": {"id": card}, "alt_text": title, "title": {"type": "plain_text", "text": title[:2000]}}
        url = f"{base}card-{e['key']}.png?v={e['id']}"
    return {"type": "image", "image_url": url, "alt_text": title, "title": {"type": "plain_text", "text": title[:2000]}}

def blocks(eng, now=None, base=IMG_BASE, card=None, images=True):
    """Pure: Block Kit for the panel (<= 50 blocks, never the event code). Returns (blocks, phase).
    images=False: text-only degrade (the image block becomes a context line with its title)."""
    snap = eng.snapshot(); now = snap["now"] if now is None else now
    e, players, rnd = snap["event"], eng.cfg["players"], snap.get("round"); ph = phase(e, now, rnd)
    h = (e or {}).get("host_say") or {}
    if ph == "betting":
        pool = eng.cfg.get("host", {}).get("lines", {}).get("bets") or ["Sázky jsou otevřené!"]
        line = pool[int(rnd["id"], 16) % len(pool)]
    elif ph == "drums": line = "Bubny, prosím! Kolotoč se roztáčí…"
    elif ph in ("spin", "almost"): line = "Točí se, točí… držte si klobouky!" if ph == "spin" else "A je to… a je to…"
    elif ph == "idle" and (not e or e["state"] == "done"): line = "Nastupovat, kolotoč čeká! Kdo roztočí první?"
    else: line = h.get("text") or "Nastupovat, kolotoč čeká!"
    b = [{"type": "header", "text": {"type": "plain_text", "text": "🎡 OKÚ KOLO ŠTĚSTÍ"}},
         {"type": "context", "elements": [{"type": "mrkdwn", "text": _t(f"🎠 *Monika Babišová* · _{line}_", 2900)}]},
         image_block(e, ph, base, card)]
    if not images:
        b[2] = {"type": "context", "elements": [{"type": "mrkdwn", "text": _t(f"🖼️ {b[2]['title']['text']}", 2900)}]}
    if ph == "betting": b += betting_blocks(eng, snap, rnd, now)
    if ph in ("live", "legend"): b += show_blocks(eng, e, now)
    if ph == "legend":
        cin = e.get("cinematic") or {}
        if cin.get("data"):
            d = cin["data"]; cast = (e.get("script_data") or {}).get("cast", {})
            q = [f"> *{d['caption']}*", f"> _{d['direction']}_"] + [f"> *{cast.get(sp, sp)}:* {tx}" for sp, tx in d.get("lines", [])]
            b.append({"type": "section", "text": {"type": "mrkdwn", "text": _t("\n".join(q))}})
    if e and ph in ("result", "live", "legend") or (e and e["state"] in ("expired", "vetoed") and ph != "betting"):
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
    if e and ph == "result" and e.get("bets_result"):
        b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _t(bets_result_text(eng, e))}]})
    b.append(leaderboard_block(snap))
    b.append({"type": "divider"})
    btns = [{"type": "button", "action_id": "kolo_spin", "style": "primary", "text": {"type": "plain_text", "text": "🎡 Točit"}}]
    if e and e["state"] in ("pending", "ready") and ph == "result":  # ready: further players may still join
        btns.append({"type": "button", "action_id": "kolo_confirm", "style": "primary", "text": {"type": "plain_text", "text": "✅ Potvrdit účast"}})
    btns.append({"type": "button", "action_id": "kolo_command", "text": {"type": "plain_text", "text": "⚡ Nabitý příkaz"}})
    btns.append({"type": "overflow", "action_id": "kolo_more", "options": [{"text": {"type": "plain_text", "text": "ℹ️ Stav"}, "value": "stav"}]})
    b.append({"type": "actions", "block_id": "kolo_actions", "elements": btns})
    b.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"OKÚ Kolo · aktualizováno {_date(now)}"}]})
    return b, ph

def leaderboard_block(snap):
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    rows = " · ".join(f"{medals[i]} {r['name']} *{_pts(r['points'])}*" for i, r in enumerate(snap.get("board", [])[:5]))
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _t(f"🏆 Žebříček OKÚ korun: {rows or '—'}")}]}

def bets_result_text(eng, e):
    name = lambda k: eng.cfg["players"].get(k, {}).get("name", k)
    parts = [f"{name(r['player'])} +{_pts(r['payout'])} 🎉" if r["state"] == "won" else f"{name(r['player'])} −{_pts(r['amount'])}"
             for r in e.get("bets_result", [])]
    return "💰 Sázky: " + " · ".join(parts)

def betting_blocks(eng, snap, rnd, now):
    """Countdown + bets so far + segment select + amount buttons (50/100/250/all-in)."""
    left = max(0, int(round(rnd["closes_at"] - now)))
    name = lambda k: eng.cfg["players"].get(k, {}).get("name", k)
    titles = {x["key"]: x["title"] for x in eng.cfg["events"]}; odds = snap.get("odds", {})
    bets = "\n".join(f"• {name(x['player'])}: {_pts(x['amount'])} na *{titles.get(x['key'], x['key'])}* (×{x['odds']})" for x in snap.get("bets", []))
    txt = f"💰 *Sázky jsou otevřené!* Kolo se roztočí za *{left} s* ({_date(rnd['closes_at'], '{time_secs}')}).\n" + (bets or "_Zatím nikdo nevsadil._")
    opts = [{"text": {"type": "plain_text", "text": _t(f"{x['title']} ×{odds.get(x['key'], 0)}", 75)}, "value": x["key"]}
            for x in eng.cfg["events"] if odds.get(x["key"], 0) > 0][:100]
    el = [{"type": "static_select", "action_id": "kolo_bet_pick", "placeholder": {"type": "plain_text", "text": "Vyber políčko"}, "options": opts}]
    el += [{"type": "button", "action_id": f"kolo_bet_{a}", "value": str(a), "text": {"type": "plain_text", "text": f"💰 {a}"}} for a in BET_AMOUNTS]
    el.append({"type": "button", "action_id": "kolo_bet_all", "value": "all", "style": "danger", "text": {"type": "plain_text", "text": "💥 All-in"}})
    return [{"type": "section", "text": {"type": "mrkdwn", "text": _t(txt)}}, {"type": "actions", "block_id": "kolo_bets", "elements": el}]

def show_blocks(eng, e, now):
    """Live show: scene pointer + hype meter, poll, 'Chyť dotaci' minigame, OKÚ quiz."""
    sh = e.get("show") or {}; out = []
    name = lambda k: eng.cfg["players"].get(k, {}).get("name", k)
    hype = sh.get("hype", 0)
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"🎭 Scéna běží v kanálu, repliky postav ve vlákně · 🔥 Hype {_show.bar(hype)} {hype}"}]})
    poll = sh.get("poll")
    if poll:
        votes = list(poll["votes"].values())
        res = " · ".join(f"{o} *{votes.count(i)}*" for i, o in enumerate(poll["options"]))
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _t(f"📊 *{poll['q']}*  {res}")}})
        out.append({"type": "actions", "block_id": "kolo_poll", "elements": [
            {"type": "button", "action_id": f"kolo_poll_{i}", "value": str(i), "text": {"type": "plain_text", "text": _t(o, 75)}} for i, o in enumerate(poll["options"])]})
    c = sh.get("catch") or {}
    if c.get("open"):
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f"💸 *Chyť dotaci!* První bere {_pts(eng.s['points_catch'])} · do {_date(c['until'], '{time_secs}')}"},
                    "accessory": {"type": "button", "action_id": "kolo_catch", "style": "primary", "text": {"type": "plain_text", "text": "💸 Chytit!"}}})
    elif c.get("winner"):
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"💸 Dotaci chytil(a) *{name(c['winner'])}* (+{_pts(eng.s['points_catch'])})"}]})
    q = sh.get("quiz") or {}
    if q.get("open"):
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _t(f"🧠 *Kvíz:* {q['q']} · správně = +{_pts(eng.s['points_quiz'])} · do {_date(q['until'], '{time_secs}')}")}})
        out.append({"type": "actions", "block_id": "kolo_quiz", "elements": [
            {"type": "button", "action_id": f"kolo_quiz_{i}", "value": str(i), "text": {"type": "plain_text", "text": _t(o, 75)}} for i, o in enumerate(q["options"])]})
    elif q and q.get("answers") and now >= q["until"]:
        ok = [name(p) for p, a in q["answers"].items() if a == q["answer"]]
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _t(f"🧠 Správně: *{q['options'][q['answer']]}* · trefili: {', '.join(ok) or 'nikdo'}")}]})
    return out

ACTIONS = {"kolo_spin": "toc", "kolo_confirm": "potvrdit", "kolo_command": "prikaz", "kolo_status": "stav"}

def action_arg(action):
    aid = action.get("action_id") or ""
    if aid == "kolo_more": return (action.get("selected_option") or {}).get("value")
    if aid == "kolo_bet_pick": return "pick " + ((action.get("selected_option") or {}).get("value") or "")
    if aid.startswith("kolo_bet_"): return "bet " + aid[len("kolo_bet_"):]
    if aid.startswith("kolo_poll_"): return "poll " + aid[len("kolo_poll_"):]
    if aid.startswith("kolo_quiz_"): return "quiz " + aid[len("kolo_quiz_"):]
    if aid == "kolo_catch": return "catch"
    return ACTIONS.get(aid)

def fallback_text(eng):
    e = eng.active_event()
    return f"OKÚ Kolo štěstí: {e['title']}" if e else "OKÚ Kolo štěstí"

class Panel:
    def __init__(self, eng, client, channel, store, clock=time.time, min_interval=1.0, refresh_s=60.0, retry_s=120.0,
                 base=IMG_BASE, card_renderer=None, backoff_cap_s=300.0, degrade_after=3, degrade_s=600.0, card_ready_s=3.0):
        self.eng, self.client, self.channel, self.store, self.clock, self.base = eng, client, channel, store, clock, base
        self.min_interval, self.refresh_s, self.retry_s = min_interval, refresh_s, retry_s
        self.backoff_cap_s, self.degrade_after, self.degrade_s, self.card_ready_s = backoff_cap_s, degrade_after, degrade_s, card_ready_s
        self.card_renderer = card_renderer  # callable(event) -> PNG bytes; None = static card URL only
        self.dirty, self.last, self.blocked_until, self.last_phase, self.last_error, self.updates = True, -1e18, 0.0, None, None, 0
        self.err_streak, self.errors, self.degraded_until, self.last_card_ref, self._pending_card_ref = 0, 0, 0.0, None, None
        self.cards, self.card_at, self.card_failed = {}, {}, set()
        self.lock = threading.Lock()

    def request(self): self.dirty = True

    def where(self):
        ch, ts = self.store.kv_get("panel_channel"), self.store.kv_get("panel_ts")
        return (ch, ts) if ch and ts else (None, None)

    def _fail(self, what, e):
        code = _err(e); now = self.clock(); self.errors += 1
        self.err_streak = self.err_streak + 1 if code == self.last_error else 1
        self.last_error = code
        if code in SOFT: wait = self.retry_s
        else: wait = min(self.backoff_cap_s, self.min_interval * 2 ** min(self.err_streak - 1, 30))
        self.blocked_until = now + wait
        meta = _meta(e)
        log.warning("panel %s failed: %s (x%d in a row, retry in %.0fs)%s", what, code, self.err_streak, wait,
                    f" slack: {meta}" if meta else "")
        if code == "invalid_blocks" and self.err_streak >= self.degrade_after and now >= self.degraded_until:
            self.degraded_until = now + self.degrade_s
            log.warning("panel degraded to text-only for %.0fs after %d invalid_blocks", self.degrade_s, self.err_streak)
        return None

    def _ok(self):
        if self.err_streak: log.info("panel recovered after %d failed attempt(s) (%s)", self.err_streak, self.last_error)
        self.err_streak, self.last_error = 0, None

    def degraded(self): return self.clock() < self.degraded_until

    def _card(self, e, ph):
        """slack_file id of the per-event card once it is safe to reference, else None (static card URL).
        B3: a file referenced right after files_upload_v2 is often not processed yet -> invalid_blocks; so the
        first render after an upload uses the static card and the slack_file switch happens card_ready_s later."""
        if ph != "result" or not e or not self.card_renderer or e["id"] in self.card_failed: return None
        if e["id"] not in self.cards:
            try:
                r = self.client.files_upload_v2(content=self.card_renderer(e), filename=f"kolo-{e['id']}.png", title=e["title"])
                f = r.get("file") or (r.get("files") or [{}])[0]
                if not f.get("id"): raise ValueError("no file id")
                self.cards[e["id"]], self.card_at[e["id"]] = f["id"], self.clock()
            except Exception as ex:
                log.warning("result card upload failed: %s (static card)", _err(ex)); self.card_failed.add(e["id"]); return None
        return self.cards[e["id"]] if self.clock() - self.card_at.get(e["id"], 0) >= self.card_ready_s else None

    def _card_due(self, e, ph):
        """True when a pending card just became referenceable but the panel still shows the static one."""
        if ph != "result" or not e or e["id"] not in self.cards or e["id"] in self.card_failed: return False
        return self.last_card_ref != self.cards[e["id"]] and self.clock() - self.card_at.get(e["id"], 0) >= self.card_ready_s

    def _render(self, now):
        e = self.eng.snapshot()["event"]; ph = phase(e, now, self.eng.round())
        card = self._card(e, ph); deg = self.degraded()
        bl = blocks(self.eng, now, self.base, None if deg else card, images=not deg)
        self._pending_card_ref = None if deg else card
        return bl, e

    def post_new(self):
        """(Re-)post the panel and remember it. Returns ts or None."""
        with self.lock:
            (bl, ph), _ = self._render(self.clock())
            try: r = self.client.chat_postMessage(channel=self.channel, text=fallback_text(self.eng), blocks=bl)
            except Exception as e: return self._fail("post", e)
            self.store.kv_set("panel_channel", r.get("channel", self.channel)); self.store.kv_set("panel_ts", r["ts"])
            self.dirty, self.last, self.last_phase, self.last_card_ref = False, self.clock(), ph, self._pending_card_ref
            self._ok(); self.updates += 1; log.info("panel posted"); return r["ts"]

    def flush(self, force=False):
        now = self.clock()
        if now < self.blocked_until and not force: return False
        ch, ts = self.where()
        if not ts: return bool(self.post_new())
        with self.lock:
            cur = self.eng.snapshot()["event"]; ph = phase(cur, now, self.eng.round())
            if ph != self.last_phase: self.dirty = True  # choreography step
            if self._card_due(cur, ph): self.dirty = True  # static card -> uploaded card
            due = self.dirty or now - self.last >= (BETTING_REFRESH_S if ph == "betting" else self.refresh_s)
            if not due or (now - self.last < self.min_interval and not force): return False
            (bl, ph), e = self._render(now)
            try: self.client.chat_update(channel=ch, ts=ts, text=fallback_text(self.eng), blocks=bl)
            except Exception as ex:
                code = _err(ex)
                if code in ("message_not_found", "cant_update_message"): self.store.kv_set("panel_ts", "")
                if code == "invalid_blocks" and e and e["id"] in self.cards and self._pending_card_ref:  # slack_file refused -> static card
                    self.card_failed.add(e["id"]); self.cards.pop(e["id"], None)
                self.dirty = True; return self._fail("update", ex)
            self.dirty, self.last, self.last_phase, self.last_card_ref = False, now, ph, self._pending_card_ref
            self._ok(); self.updates += 1; return True

    def run(self, period=0.25):
        def loop():
            while True:
                try: self.flush()
                except Exception as ex: log.error("panel loop: %s", type(ex).__name__)
                time.sleep(period)
        threading.Thread(target=loop, daemon=True, name="kolo-panel").start()
