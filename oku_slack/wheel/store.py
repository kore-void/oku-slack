"""SQLite persistence: events, cooldowns, sequences, chat. JSON blobs keep the schema small."""
import json, sqlite3, threading

SCHEMA = """
create table if not exists events(id text primary key, data text not null);
create table if not exists cooldowns(player text primary key, last_used real not null);
create table if not exists sequences(id text primary key, data text not null);
create table if not exists kv(k text primary key, v text);
create table if not exists chat(id integer primary key autoincrement, ts real, player text, text text);
"""

class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock: self.db.executescript(SCHEMA); self.db.commit()

    def _put(self, table, obj):
        with self.lock:
            self.db.execute(f"insert or replace into {table}(id,data) values(?,?)", (obj["id"], json.dumps(obj)))
            self.db.commit()

    def _all(self, table):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(f"select data from {table} order by rowid")]

    def put_event(self, e): self._put("events", e)
    def events(self): return self._all("events")
    def put_sequence(self, s): self._put("sequences", s)
    def sequences(self): return self._all("sequences")

    def last_used(self, player):
        with self.lock:
            r = self.db.execute("select last_used from cooldowns where player=?", (player,)).fetchone()
        return r[0] if r else None

    def set_used(self, player, ts):
        with self.lock:
            self.db.execute("insert or replace into cooldowns(player,last_used) values(?,?)", (player, ts)); self.db.commit()

    def add_chat(self, ts, player, text):
        with self.lock:
            self.db.execute("insert into chat(ts,player,text) values(?,?,?)", (ts, player, text)); self.db.commit()

    def chat(self, limit=50):
        with self.lock:
            rows = self.db.execute("select ts,player,text from chat order by id desc limit ?", (limit,)).fetchall()
        return [{"ts": a, "player": b, "text": c} for a, b, c in reversed(rows)]

    def kv_get(self, k):
        with self.lock:
            r = self.db.execute("select v from kv where k=?", (k,)).fetchone()
        return r[0] if r and r[0] else None

    def kv_set(self, k, v):
        with self.lock:
            self.db.execute("insert or replace into kv(k,v) values(?,?)", (k, v)); self.db.commit()
