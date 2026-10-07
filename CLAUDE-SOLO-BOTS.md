# Procedure for Claude: OKÚ "solo bots" (one Slack app per persona)

## Goal
Today one Slack app (bot user `andrej_babis`, tokens `SLACK_OKU_BOT_TOKEN` / `SLACK_OKU_APP_TOKEN`) posts as all 6 personas through `chat:write.customize` (username/icon override). Kore has decided on **solo bots**: each persona gets its own Slack app / workspace member and answers only as itself.

Personas (config key → display name): `babis` Andrej Babiš, `alenka` Alenka Hranolka, `bourak` Filip „Bourák“ Turek, `marty` Marty Prchal, `peta` Peťa Maci, `kalousek` Kalousek.

## Current architecture
- `oku_slack/bridge.py`: `Bridge` class. `handle()` strips `<@bot>`, calls `core.route()` to pick a persona by alias, generates a reply, and posts it with `username` + `icon_url` (`post()`). If `core.is_blame()` matches, it also posts a Kalousek follow-up. `main()` builds one Bolt `App` with `SLACK_OKU_BOT_TOKEN`, registers `app_mention` and DM `message` handlers (skips `bot_id` and `subtype`), and starts one `SocketModeHandler` with `SLACK_OKU_APP_TOKEN`.
- `oku_slack/core.py`: config loading, `build_prompt`, `route`, `is_blame`, `icon_url`, and the LLM backends (`gemini` is the default; it reads only the GEMINI_* names from `OKU_GEMINI_ENV_FILE`).
- `config.toml`: personas, aliases, `channel_defaults`, `icon_base_url`, and Kalousek with `blame_only` + `blame_patterns`.
- `slack-manifest.yaml`: the existing single-app manifest (includes `chat:write.customize`).
- `tests/test_oku.py`: pytest coverage of routing, blame, prompts, handle/post, and the LLM backends.
- Heimdall service `oku_slack`: `C:\code\heimdall\services.d\oku_slack.toml` runs `.venv\Scripts\python.exe -m oku_slack.bridge` in `C:\code\oku-slack`, which logs to `logs\oku_slack.log`.
- `assets/avatars/<key>.png`: 512x512 avatars, ready to upload as app icons.

## Ready-made manifests
`manifests/{babis,alenka,bourak,marty,peta,kalousek}.yaml`: Socket Mode on; bot events `app_mention`, `message.im`; scopes `app_mentions:read, chat:write, im:history, im:read, im:write, channels:history, groups:history, users:read` (`chat:write.customize` is dropped on purpose).

## Implementation plan
1. **Tokens.** Add a helper `tokens(key)` that returns `(os.environ.get(f"SLACK_OKU_{KEY}_BOT_TOKEN"), ..._APP_TOKEN)`, where KEY is the uppercased key. For `babis` only, fall back to `SLACK_OKU_BOT_TOKEN` / `SLACK_OKU_APP_TOKEN`. Never log the values. Log only the variable names or present/missing.
2. **Per-persona bridge.** Give `Bridge` a fixed `persona` key. `handle()` answers as that persona only. Do not use `route()` to pick a different speaker, and drop `username`/`icon_url` from `post()`. Babiš's app → Babiš. Kalousek's app answers when mentioned or DMed (ignore `blame_only` for direct mentions). Drop the blame follow-up from Babiš's app, or keep it optional behind a config flag that stays off by default (Kalousek is now his own member). Keep `channel_defaults` only as documentation, or remove it.
3. **History.** Mark a message as `assistant` only when `m.get("user") == own bot user id`. Other bots' messages become `user` content prefixed with their name/bot_id, so the personas can see each other in threads.
4. **Loop guard.** In both handlers, ignore events with `bot_id`, `subtype` (bot_message, message_changed…), or `user == own bot id`. Personas must never trigger each other automatically.
5. **Multi-app main().** For each persona in `config.toml`: if a token is missing, log `persona=<key> skipped: missing SLACK_OKU_<KEY>_BOT_TOKEN/APP_TOKEN` and continue. Otherwise create a Bolt `App(token=bot)`, run `auth_test()` for the user id, register the handlers, and start `SocketModeHandler(app, app_tok).connect()` (non-blocking), or run each `.start()` in its own daemon thread. Log `persona=<key> connected user=<id>`. Wrap each persona's startup in try/except so one bad token doesn't kill the others. If none connected, log an error and exit non-zero. Otherwise block forever (`threading.Event().wait()`).
6. **Tests** (`tests/test_oku.py`): update `test_handle_posts_as_persona_with_kalousek` and `test_llm_failure_fallback_and_icon` to the new semantics (no username/icon, own persona only). Add tests for: token resolution, including the babis fallback; skipping a persona with missing tokens (monkeypatch env, fake App factory); the bot-loop guard (`bot_id`/`subtype`/own user ignored); and Kalousek answering a mention. Run `.venv\Scripts\python.exe -m pytest -q`. Everything must pass.
7. Update `README.md` and the comment in `oku_slack.toml` with the new env var names. Mark `slack-manifest.yaml` as legacy.
8. Commit and push to `kore-void/oku-slack` main.
9. Run `heimdall restart oku_slack`, then check `logs\oku_slack.log`. Expect `persona=babis connected` and `skipped` lines for the personas without tokens. Confirm the process stays alive (`heimdall status oku_slack`).

## Env vars Kore sets (user env, `setx`)
```
SLACK_OKU_BABIS_BOT_TOKEN     SLACK_OKU_BABIS_APP_TOKEN     (optional; falls back to SLACK_OKU_BOT_TOKEN / SLACK_OKU_APP_TOKEN)
SLACK_OKU_ALENKA_BOT_TOKEN    SLACK_OKU_ALENKA_APP_TOKEN
SLACK_OKU_BOURAK_BOT_TOKEN    SLACK_OKU_BOURAK_APP_TOKEN
SLACK_OKU_MARTY_BOT_TOKEN     SLACK_OKU_MARTY_APP_TOKEN
SLACK_OKU_PETA_BOT_TOKEN      SLACK_OKU_PETA_APP_TOKEN
SLACK_OKU_KALOUSEK_BOT_TOKEN  SLACK_OKU_KALOUSEK_APP_TOKEN
```
BOT = `xoxb-…` (OAuth & Permissions), APP = `xapp-…` (Basic Information → App-Level Token with `connections:write`).

## Kore's manual steps (per persona)
1. Go to api.slack.com/apps → Create New App → From a manifest, and paste `manifests/<key>.yaml`.
2. Basic Information → App icon: upload `assets/avatars/<key>.png` (512x512).
3. Create an App-Level Token with scope `connections:write` (this gives the APP token).
4. Install to the workspace, then copy the Bot User OAuth Token (this gives the BOT token).
5. `setx SLACK_OKU_<KEY>_BOT_TOKEN ...` and `setx SLACK_OKU_<KEY>_APP_TOKEN ...`. Then restart Heimdall or the service so it picks up the new env.
6. Invite the bot to its channels (`/invite @<name>`).

## Acceptance criteria
- With only the legacy tokens set, the service starts, Babiš connects, and the other 5 are logged as skipped. No crash.
- Each connected app answers mentions/DMs only as its own persona, with no username/icon override.
- Bot messages never trigger replies (no loops between personas).
- All tests pass, and the commit is pushed.

## Rules
- Never print, log, or commit secrets (tokens, Gemini keys). Mention env var names only.
- No `Co-authored-by: Claude` (or any AI) trailer in commits.
- Do not post to Slack while implementing or verifying. Verify through logs only.
