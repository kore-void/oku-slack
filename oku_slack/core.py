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


# --- Gemini backend (mirrors Umbra: GEMINI_API_KEY / GEMINI_API_KEYS rotation, GEMINI_MODEL) ---
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
_GEMINI_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GEMINI_MODEL")

def _env_file_values(path):
    """Read ONLY the Gemini names from an env file (e.g. Umbra's bot/.env). Values never logged."""
    out = {}
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8-sig").splitlines():
            k, sep, v = line.partition("=")
            k = k.strip()
            if sep and k in _GEMINI_NAMES: out[k] = v.strip().strip('"').strip("'")
    except OSError as e:
        log.warning("gemini env file unreadable: %s", type(e).__name__)
    return out

def gemini_settings():
    vals = _env_file_values(os.environ["OKU_GEMINI_ENV_FILE"]) if os.environ.get("OKU_GEMINI_ENV_FILE") else {}
    get = lambda k: os.environ.get(k) or vals.get(k, "")
    raw = get("GEMINI_API_KEYS") or get("GEMINI_API_KEY")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    model = os.environ.get("LLM_MODEL") or get("GEMINI_MODEL") or "gemini-2.5-flash"
    return keys, list(dict.fromkeys([model] + GEMINI_FALLBACK_MODELS))

def gemini(system, history, post=requests.post):
    """Model-major, key-minor (like Umbra): try best model on every key before degrading."""
    keys, models = gemini_settings()
    if not keys: raise RuntimeError("no GEMINI_API_KEY(S)")
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"] or "…"}]} for m in history]
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents or [{"role": "user", "parts": [{"text": "…"}]}]}
    last = None
    for model in models:
        for i, key in enumerate(keys):
            r = post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": key}, json=body, timeout=90)
            if r.status_code in (429, 500, 503, 404):
                log.warning("gemini %s key#%d -> %s", model, i, r.status_code); last = r.status_code
                if r.status_code == 404: break
                continue
            r.raise_for_status()
            parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text: return text
            last = "empty"
    raise RuntimeError(f"gemini exhausted ({last})")

def generate(system, history):
    backend = os.environ.get("LLM_BACKEND", "gemini")
    return gemini(system, history) if backend == "gemini" else llm(system, history)
