"""Wheel -> world adapter (plan 2.2): the wheel is ONE optional event source at the edge of the world.
It no longer holds or renders world state. Each diary-worthy action is appended as a draft line to
logs/outbox/wheel.jsonl (source=wheel, payload.via = engine | slack | room, dedupe_key = wheel:<uuid>,
subject namespaced wheel:<subject>). The oku_world service tails that file; if it is stopped, nothing is lost and the
wheel does not care. record() never blocks on the network and never raises into the 0.5 s tick.
Without a path (tests) rows are only kept in memory (`events()`), so tests never touch real logs."""
import collections, json, logging, pathlib, threading, uuid
from ..world import log as world_log

log = logging.getLogger("oku_wheel.world_client")

class WorldClient:
    def __init__(self, path=None, clock=None, regime="A_scarce", run_id="oku-world-1", keep=2000):
        import time
        self.path = pathlib.Path(path) if path else None
        self.clock, self.regime, self.run_id = clock or time.time, regime, run_id
        self.recent = collections.deque(maxlen=keep); self.lock = threading.Lock(); self.failures = 0

    def record(self, type, actor=None, subject=None, payload=None, source=None, ts=None, **_):
        """Append one draft. Returns the draft dict or None on failure (logged, never raised)."""
        try:
            pl = dict(payload or {}); pl.setdefault("via", source or world_log.current_source())
            subj = subject if (subject is None or subject.startswith("wheel:")) else "wheel:" + subject
            row = {"type": type, "source": "wheel", "actor": actor, "subject": subj, "payload": pl,
                   "ts": float(self.clock() if ts is None else ts), "dedupe_key": "wheel:" + uuid.uuid4().hex,
                   "regime": self.regime, "run_id": self.run_id}
            line = json.dumps(row, ensure_ascii=False) + "\n"
            with self.lock:
                self.recent.append(row)
                if self.path:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as f: f.write(line)
            return row
        except Exception as e:
            self.failures += 1; log.warning("world outbox %s failed: %s", type, e.__class__.__name__); return None

    def events(self):
        """Rows recorded by this process (newest last; bounded). For tests/diagnostics only: the world owns the diary."""
        with self.lock: return list(self.recent)
