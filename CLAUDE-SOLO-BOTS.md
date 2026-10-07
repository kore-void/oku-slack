# OKÚ "solo bots" (one Slack app per persona)

## Goal
Originally one Slack app (bot user `andrej_babis`, tokens `SLACK_OKU_BOT_TOKEN` / `SLACK_OKU_APP_TOKEN`) posted as all 6 personas through `chat:write.customize` (username/icon override), and a per-channel default picked the speaker (so `@Babiš` in #oku-socky answered as Marty). Kore decided on **solo bots**: each persona is its own Slack app / workspace member and answers only as itself. Babiš keeps the existing app and tokens.

Personas (config key → display name): `babis` Andrej Babiš, `alenka` Alenka Hranolka, `bourak` Filip „Bourák“ Turek, `marty` Marty Prchal, `peta` Peťa Maci, `kalousek` Kalousek.

## How it works now (implemented)
- `oku_slack/bridge.py`
  - `start(cfg)`: for each persona in `config.toml`, look up its tokens. A persona with a missing token is logged as `persona=<key> skipped: missing <VAR names>` and skipped. Otherwise it creates a Bolt `App`, runs `auth.test` for the bot user id, registers handlers, and connects a `SocketModeHandler` (non-blocking). Each persona starts inside its own try/except (`persona=<key> failed to start: BoltError(invalid_auth)` etc.), so one bad token doesn't stop the others. If two personas resolve to the same Slack bot user (the same xoxb pasted twice), the second one is skipped, so a mention never gets two answers.
  - `main()`: if no persona connected, it logs an error and exits 1. Otherwise it logs `running personas=...` and blocks.
  - `Bridge(client, cfg, key, bot_user)`: a fixed persona. `handle()` answers in the thread as that persona only, with a plain `chat.postMessage` (no username/icon override).
  - `wants(event, bot_user)`, the loop guard used by both handlers (`app_mention`, DM `message`): ignore events with `bot_id`, any `subtype`, or `user == own bot user`. Personas never trigger each other. If a human mentions two personas in one message, each app answers once, as itself.
  - History (threads): only this app's own messages are `assistant`. Messages from other bots become `user` content prefixed `[Name]`. Mentions of sibling personas (`<@U…>`) are rewritten to `@Name`.
- `oku_slack/core.py`: `token_names(key)`, `tokens(key)` (Babiš falls back to the legacy names), and `env(name)`, which reads the process env and then, on Windows only, `HKCU\Environment`. That registry key is where `setx` writes, so a newly set token is picked up by restarting just the service, even when Heimdall itself was started before the `setx`. `route()`, `is_blame()`, `icon_url()`, `channel_defaults`, aliases and blame patterns are removed.
- `config.toml`: personas only (`name` is used to label sibling personas in history, `avatar` is the icon file to upload).
- Kalousek: his app answers mentions and DMs like everyone else. His prompt ("silent sniper") still makes him reply with a terse `…` unless he is blamed. The old automatic "Kalousek follow-up when blamed" is gone, because his app doesn't receive channel messages that don't mention him.

## Manifests
`manifests/{babis,alenka,bourak,marty,peta,kalousek}.yaml`: Socket Mode on; bot events `app_mention`, `message.im`; scopes `app_mentions:read, chat:write, im:history, im:read, im:write, channels:history, groups:history, users:read` (`chat:write.customize` is dropped on purpose). The Messages tab is enabled, so people can DM each bot. `slack-manifest.yaml` is the legacy single-app manifest (kept for reference only).

## Env vars (user env, `setx`)
```
SLACK_OKU_BABIS_BOT_TOKEN     SLACK_OKU_BABIS_APP_TOKEN     (optional; if both unset, Babiš uses SLACK_OKU_BOT_TOKEN / SLACK_OKU_APP_TOKEN)
SLACK_OKU_ALENKA_BOT_TOKEN    SLACK_OKU_ALENKA_APP_TOKEN
SLACK_OKU_BOURAK_BOT_TOKEN    SLACK_OKU_BOURAK_APP_TOKEN
SLACK_OKU_MARTY_BOT_TOKEN     SLACK_OKU_MARTY_APP_TOKEN
SLACK_OKU_PETA_BOT_TOKEN      SLACK_OKU_PETA_APP_TOKEN
SLACK_OKU_KALOUSEK_BOT_TOKEN  SLACK_OKU_KALOUSEK_APP_TOKEN
```
BOT = `xoxb-…` (OAuth & Permissions → Bot User OAuth Token), APP = `xapp-…` (Basic Information → App-Level Tokens, scope `connections:write`).

## Kore's manual steps

### 0. Deploy the code (Heimdall)
`cd C:\code\oku-slack` → `git pull` (after the PR is merged) → `heimdall restart oku_slack`. No new dependencies. With only the legacy tokens set, the log shows `persona=babis connected` and 5 `skipped` lines. From then on every `@Babiš` mention is answered by Babiš. The other personas come back as their apps are added below.

### 1. Babiš: keep the EXISTING app (do not create a new one)
1. api.slack.com/apps → the existing app (the one whose tokens are `SLACK_OKU_BOT_TOKEN` / `SLACK_OKU_APP_TOKEN`) → **App Manifest** → replace the content with `manifests/babis.yaml` → Save. Slack asks you to reinstall, because `chat:write.customize` is removed: **Install App → Reinstall to Workspace**.
2. Basic Information → Display Information → App icon: upload `assets/avatars/babis.png`.
3. Tokens normally stay the same, so nothing to `setx`. If the reinstall shows a different Bot User OAuth Token, run `setx SLACK_OKU_BOT_TOKEN "xoxb-…"`, then restart Heimdall itself (see the note below).

### 2. The other 5: alenka, bourak, marty, peta, kalousek (one by one, in any order)
1. api.slack.com/apps → **Create New App → From a manifest** → pick the workspace → paste `manifests/<key>.yaml` → Create.
2. Basic Information → Display Information → App icon: upload `assets/avatars/<key>.png` (512×512) → Save.
3. Basic Information → **App-Level Tokens → Generate Token and Scopes**, add scope `connections:write` → Generate → copy the `xapp-…` token.
4. **Install App → Install to Workspace** → Allow → copy the **Bot User OAuth Token** (`xoxb-…`).
5. In a terminal on Heimdall:
   ```
   setx SLACK_OKU_<KEY>_BOT_TOKEN "xoxb-…"
   setx SLACK_OKU_<KEY>_APP_TOKEN "xapp-…"
   ```
   (`<KEY>` = `ALENKA`, `BOURAK`, `MARTY`, `PETA`, `KALOUSEK`)
6. `heimdall restart oku_slack`, then check `logs\oku_slack.log` for `persona=<key> connected user=U…`.
7. In Slack, invite the new bot into its channels: `/invite @<name>` (e.g. Marty into #oku-socky).

Note: `setx` only changes the user env for *new* processes. The service also reads missing tokens straight from `HKCU\Environment`, so a newly added persona needs only `heimdall restart oku_slack`. If you *change* a token that the running Heimdall already has in its env (e.g. a reissued `SLACK_OKU_BOT_TOKEN`), restart Heimdall itself from a new terminal, or sign out and back in, so the service doesn't inherit the stale value.

## Verify (no posting needed)
- `logs\oku_slack.log`: one `connected` line per persona that has tokens. Each missing persona logs `skipped: missing …` and each bad token logs `failed to start: …(invalid_auth)`. Process status: `heimdall status oku_slack`.
- Tests: `.venv\Scripts\python.exe -m pytest -q`.

## Acceptance criteria
- With only the legacy tokens set, the service starts, Babiš connects, and the other 5 are logged as skipped. No crash.
- Each connected app answers mentions/DMs only as its own persona, with no username/icon override.
- Bot messages never trigger replies (no loops between personas). The same token reused for two personas is refused.

## Rules
- Never print, log, or commit secrets (tokens, Gemini keys). Mention env var names only.
- No `Co-authored-by: Claude` (or any AI) trailer in commits.
- Do not post to Slack while implementing or verifying. Verify through logs only.
- The Heimdall service definition `C:\code\heimdall\services.d\oku_slack.toml` lives in the Heimdall repo. If its comment lists the env vars, update it there to the names above.
