import os, pathlib, tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULTS = dict(cooldown_s=1800, sequence_s=120, alarm_before_s=300, lead_s=600, confirm_wait_s=1800,
                hold_min_s=2.0, spin_ms=6000, slack_channel="")

def load(path=None):
    path = pathlib.Path(path or os.environ.get("OKU_WHEEL_CONFIG") or ROOT / "players.toml")
    with open(path, "rb") as f: raw = tomllib.load(f)
    s = dict(DEFAULTS, **raw.get("settings", {}))
    players = raw.get("players", {})
    # Untracked local secrets (gitignored): real room_keys etc. override the committed placeholders.
    local = pathlib.Path(os.environ.get("OKU_WHEEL_LOCAL") or path.with_name("players.local.toml"))
    if local.exists():
        with open(local, "rb") as f: loc = tomllib.load(f)
        for k, v in loc.get("players", {}).items():
            if k in players: players[k].update(v)
    events = raw.get("events", [])
    if not players: raise ValueError("players.toml: no players")
    if not events: raise ValueError("players.toml: no events")
    for e in events: e.setdefault("weight", 1); e.setdefault("duration_s", 600); e.setdefault("music_url", "")
    s.setdefault("allow_force", False); s.setdefault("nag_before_s", 120)
    here = pathlib.Path(__file__).parent
    with open(here / "host.toml", "rb") as f: host = tomllib.load(f)
    scripts = {}
    for e in events:
        if e.get("script"):
            with open(here / "events" / f"{e['script']}.toml", "rb") as f: scripts[e["script"]] = tomllib.load(f)
            e["duration_s"] = scripts[e["script"]].get("duration_s", e["duration_s"])
    return {"settings": s, "players": players, "events": events, "host": host, "scripts": scripts}
