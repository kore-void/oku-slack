"""Charged-command effect registry. players.toml: players.<p>.command_effects = ["veto_respin", ...],
players.<p>.command_persona = "<persona key>" (who speaks for persona effects).
Each effect: fn(eng, player, ctx) -> {"ok": bool, "text": Czech summary for the panel sequence}.
Effects may mutate state through the engine and queue notifications with eng.emit(kind, obj).
A command is accepted when at least one of its effects applies; otherwise the cooldown is NOT spent."""

def _name(eng, p): return eng.cfg["players"].get(p, {}).get("name", p)

def veto_respin(eng, p, ctx):
    """Cancel the current (not yet live) result and spin again immediately (no betting window)."""
    e = eng.active_event()
    if not e or e["state"] == "live": return {"ok": False, "text": "Není co vetovat."}
    e["state"] = "vetoed"; e["vetoed_by"] = p
    if e.get("round_id") and not e.get("revealed", True): eng.eco.refund(e["round_id"], "veto: vrácená sázka")
    eng.store.put_event(e)
    new = eng.spin(p, respin_of=e["id"])
    eng.emit("spin", new)
    return {"ok": True, "text": f"🙅 Veto! *{e['title']}* padá, kolo se točí znovu.", "respin": new["id"]}

def steal_points(eng, p, ctx):
    """Steal settings.steal_pct % of the leader's points (the richest other player if p leads)."""
    others = [r for r in eng.eco.board(limit=99) if r["player"] != p]
    if not others: return {"ok": False, "text": "Není komu brát."}
    leader = eng.eco.leader(); victim = leader if leader != p else others[0]["player"]
    if eng.shielded(victim):
        txt = f"🛡️ {_name(eng, victim)} má štít, krádež se odrazila."
        eng.emit("persona_line", {"persona": ctx.get("persona") or "kalousek", "kind": "steal_blocked",
                                  "cue": f"Chtěl jsi vzít body hráči {_name(eng, victim)}, ale měl štít. Jedna kousavá věta.",
                                  "fallback": "Štít? To je typické. Za tohle taky můžu já, že jo."})
        return {"ok": True, "text": txt}
    amt = int(eng.eco.balance(victim) * float(eng.s["steal_pct"]) / 100)
    if amt <= 0: return {"ok": False, "text": f"{_name(eng, victim)} nemá co vzít."}
    eng.eco.transfer(victim, p, amt, "krádež: " + ctx.get("label", ""))
    eng.emit("persona_line", {"persona": ctx.get("persona") or "kalousek", "kind": "steal",
                              "cue": f"Hráč {_name(eng, p)} právě ukradl {amt} OKÚ korun hráči {_name(eng, victim)}. Jedna jedovatá věta, kdo za to může.",
                              "fallback": f"{_name(eng, victim)} přišel o {amt} korun. A kdo za to může? No já ne."})
    return {"ok": True, "text": f"🕵️ {_name(eng, p)} bere {amt} OK hráči {_name(eng, victim)}."}

def double_bet(eng, p, ctx):
    """The player's winning bets in the next settled round pay double."""
    eng.store.kv_set(f"double:{p}", "1")
    return {"ok": True, "text": f"✖️2 {_name(eng, p)}: příští výhra v sázce dvojnásobná."}

def persona_interrupt(eng, p, ctx):
    """A persona interrupts the running scene (or speaks under the panel when nothing is live)."""
    who = ctx.get("persona") or "babis"; e = eng.active_event()
    live = bool(e and e["state"] == "live")
    eng.emit("persona_line", {"persona": who, "kind": "interrupt", "event_id": e["id"] if live else None,
                              "cue": (f"Skoč všem do řeči uprostřed události {e['title']}. Hráč {_name(eng, p)} právě použil nabitý příkaz {ctx.get('label', '')}."
                                      if live else f"Hráč {_name(eng, p)} právě použil nabitý příkaz {ctx.get('label', '')}. Krátce a rázně to okomentuj."),
                              "fallback": "Počkejte, počkejte! Tohle je kampaň. Já chci vidět čísla!"})
    return {"ok": True, "text": f"📣 {eng.persona_name(who)} skáče do řeči."}

def shield(eng, p, ctx):
    until = eng.clock() + float(eng.s["shield_s"])
    eng.store.kv_set(f"shield:{p}", str(until))
    return {"ok": True, "text": f"🛡️ {_name(eng, p)} má štít proti krádeži na {int(eng.s['shield_s'] // 60)} min."}

REGISTRY = {"veto_respin": veto_respin, "steal_points": steal_points, "double_bet": double_bet,
            "persona_interrupt": persona_interrupt, "shield": shield}
