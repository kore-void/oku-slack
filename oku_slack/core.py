"""Pure logic: config, persona prompts, Slack token lookup, LLM call. No Slack imports (unit-testable)."""
import json, os, pathlib, tomllib, logging, requests

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

# --- Slack tokens: one Slack app per persona (solo bots) ---
LEGACY_TOKENS = ("SLACK_OKU_BOT_TOKEN", "SLACK_OKU_APP_TOKEN")  # the original single app, now Babiš
LEGACY_PERSONA = "babis"

def token_names(key):
    k = key.upper(); return f"SLACK_OKU_{k}_BOT_TOKEN", f"SLACK_OKU_{k}_APP_TOKEN"

def _user_env(name):
    """Windows: `setx` writes HKCU\\Environment, which already-running processes (Heimdall) don't see. Read it directly."""
    if os.name != "nt": return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: return winreg.QueryValueEx(k, name)[0] or None
    except OSError: return None

def env(name, user_env=_user_env):
    return os.environ.get(name) or user_env(name)

def tokens(key, get=env):
    """Return (bot_token, app_token, (bot_name, app_name)). Babiš falls back to the legacy names. Never log the values."""
    names = token_names(key); bot, app = get(names[0]), get(names[1])
    if key == LEGACY_PERSONA and not bot and not app:
        names = LEGACY_TOKENS; bot, app = get(names[0]), get(names[1])
    return bot, app, names

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
