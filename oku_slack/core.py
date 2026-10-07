"""Pure logic: config, persona prompts, routing, LLM call. No Slack imports (unit-testable)."""
import json, os, re, pathlib, tomllib, logging, requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
log = logging.getLogger("oku")
PARODY = ("Jsi satirická PARODIE veřejné osoby, ne skutečná osoba. Odpovídej česky, stručně, ve stylu Slacku. "
          "Nepiš se jako jiná postava týmu OKÚ.")

def load_config(path=None):
    with open(path or os.environ.get("OKU_CONFIG", ROOT / "config.toml"), "rb") as f:
        return tomllib.load(f)

def build_prompt(p, persona_root=None):
    d = pathlib.Path(persona_root or os.environ.get("OKU_PERSONA_DIR", ROOT / "persona")) / p["dir"]
    parts = []
    try: parts.append(json.loads((d / "profile.json").read_text(encoding="utf-8"))["description"])
    except Exception as e: log.warning("profile unreadable for %s: %s", p["dir"], type(e).__name__)
    for f in p.get("files", []):
        fp = d / f
        if fp.exists(): parts.append(fp.read_text(encoding="utf-8"))
    parts.append(PARODY)
    return "\n\n".join(parts)

def _norm(s): return (s or "").lower()

def _mentions(text, alias):
    return re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text) is not None

def is_blame(cfg, text):
    t = _norm(text); k = cfg["personas"].get("kalousek")
    if not k or not any(_mentions(t, a) for a in k["aliases"]): return False
    return any(b in t for b in k.get("blame_patterns", []))

def route(cfg, text, channel=None):
    """Return (persona_key, kalousek_followup: bool)."""
    t = _norm(text); best = None
    for key, p in cfg["personas"].items():
        if p.get("blame_only"): continue
        for a in p["aliases"]:
            m = re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", t)
            if m and (best is None or m.start() < best[0]): best = (m.start(), key)
    key = best[1] if best else cfg.get("channel_defaults", {}).get(channel) or cfg["default_persona"]
    return key, is_blame(cfg, text)

def icon_url(cfg, p):
    base = cfg.get("icon_base_url", "").strip()
    return f"{base.rstrip('/')}/{p['avatar']}" if base and p.get("avatar") else None

def llm(system, history, post=requests.post):
    r = post(os.environ.get("LLM_BASE_URL", "https://api.x.ai/v1").rstrip("/") + "/chat/completions",
             headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
             json={"model": os.environ.get("LLM_MODEL", "grok-4"),
                   "messages": [{"role": "system", "content": system}] + history}, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
