"""One-off 'awaiting reply' follow-up for a single anchor post (config [capak_followup]).
- one status message, edited in place (chat.update) every `interval` s with funny lines
- reminders as new messages at anchor+30/+45 min (skipped if already past by > grace)
- cancelled when the target user posts in the channel; hard stop at anchor+45 min
State persisted in logs/<state_file> so a restart never resends."""
import json, threading, time, pathlib, logging
log = logging.getLogger("oku")

LINES = ["⏳", "⏳⏳", "⏳⏳⏳", "Babiš si hraje s kalkulačkou 🧮", "Babiš si vaří kafe ☕",
         "Babiš počítá dotace na čekání 💶", "Babiš volá do Agrofertu, ať pošlou posily 📞",
         "Babiš píše novou knihu: Čekání, jak jsem ho nezavinil 📖", "Babiš už má nachystanou tiskovku 🎤",
         "Babiš už to vzdává 😤"]
FINAL = "Babiš: tak nic. Já jsem pracoval, ICIK ne. To je celý. 🤷"
DONE = "Babiš: no konečně! 🎉"

class Followup:
    def __init__(self, client, cfg, state_path, now=time.time):
        self.c, self.cfg, self.path, self.now = client, cfg, pathlib.Path(state_path), now
        self.ch, self.user, self.anchor = cfg["channel"], cfg["user"], float(cfg["anchor_ts"])
        self.interval = cfg.get("interval_s", 120); self.stop_at = self.anchor + cfg.get("hard_stop_min", 45) * 60
        self.grace = cfg.get("grace_s", 300)
        self.reminders = cfg.get("reminders") or [
            {"at_min": 30, "text": "<@{u}> ICIKu, já tady nebudu čekat věčně!"},
            {"at_min": 45, "text": "<@{u}> Kampaň proti mně pokračuje, už mám nachystanýho druhýho!"}]
        self.lock = threading.Lock(); self.state = self._load()

    def _load(self):
        try: return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception: return {"status_ts": None, "idx": 0, "last_edit": 0, "sent": [], "done": None}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp"); tmp.write_text(json.dumps(self.state), encoding="utf-8"); tmp.replace(self.path)

    def _edit(self, text):
        if self.state["status_ts"]:
            try: self.c.chat_update(channel=self.ch, ts=self.state["status_ts"], text=text)
            except Exception as e: log.warning("followup edit failed: %s", type(e).__name__)

    def _finish(self, reason, text):
        self.state["done"] = reason; self._save(); self._edit(text); log.info("followup done: %s", reason)

    def tick(self):
        with self.lock:
            s = self.state; t = self.now()
            if s["done"]: return False
            if not s["status_ts"]:
                if t >= self.stop_at: s["done"] = "expired_before_start"; self._save(); return False
                s["status_ts"] = "pending"; self._save()  # never double-post, even if the call below crashes
                try: s["status_ts"] = self.c.chat_postMessage(channel=self.ch, text=LINES[0])["ts"]
                except Exception as e:
                    s["fails"] = s.get("fails", 0) + 1
                    if s["fails"] < 3: s["status_ts"] = None  # nothing was posted: retry next tick
                    log.warning("followup status post failed: %s %s", type(e).__name__, getattr(getattr(e, "response", None), "data", {}).get("error") if hasattr(e, "response") else e)
                s["last_edit"] = t; s["idx"] = 1; self._save()
            for i, r in enumerate(self.reminders):
                due = self.anchor + r["at_min"] * 60
                if i in s["sent"] or t < due: continue
                s["sent"].append(i); self._save()
                if t - due <= self.grace:
                    try: self.c.chat_postMessage(channel=self.ch, text=r["text"].format(u=self.user))
                    except Exception as e: log.warning("followup reminder failed: %s", type(e).__name__)
            if t >= self.stop_at: self._finish("timeout", FINAL); return False
            if t - s["last_edit"] >= self.interval and s["status_ts"] != "pending":
                self._edit(LINES[s["idx"] % len(LINES)]); s["idx"] += 1; s["last_edit"] = t; self._save()
            return True

    def on_message(self, event):
        if event.get("channel") != self.ch or event.get("user") != self.user or event.get("bot_id"): return False
        if event.get("subtype") not in (None, "thread_broadcast", "file_share"): return False
        with self.lock:
            if self.state["done"]: return False
            self._finish("replied", DONE); return True

    def run(self, every=10, sleep=time.sleep):
        while self.tick(): sleep(every)

def start(client, cfg, logs_dir):
    fc = cfg.get("capak_followup")
    if not fc or not fc.get("enabled", True): return None
    f = Followup(client, fc, pathlib.Path(logs_dir) / fc.get("state_file", "capak_followup.json"))
    if f.state["done"]: return f
    threading.Thread(target=f.run, daemon=True).start(); return f
