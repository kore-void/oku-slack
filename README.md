# oku-slack
Always-on Slack bridge for the OKÚ satire team (parody personas), supervised by Heimdall (`services.d\oku_slack.toml`).
One Slack app (Socket Mode) posts as each persona via `chat:write.customize` (username + optional icon_url).

Routing: @mention or DM -> first persona alias found in text, else `channel_defaults[channel]`, else `default_persona` (Babiš).
Kalousek is blame-only: if the message blames him (alias + blame pattern), he adds one follow-up line.

Secrets (user env, never in repo): `SLACK_OKU_BOT_TOKEN`, `SLACK_OKU_APP_TOKEN`, `LLM_API_KEY`.
Optional env: `LLM_BASE_URL` (https://api.x.ai/v1), `LLM_MODEL` (grok-4), `OKU_CONFIG`, `OKU_PERSONA_DIR`.

Run: `.venv\Scripts\python.exe -m oku_slack.bridge`   Tests: `.venv\Scripts\python.exe -m pytest -q`
Avatars in `assets/avatars`; set `icon_base_url` only once they are hosted at a public https URL.
