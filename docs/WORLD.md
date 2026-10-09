# OKÚ World core (`oku_world`): ops + reference

Plan and rationale: `docs/ECOSYSTEM-PLAN.md` (P-001 done; P-005 chatter, P-004 real-life triggers (podnety), P-003 persona memory and P-006 consequences built and running in dry run). The world works completely without the wheel; the wheel is one optional event source at its edge.

## What runs
`python -m oku_slack.world.service` (stdlib only: works with `.venv` or `.venv-wheel`), Heimdall service `oku_world`.
- HTTP on **127.0.0.1:8798 only**. Every route answers 403 unless the peer is exactly `127.0.0.1` and the request carries no proxy or tunnel header (`CF-*`, `CDN-Loop`, `X-Forwarded-*`, `Forwarded`, `Via`, `X-Real-IP`, `True-Client-IP`, ...). Binding to anything other than 127.0.0.1 raises an error.
  - `GET /healthz`: liveness, version, dry_run, event count, ingest stats.
  - `GET /api/world[?events=N&player=<id>]`: projection snapshot (resources, blame, actors, memory, bridge, porada, sources, 7-day metrics, players).
  - `GET /api/budget`: limits, today's usage, kill/quiet state, and whether a porada would pass right now.
  - `GET /api/world/brief[?persona=x]`: persona memory brief(s), ≤ 600 chars each (404 for an unknown persona). The bridge uses it for normal replies.
  - `POST /api/events` (JSON draft or list): for local in-process sources (`manual`, `x`, `stream`, ...). The sources `wheel`, `bridge`, `backfill`, `podnet` and `consequence` are reserved for their own tails/engines.
- `/healthz` also shows `podnet` (inbox path, X poller state, pplx state + last run) and `consequences` (rows applied this run, rules-file error).
- **Loop** (`tick_s`, default 15 s): podnet pollers (X, pplx), then ingest sources (wheel outbox, usage, legacy wheel diary, podnet inbox), then the podnet reactor, the porada scheduler, the chatter director, and last the consequence engine.

## Files
| Path | Who writes | What |
|---|---|---|
| `<logs>/world.sqlite3` | oku_world (single writer) | diary `world_events` (`dedupe_key` unique) + `kv` (tail offsets, flags) |
| `<logs>/world.jsonl` | oku_world | JSONL mirror of every diary row (always has `dedupe_key`) |
| `<logs>/oku_world.log` | oku_world | rotating log (5 MB x 5) |
| `<logs>/world.kill` | Kore | kill switch: if the file exists, every autonomous action is denied |
| `<logs>/inbox/podnety.jsonl` | any feeder (Kore, CLI, script, Grok Bot, X poller, pplx poller) | podnety (env `OKU_WORLD_INBOX` overrides the path) |
| `oku_slack/world/consequences.toml` | Kore (repo) | consequence rules, resource seeds, storylet thresholds (env `OKU_WORLD_CONSEQUENCES` overrides) |
| `<src>/outbox/wheel.jsonl` | oku_wheel | wheel drafts (`world_client.py`) |
| `<src>/usage.jsonl` | oku_slack / wheel scenes | one line per LLM call -> `bridge.reply` |
| `<src>/wheel.sqlite3` | oku_wheel | read-only: one-time backfill + legacy `world_events` tail |
| `<src>/outbox/meeting_start.jsonl` / `meeting_ack.jsonl` | oku_world / oku_slack bridge | porada + chatter hand-off (`kind=chatter` lines) |

`<logs>` = env `OKU_WORLD_LOGS`, else `<checkout>/logs`. `<src>` = env `OKU_WORLD_SOURCE_LOGS`, else `<logs>`. Config: `[world]` in `config.toml` (env `OKU_CONFIG`), re-read whenever the file changes. Env `OKU_WORLD_DRY_RUN=0|1` overrides `dry_run`; `OKU_WORLD_KILL=1` kills.

On first start in a `<logs>` dir that still holds the P-000 wheel's own `world.jsonl` (rows without `dedupe_key`) and no `world.sqlite3`, that file is renamed to `world.legacy-wheel.<ts>.jsonl`, so the two never mix.

