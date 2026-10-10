"""OKÚ korun: points, bets and leaderboard. Pure logic on top of Store (ledger + bets tables).
Balance = settings.start_points + sum(ledger deltas); every change is an auditable ledger row.
Bets are placed into an open betting round (escrow: the stake is deducted immediately) and settled
when the wheel result is revealed. Odds are derived from segment weights: total/weight * (1 - house_edge)."""
import uuid

class EconomyError(Exception):
    def __init__(self, code, msg=""): super().__init__(msg or code); self.code = code

class Economy:
    def __init__(self, cfg, store, clock):
        self.cfg, self.s, self.store, self.clock = cfg, cfg["settings"], store, clock

    # ---------- balances ----------
    def balance(self, p): return int(self.s["start_points"]) + self.store.ledger_sum(p)

    def add(self, p, delta, reason, ref=""):
        if delta: self.store.add_ledger(self.clock(), p, int(delta), reason, ref)
        return self.balance(p)

    def transfer(self, src, dst, amount, reason, ref=""):
        amount = max(0, min(int(amount), self.balance(src)))
        if amount:
            self.add(src, -amount, reason, ref); self.add(dst, amount, reason, ref)
        return amount

    def board(self, limit=5):
        rows = [{"player": k, "name": v["name"], "points": self.balance(k)} for k, v in self.cfg["players"].items()]
        rows.sort(key=lambda r: (-r["points"], r["name"]))
        return rows[:limit]

    def leader(self):
        b = self.board(limit=len(self.cfg["players"]))
        return b[0]["player"] if b else None

    # ---------- odds ----------
    def odds(self, key):
        evs = self.cfg["events"]; total = sum(float(e["weight"]) for e in evs)
        w = next((float(e["weight"]) for e in evs if e["key"] == key), 0.0)
        if w <= 0: return 0.0
        return max(float(self.s["min_odds"]), round(total / w * (1 - float(self.s["house_edge"])), 1))

    def all_odds(self): return {e["key"]: self.odds(e["key"]) for e in self.cfg["events"]}

    # ---------- bets ----------
    def place(self, rnd, p, key, amount):
        """amount: int or 'all'. Raises EconomyError with a Czech message."""
        if not rnd or rnd.get("state") != "open" or self.clock() >= rnd["closes_at"]:
            raise EconomyError("no_round", "Sázky teď nejsou otevřené. Zmáčkni 🎡 Točit.")
        if key not in {e["key"] for e in self.cfg["events"]}: raise EconomyError("bad_segment", "Tohle políčko na kole není.")
        bal = self.balance(p)
        amt = bal if amount == "all" else int(amount)
        if amt < int(self.s["min_bet"]): raise EconomyError("too_small", f"Minimální sázka je {self.s['min_bet']} 🪙. Máš {bal} 🪙.")
        if amt > bal: raise EconomyError("too_much", f"Na tohle nemáš: zůstatek {bal} 🪙.")
        b = {"id": uuid.uuid4().hex[:8], "round_id": rnd["id"], "player": p, "key": key, "amount": amt,
             "odds": self.odds(key), "at": self.clock(), "state": "open", "payout": 0}
        self.store.put_bet(b); self.add(p, -amt, "sázka", b["id"])
        return b

    def settle(self, round_id, key, multipliers=None):
        """Pay out winning bets of a round. multipliers: {player: x} (e.g. double_bet). Returns result rows."""
        out = []
        for b in self.store.bets(round_id):
            if b["state"] != "open": continue
            if b["key"] == key:
                m = (multipliers or {}).get(b["player"], 1)
                b["payout"] = int(round(b["amount"] * b["odds"] * m)); b["state"] = "won"
                self.add(b["player"], b["payout"], "výhra v sázce", b["id"])
            else:
                b["state"] = "lost"
            self.store.put_bet(b); out.append(b)
        return out

    def refund(self, round_id, reason="vrácená sázka"):
        n = 0
        for b in self.store.bets(round_id):
            if b["state"] != "open": continue
            b["state"] = "refunded"; self.store.put_bet(b); self.add(b["player"], b["amount"], reason, b["id"]); n += 1
        return n
