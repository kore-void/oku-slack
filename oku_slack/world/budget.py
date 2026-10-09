"""Autonomy guard rails (plan 1.7, decision D2): daily/per-channel post caps, chatter caps, LLM call cap, quiet
hours (Europe/Prague), "never while a wheel event is live", kill switch and dry run. Pure functions over diary rows:
the budget is a PROJECTION of what the world already did (no separate counter that could drift).

What counts:
- top-level post: `agent.posted` (payload.top_level != false), `porada.requested`, `chatter.requested` and
  `podnet.requested` (the bridge posts the opener / the podnet repost top-level);
- chatter exchange: `chatter.requested` / `chatter.started`, counted once per subject (`chatter:<slot>`), so the
  request and its later ack never count twice; turns of one exchange are capped by chatter_max_turns;
- LLM calls charged to the world: sum of payload.llm_calls on rows from world-owned sources (agent, director,
  schedule, chatter, world). The porada charges its estimate (porada_llm_estimate) when it is requested.
Dry-run rows (`*.dry_run`) never consume budget."""
import os, pathlib
from . import tz

DEFAULTS = {
    "dry_run": True, "kill_switch": False, "timezone": tz.PRAGUE, "quiet_hours": ["22:00", "08:00"],
    "posts_per_day": 6, "per_channel_gap_h": 3.0, "chatter_threads_per_day": 2, "chatter_max_turns": 4,
    "llm_calls_per_day": 40, "no_posts_while_wheel_live": True,
}
POST_TYPES = ("agent.posted", "porada.requested", "chatter.requested", "podnet.requested")
CHATTER_TYPES = ("chatter.requested", "chatter.started")
WORLD_SOURCES = ("agent", "director", "schedule", "chatter", "world")

def settings(raw=None):
    s = dict(DEFAULTS); s.update({k: v for k, v in (raw or {}).items() if v is not None}); return s

def is_quiet(now, quiet_hours=("22:00", "08:00"), zone=tz.PRAGUE, force_fallback=False):
    """True inside the quiet window [start, end) in local time; windows may wrap midnight. Empty/equal = never."""
    if not quiet_hours or len(quiet_hours) != 2: return False
    a, b = tz.hm(quiet_hours[0]), tz.hm(quiet_hours[1])
    if a == b: return False
    m = tz.minutes(now, zone, force_fallback)
    return a <= m < b if a < b else (m >= a or m < b)

def killed(cfg, logs_dir=None, kv_get=None, now=0.0):
    """Kill switch: config kill_switch=true, file <logs>/world.kill, or kv world:silenced_until in the future
    (for the later `/oku ticho`). Returns the reason or None."""
    if cfg.get("kill_switch"): return "kill_switch:config"
    if os.environ.get("OKU_WORLD_KILL") == "1": return "kill_switch:env"
    if logs_dir and (pathlib.Path(logs_dir) / "world.kill").exists(): return "kill_switch:file"
    if kv_get:
        try:
            v = kv_get("world:silenced_until")
            if v and float(v) > now: return "kill_switch:silenced"
        except (TypeError, ValueError): pass
    return None

def usage(rows, now, cfg):
    """Budget usage from diary rows (pass at least today's rows plus the per-channel gap window)."""
    zone = cfg.get("timezone", tz.PRAGUE)
    day0 = tz.day_start(now, zone)
    u = {"day_start": day0, "posts_today": 0, "chatter_today": 0, "llm_today": 0, "channel_last": {}}
    chats = set()
    for i, r in enumerate(rows):
        t, ts, pl = r["type"], r["ts"], r.get("payload") or {}
        if t.endswith(".dry_run"): continue
        if t in POST_TYPES and pl.get("top_level", True):
            ch = pl.get("channel")
            if ch: u["channel_last"][ch] = max(u["channel_last"].get(ch, 0), ts)
            if ts >= day0: u["posts_today"] += 1
        if ts < day0: continue
        if t in CHATTER_TYPES: chats.add(r.get("subject") or f"row:{i}")
        if r.get("source") in WORLD_SOURCES: u["llm_today"] += int(pl.get("llm_calls", 0) or 0)
    u["chatter_today"] = len(chats)
    return u

def check(action, rows, now, cfg, channel=None, llm=0, turns=0, wheel_live=False, kill=None):
    """Decide whether an autonomous action may happen now. action: 'post' | 'chatter' | 'chatter_turn' | 'llm'.
    Returns {"ok": bool, "reasons": [denial reasons], "why": [passed checks], "usage": {...}}. Pure."""
    c = settings(cfg); u = usage(rows, now, c); deny, why = [], []
    if kill: deny.append(kill)
    if is_quiet(now, c.get("quiet_hours"), c.get("timezone", tz.PRAGUE)): deny.append("quiet_hours")
    else: why.append("not quiet hours")
    if wheel_live and c.get("no_posts_while_wheel_live", True) and action in ("post", "chatter", "chatter_turn"):
        deny.append("wheel_live")
    if action in ("post", "chatter"):
        if u["posts_today"] >= int(c["posts_per_day"]): deny.append(f"posts_per_day {u['posts_today']}/{c['posts_per_day']}")
        else: why.append(f"posts {u['posts_today']}/{c['posts_per_day']} today")
        if channel:
            last = u["channel_last"].get(channel)
            gap = float(c["per_channel_gap_h"]) * 3600
            if last is not None and now - last < gap: deny.append(f"per_channel_gap {channel} {int((now - last) // 60)} min < {c['per_channel_gap_h']} h")
            else: why.append(f"{channel} gap ok")
    if action == "chatter":
        if u["chatter_today"] >= int(c["chatter_threads_per_day"]): deny.append(f"chatter_threads_per_day {u['chatter_today']}/{c['chatter_threads_per_day']}")
        if turns and turns > int(c["chatter_max_turns"]): deny.append(f"chatter_max_turns {turns}>{c['chatter_max_turns']}")
    if action == "chatter_turn" and turns > int(c["chatter_max_turns"]): deny.append(f"chatter_max_turns {turns}>{c['chatter_max_turns']}")
    if llm:
        if u["llm_today"] + int(llm) > int(c["llm_calls_per_day"]): deny.append(f"llm_calls_per_day {u['llm_today']}+{llm}>{c['llm_calls_per_day']}")
        else: why.append(f"llm {u['llm_today']}+{llm}/{c['llm_calls_per_day']}")
    return {"ok": not deny, "reasons": deny, "why": why, "usage": u}