## Diary
Envelope: `id` (`we_0001`), `seq`, `ts`, `type`, `source`, `actor`, `subject`, `dedupe_key`, `payload`, `causal_parents` (default: the first row about the same subject), `regime`, `content_version`, `run_id`.
Ingest validation rejects (and counts) the following:
- a bad type pattern;
- an unknown source (`wheel bridge schedule backfill director agent consequence budget world x stream slack manual chatter podnet`);
- actor longer than 64 characters, subject longer than 160, dedupe_key longer than 200;
- a payload that isn't a JSON object or is over 4 KB;
- **free-text payload keys** (`text message body content prompt reply chat transcript`);
- non-numeric `ts`, or `ts` outside 2020…now+1 day;
- malformed `causal_parents`.
A duplicate `dedupe_key` returns `dup` and writes nothing. Ingest never raises.

Dedupe keys: `wheel:<uuid>` (outbox), `usage:<byte offset>:<sha1>` (usage tail), `wheel-legacy:<seq>` (old in-wheel diary), `schedule:porada:<date time>`, `porada:*:<slot|request id>`, `budget:porada:<slot>`, `schedule:chatter:<date time>`, `chatter:dry_run|requested:<slot>`, `chatter:started|ended:<request id>`, `budget:chatter:<slot>`, `podnet:<sha1(normalised url)[:16]>` (podnet.received), `podnet:rejected:<offset>:<sha1>`, `podnet:decided:<podnet key>` (the one reaction decision), `podnet:started|ended:<request id>`, `x:degraded:<status>:<hour>`, `consequence:<rule>:<trigger id>`.

## Sources (P-001)
- `wheel`: tail of `outbox/wheel.jsonl` (byte offset in kv; a half-written line waits; truncation restarts at 0 and dedupe absorbs the replay).
- `usage`: tail of `usage.jsonl` -> `bridge.reply` (persona, kind solo/meeting/..., channel, thread_ts, model, tokens, latency; never text). On first start it reads from byte 0, which backfills the bridge history.
- `wheel_legacy` (`legacy_wheel_tail = true`): read-only tail of `wheel.sqlite3:world_events`, the diary written by the currently deployed (P-000) wheel. It stops growing once the outbox wheel is deployed.
- **Backfill** (once, kv `world:backfilled`): the old `world_events` rows if any (source `backfill`, `payload.via` = original source, `payload.legacy_id`), otherwise reconstructed from the wheel tables `events/bets/sequences`. Writes `world.backfilled`.

## Projection (`state.py`, pure, rebuild == incremental)
Covers:
- resources Dotace 5 000 / Kampaň **60** (0–100) / Hranolky 80 (0–200) / Lajky 1 200 (seeds from `consequences.toml [seeds]`; players.toml `world_<k>` settings still win), which only move through `consequence.applied` rows (legacy `world.delta` / `consequence.delta` still fold);
- relationships `relations[who][whom]` (persona → persona or player) from `consequence.applied` effects;
- blame with evidence: missed `expired` events, vetoes, Kalousek on `steal_points`;
- actors: player / persona / ext, with per-source counts;
- per-player stats and witnessed acts;
- **persona memory**: the ≤ 20 newest rows each persona witnessed (actor, event host, command persona, porada chair, participants) with topic, co-participants and channel; `bridge.reply` is counted in `bridge.by_persona`, not remembered (it used to crowd everything else out); consequence rows are never "witnessed";
- bridge activity, porada counters, per-source counters, the wheel fold, 7-day metrics, `wheel_live`.

## Budget, quiet hours, kill switch (`budget.py`)
Defaults (D2): ≤ 6 top-level posts/day, ≤ 1 per channel per 3 h, ≤ 2 chatter threads/day of ≤ 4 turns, ≤ 40 world LLM calls/day, quiet hours 22:00–08:00 Europe/Prague, nothing while a wheel event is live.
The budget is a projection over diary rows, as follows:
- **posts**: `agent.posted` (top_level), `porada.requested` and `chatter.requested` (the opener is a top-level post in its channel);
- **chatter**: `chatter.requested` / `chatter.started`, counted once per subject `chatter:<slot>`;
- `podnet.requested` also counts as a top-level post in its channel (a podnet repost); its LLM charge is 1 when a reply persona is planned, else 0.
- **LLM**: `payload.llm_calls` on world-owned rows. A porada charges `porada_llm_estimate` = 12; a chatter charges `turns - 1` (the opener is a template, the bridge generates the replies). Human-triggered bridge replies never count. `chatter.ended` records the real number as `llm_calls_actual` (retries included) without charging it again.

`*.dry_run` rows never consume budget. Kill switch: `kill_switch = true`, the file `<logs>/world.kill`, env `OKU_WORLD_KILL=1`, or kv `world:silenced_until` (reserved for `/oku ticho`). Prague time works without `tzdata` through a built-in EU DST rule (Windows venvs have no tz database).

