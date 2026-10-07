# oku-slack
Always-on Slack bridge for the OKÚ satire team (parody personas), supervised by Heimdall (`services.d\oku_slack.toml`).

**Solo bots:** every persona is its own Slack app / workspace member (own name, avatar, DMs). One process connects all of them
over Socket Mode. Each app answers only its own @mentions and DMs, always as its own persona (no per-channel default, no
username/icon override). Messages from bots (including the other personas), edits and the app's own posts never trigger a reply.

Personas (`config.toml` key): `babis`, `alenka`, `bourak`, `marty`, `peta`, `kalousek` (Kalousek's prompt keeps him silent unless blamed).

Secrets (user env, `setx`), per persona `<KEY>` = uppercased key:
`SLACK_OKU_<KEY>_BOT_TOKEN` (xoxb-) and `SLACK_OKU_<KEY>_APP_TOKEN` (xapp-, `connections:write`).
Babiš keeps the original app and falls back to `SLACK_OKU_BOT_TOKEN` / `SLACK_OKU_APP_TOKEN`.
A persona with a missing token is skipped with a warning in `logs\oku_slack.log`; the rest keep running.
On Windows, tokens that are missing from the process env are also read from the user registry (`HKCU\Environment`, where `setx` writes).

LLM: LLM_BACKEND=gemini (default) uses GEMINI_API_KEY(S)/GEMINI_MODEL from env or OKU_GEMINI_ENV_FILE (Umbra bot\.env, only those names read); LLM_BACKEND=openai uses LLM_API_KEY/LLM_BASE_URL/LLM_MODEL.
Optional env: `LLM_BASE_URL` (https://api.x.ai/v1), `LLM_MODEL` (grok-4), `OKU_CONFIG`, `OKU_PERSONA_DIR`.

Run: `.venv\Scripts\python.exe -m oku_slack.bridge`   Tests: `.venv\Scripts\python.exe -m pytest -q`
Slack app manifests: `manifests\<key>.yaml`; app icons: `assets\avatars\<key>.png`. Setup steps: `CLAUDE-SOLO-BOTS.md`.