## Standalone scheduled porada (independent of the wheel)
`porada_schedule = "mon-fri 10:00"` (also `daily 09:30`, `mon,wed,fri 10:00,15:00`). On each due slot:
1. `schedule.due` claims the slot once (restart-safe). A slot that is more than `porada_grace_min` (30) minutes late is recorded once as `schedule.skipped` and is never caught up.
2. Kill switch / quiet hours / wheel live / budget -> `budget.denied` with reasons.
3. `dry_run = true` -> `porada.dry_run` with the topic and what it would request. **No hand-off line is written.**
4. Live -> one line `{id, at, channel, thread_ts: null, source: "world", topic, opener, slot}` in `<src>/outbox/meeting_start.jsonl` + `porada.requested`. The bridge's `handoff.Inbox` calls `Coordinator.start_external`: for `source=world` the chair (Babiš) posts the opener top-level in #oku-porada and a real `meeting.py` porada runs in its thread. The ack (`meeting_ack.jsonl`, now with `thread_ts`) becomes `porada.started`, or `porada.failed` (dup / rejected / stale / error / `no_ack` after 120 s).

The topic comes from world state (top blame, low Hranolky/Kampaň, the last expired wheel event, last week's bridge activity) or a template. The choice is deterministic per slot (keyed RNG `PORADA:<slot>`). A bridge older than this change answers `rejected` (no `thread_ts`), so a premature live request is harmless.

## Autonomous chatter (P-005; `world/chatter.py` + bridge `oku_slack/chatter.py`)
`chatter_schedule = "mon-fri 11:30,16:30"` (same syntax as the porada). On each due slot:
1. `schedule.due` (storylet `CHATTER`) claims the slot once; more than `chatter_grace_min` (30) late -> `schedule.skipped`.
2. **Director pick** (keyed RNG `CHATTER:<slot>`, reproducible): eligible storylets are those whose fact holds (`blame` -> `KALOUSEK_VINA`, an expired wheel event -> `PROPADLA_AKCE`, Hranolky < 60 -> `HRANOLKY_DOCHAZEJI`, Kampaň < 50 -> `KAMPAN_DOLE`, a porada topic today -> `PORADA_DOZVUK`, Slack replies in the last 7 days -> `SLACK_KECY`) plus the always-on templates (`BABIS_CHCE_CISLA`, `MARTY_VIRAL`, `KANTYNA_MENU`, `DISKO_PATEK`). Storylets and channels already used today, and channels inside the per-channel gap, are avoided when anything else is left. Cast = the storylet's lead + 1–2 partners (2–3 of Babiš, Alenka, Bourák, Marty, Peťa/Macinka, Kalousek); turns = 3 or 4 (capped by `chatter_max_turns`, the bridge hard-caps at 4).
3. Kill switch / quiet hours / wheel live / chatter threads/day / posts/day / per-channel gap / LLM cap -> `budget.denied` (`storylet: CHATTER`, `pick`, `reasons`). No storylet fits the configured channels -> `budget.denied` `no_storylet`.
4. `dry_run = true` -> **`chatter.dry_run` only** (personas, channel, channel_name, topic, opener, turns, llm_estimate, candidates, rng_key, why). **No hand-off line.**
5. Live -> one line `{id, at, kind: "chatter", source: "world:chatter", channel, thread_ts: null, personas, topic, opener, turns, storylet, slot}` in `meeting_start.jsonl` + `chatter.requested`. The bridge (`Coordinator.start_chatter`) validates it (channel in `[world.chatter_channels]`, 2–3 distinct online personas, 2 ≤ turns, opener present), refuses while a meeting/chatter runs in that channel (`dup`) or a wheel skit is live (`rejected wheel_live`), has the first persona post the opener **top-level**, and acks `started` with `thread_ts`. It then drives turns 2..N **itself** in that thread (round robin, never the same persona twice in a row, 6–12 s apart), each generated with the thread so far as context, and appends a second ack `done` (or `stopped` after a human "stop"/"konec", or `error`) with `turns`, `llm_calls`, `guarded`, `fallbacks`. The world turns those into `chatter.started` -> `chatter.ended`, or `chatter.failed` (`rejected`/`dup`/`stale`/`error` before start, `no_ack` after `chatter_ack_timeout_s` = 120 s). A started exchange without a terminal ack after `chatter_done_timeout_s` (900 s) -> `chatter.ended` `timeout`.

Loop guard: unchanged. Every chatter message is a bot message (`bridge.ignored()`); the chatter thread is never registered as a meeting, so `Coordinator.claim` / `route_plain` never route into it; no persona answers another through Slack events. A human who @mentions a persona in the thread gets the usual solo reply.

Satire guardrails: `CHATTER_RULES` in every turn prompt (stay in character, 1–2 sentences, ≤ 200 chars, no invented quotes of real people, nothing presented as real fact or news, no health/family/crime topics, no @mentions). The bridge strips mentions and broadcasts, caps each turn at 280 chars, and rejects a turn that hits the sensitive-topic or quotation filter (`guard_hit`): it retries up to 3 times, then posts a neutral in-character fallback line. The storylet templates are linted by tests against the same filter.

Compatibility: a bridge without chatter support sees `source: world:chatter` with no thread and answers `rejected`, so it never starts a porada from a chatter line.

## Podnety: real-life triggers (P-004; `world/podnet.py`, `xsource.py`, `pplx.py`, `react.py`)
A podnet is one public link (a Babiš video on X, a stream announcement, a news article). **Every feeder writes the same inbox**, `<logs>/inbox/podnety.jsonl`, one JSON object per line:
`{"url", "kind": "x_video|x_post|stream|video|news|other", "author", "title" (or "text", ≤ 280 chars), "observed_at" (unix or ISO), "source" (feeder label, e.g. manual/x/pplx/grokbot), "test": bool}`.
- **Inbox tail** (`InboxSource`, byte offset in kv): validation (http(s) URL ≤ 500 chars, no credentials/odd ports/localhost, known kind, title ≤ 280, sane time), URL normalisation (https, no `www.`/`mobile.`, `twitter.com` → `x.com`, X handle lower-cased, tracking params and fragment dropped) and **dedupe by URL** → one `podnet.received` row (source `podnet`, actor `ext:<author>`, payload `url kind host author title observed_at source test`; the diary never takes a `text` key). A bad line → `podnet.rejected` (reason + offset only).
- **Manual**: `python -m oku_slack.world.podnet <url> [--kind x_video] [--text "…"] [--author @x] [--source manual] [--test] [--inbox <file>]` appends one validated line (exit 2 + reason when rejected). Any script (or Grok Bot) can append lines itself. `/kolo podnet` was NOT added to the wheel (owner check + cross-service inbox path is not trivial/isolated); `/oku podnet` waits for P-002.
- **X poller** (`xsource.py`): enabled ONLY when env `X_BEARER_TOKEN` is set for the oku_world process (on Windows also read from `HKCU\Environment`, like the wheel's tokens). No token → logs `x source disabled: no token` once. Official API v2, read-only GETs (`/2/users/by/username/<h>` cached in kv, `/2/users/<id>/tweets?since_id=…&exclude=retweets,replies&expansions=attachments.media_keys&media.fields=type`), every `interval_min` (10) between 08:00 and 22:00 Prague; the first poll only stores `since_id`; video posts only (`video_only`); new posts are appended to the inbox (`source: x`). 401/402/403/429 → `source.degraded` (≤ 1 row/hour) and back-off (3 × interval or until `x-rate-limit-reset`). It never posts to X. Config `[world.podnet_x]` (`enabled`, `accounts`, `interval_min`, `hours`, `video_only`, `max_results`).
- **pplx poller** (`pplx.py`, optional, free): every `interval_min` (120) between 08:00 and 22:00 it runs `void-pplx-ask --mode fast` once (argument list, no shell, background thread, never two at a time) asking for new public posts/videos of `accounts` (and stream announcements of `streams`). The output is **untrusted data**: only URLs are extracted (plus a ≤ 120-char title from the same answer line, markup/mentions stripped), hosts must be x.com/twitter.com/youtube.com/youtu.be/twitch.tv/kick.com, X URLs must be `/<configured handle>/status/<id>` and the id's snowflake time must be within `max_age_h` (48; pplx returns year-old posts), stream links must start with a configured channel. ≤ `max_per_run` (3) new podnety per run, deduped by URL, `source: pplx`. Config `[world.podnet_pplx]` (`enabled`, `interval_min`, `hours`, `accounts`, `streams`, `mode`, `timeout_s`, `command`, `max_age_h`, `max_per_run`).
- **Reaction** (`react.py`, `Reactor`, one decision per podnet, oldest first): guardrails first (a title touching health/family/crime, Czech or English, or carrying a quotation → `podnet.skipped guard:…`). Then a reproducible plan (keyed RNG `PODNET:<key>`): `x_video`/`x_post` → Marty (#oku-socky) or Babiš (#oku-dotace-desk), `stream` → Peťa/Macinka (#oku-disko), `video` → Marty, `news` → Babiš or Kalousek (#oku-vina); the opener is **the URL + a template comment of ≤ 2 sentences** that never repeats the title and says nothing about the post's content or the real person; with `podnet_reply_rate` (0.5) one reply persona answers once. Budget: posts/day, per-channel gap, LLM cap, quiet hours, wheel live, kill switch, plus `podnet_reactions_per_day` (2; test podnets don't count). Quiet hours / wheel live / channel gap wait and retry until `podnet_max_age_h` (6) → `podnet.skipped stale`; other denials → `budget.denied` (storylet `PODNET`).
  - **dry_run** → `podnet.dry_run` (persona, reply_persona, participants, channel, channel_name, opener, turns, llm_estimate, rng_key, why, test). No hand-off line.
  - **live** → a `kind=chatter` line (source `world:chatter`, `storylet: PODNET`, `podnet: {url, kind, key}`, 1–2 personas, 1–2 turns, `briefs`) on `meeting_start.jsonl` + `podnet.requested`; acks → `podnet.started` / `podnet.ended` / `podnet.failed` (`no_ack` after 120 s). **Test podnets are never handed off** (`podnet.skipped test`).
  - Bridge side (`oku_slack/chatter.py`): a request with `podnet` may have 1–2 personas and 1–2 turns, and its opener must contain the podnet URL. The first persona posts the opener top-level (the "repost"); the optional reply gets `PODNET_RULES` in its prompt (it does not know the link's content: no claims, no quotes, nothing about the real person) on top of the usual chatter guardrails/filter.

## Persona memory (P-003; `world/memory.py`)
`memory.brief(state, persona)` builds a Czech brief of ≤ 600 chars from the projection: recent events the persona took part in (porada, debates with whom/where, shared links, wheel events with who missed them), blame (its own + the team's top), relationships with other personas and with players (and who holds a grudge against it), the last topics, its Slack reply count and the resource state. Only diary metadata, never human message text.
- Hand-offs carry `briefs` (`{persona: brief}`): live chatter and podnet lines for their personas, the live porada line for Babiš/Alenka/Bourák/Marty/Peťa. Dry-run rows record only `brief_chars`. The bridge appends `TVOJE PAMĚŤ ZE SVĚTA OKÚ (…): <brief>` to that persona's system prompt (chatter turns, porada turns).
- Normal replies: `Bridge.handle` fetches `GET /api/world/brief?persona=<me>` (loopback, no proxies, 0.6 s timeout, cached 120 s). Any failure, a down world, or `[world] brief_in_replies = false` → empty brief, and the reply works exactly as before.
- Switches: `briefs_in_handoff` (world), `brief_in_replies` (bridge).

## Consequences (P-006; `world/consequences.py` + `consequences.toml`)
The engine tails the diary (kv `consequence:seq`) and, for every rule matching a row, writes one `consequence.applied` row: source `consequence`, `causal_parents = [trigger]`, `ts` = the trigger's ts, `dedupe_key consequence:<rule>:<trigger id>`, payload `{rule, trigger, trigger_type, dry_run, effects: [{resource, delta} | {blame, n} | {relation: [from, to], delta}]}`. Targets (event host, missing players, lead, participants) are resolved when the row is written, so the projection folds the rows without config: rebuild == incremental == replay. Changing the TOML only affects rows processed afterwards (re-read on change; a broken file keeps the last good rules and shows the error in `/healthz`).
Starter rules: `wheel.expired` → Kampaň −3 and host→missing players −1; `wheel.done` → Kampaň +3 (`kantyna` also Hranolky +40); `wheel.vetoed` → Kampaň −2; porada held (`porada.started`/`porada.dry_run`) → Dotace +250, Hranolky −10 (catering); podnet reacted (`podnet.started`/`podnet.dry_run`) → Lajky +100 (videos/streams also Kampaň +2); every chatter → rapport +1 between all participants; blame chatter (`KALOUSEK_VINA`, `PROPADLA_AKCE`) → Kalousek blame +1 and lead→others −2; canteen chatter → Hranolky −5; campaign chatter → Kampaň +2; viral chatter → Lajky +30.
Rows marked `test: true` (test podnets) never trigger consequences. `include_dry_run = true`: dry-run rows count as if the action happened (the row says `dry_run: true`), so the dry run shows world dynamics. **Kampaň fix:** seed 60 (was 35) and `kampan_low = 40` (was 50), `hranolky_low = 60`; the porada topic and chatter storylet facts read these thresholds.

## Go-live
**Stage 0: dry run from the worktree (now).** `C:\code\heimdall\services.d\oku_world.toml` sets `cwd = C:\code\oku-slack-world`, uses `C:\code\oku-slack\.venv-wheel\Scripts\python.exe`, `OKU_WORLD_SOURCE_LOGS = C:\code\oku-slack\logs` (reads the live usage.jsonl, wheel outbox and wheel.sqlite3 read-only; the diary stays in the worktree's `logs`), `autostart = false`.
- Start oku_world through Heimdall, then check `http://127.0.0.1:8798/healthz` and `/api/budget`.
- At the next weekday 10:00, `C:\code\oku-slack-world\logs\world.jsonl` should contain `schedule.due` + `porada.dry_run`.

**Stage 1: deploy the code to the live services.** The bridge (world porada requests), the wheel (outbox, panel/canvas/`/api/world` removal) and the world must run the same code.
- `feat/oku-world` is based on `5cdd36b`. If `feat/wheel-of-fortune` has not moved, run `git -C C:\code\oku-slack merge --ff-only origin/feat/oku-world` in the live tree (Kore's call), then restart `oku_wheel` and `oku_slack` (or wait for the 05:25 daily restart).
- Then repoint the `oku_world` manifest to `cwd = C:\code\oku-slack`. To keep the dry-run diary, either set `OKU_WORLD_LOGS = C:\code\oku-slack-world\logs`, or stop the world and copy `world.sqlite3` + `world.jsonl` into `C:\code\oku-slack\logs` first. Otherwise the old `world.jsonl` is renamed and the backfill runs again from `wheel.sqlite3`.

**Stage 2: live porada.** Do this only after Stage 1 and ≥ 2 weekdays of plausible dry-run rows. Set `dry_run = false` in `[world]` of the `config.toml` the service reads (picked up within one tick), or set env `OKU_WORLD_DRY_RUN=0` in the manifest and restart. Rollback: `dry_run = true`, `logs\world.kill`, or stop `oku_world` (replies and the wheel are unaffected).

**Enable the X source.** Set the token as a user env var on the PC (never in a repo or manifest): `setx X_BEARER_TOKEN <token>` (or System Properties → Environment Variables). The oku_world process reads env `X_BEARER_TOKEN`, falling back to `HKCU\Environment`, so `heimdall restart oku_world` is enough. If you prefer an explicit manifest entry, Heimdall manifests never hold secret values (LAW 6); at most a comment `# X_BEARER_TOKEN: user env var, inherited by the supervisor` under `[service.oku_world.env_vars]`. Check `/healthz` → `podnet.x` (`ok`, `idle: outside hours`, `degraded:<status>`), and set a Developer Console spending limit first (plan §4.1).

**Stage 3: live chatter (P-005).** Needs Stage 1 including commit `c3fcf1f`+ in the live tree (the bridge must have `start_chatter`), and ≥ 2 weekdays of `chatter.dry_run` rows Kore finds acceptable (check personas, channels, topics, openers in `logs/world.jsonl`). `dry_run` is one switch for porada and chatter together. To go live with the porada but without chatter, set `chatter_enabled = false` first. Before the first live chatter: make sure every persona app is a member of its chatter channel (`chat:write` into #oku-kantyna, #oku-dotace-desk, #oku-socky, #oku-disko, #oku-vina; not verified). Rollback: `chatter_enabled = false`, `dry_run = true`, `logs\world.kill`, or stop `oku_world`; an exchange already running finishes its ≤ 4 turns (or a human types "stop" in the thread).

**Stage 4: live podnet reactions (P-004).** Same `dry_run` switch as porada/chatter. Before it: ≥ 2 weekdays of `podnet.dry_run` rows Kore finds acceptable (persona, channel, opener), and Stage 1 including the bridge's podnet support (`chatter.validate` with `podnet`). To keep podnety dry while porada/chatter go live, set `podnet_react = false` (podnety are still recorded). Rollback: `podnet_react = false`, `dry_run = true`, `logs\world.kill`, or stop `oku_world`.
