# Production rollout: `voice-improvements` to the live/prod server

Written 2026-10-30. Nothing in this document has been run against prod; every
step is for the operator. Prod reads from the agent sandbox are blocked, so the
prod facts below come from the 2026-10-15..22 setup notes and must be
re-checked with the read-only queries in step 0.

**Why now:** OpenAI shuts down gpt-5-mini on 2026-12-11 and gpt-4o-mini /
gpt-4.1-mini on 2027-01-14. Prod bots run on those models; the replacement
(gpt-6-luna as decision and reply engine) lives on `voice-improvements`, which
has been running on demo since 2026-10-26 (Phase 3) and 2026-10-29 (Phase 4).

Servers: **demo** = `echosphere-live` (192.168.60.123, services as root,
`/var/www/html/EchoSphere`); **prod** = `aurexion-voicebot` (192.168.60.187,
services as user `echosphere`, same path, sparse checkout, Python 3.12 venv
`env/`, MySQL 127.0.0.1:3306 `voice_bot`, Postgres 127.0.0.1:5432
`echosphere_knowledge`, Mongo 127.0.0.1:27018, Redis 127.0.0.1:6380, Apache
`echosphere.aurexion.ai` serving `dist/`). Prod was last deployed at
`93199f3` (2026-10-21), DB at alembic `b2d4f6a8c0e2` (2026-10-21), PG at
`b2c4e6f8a0d1` (2026-10-15).

---

## 0. Read-only on prod first

Run from the checkout as the `echosphere` user or root (`cd
/var/www/html/EchoSphere` first; a relative path in an ssh one-liner silently
runs in `/root`).

```bash
git status --porcelain            # expect: nothing (dist-prev*, env/, .env are excluded/untracked)
git rev-parse --short HEAD        # expect 93199f3
env/bin/alembic -c backend/alembic.ini current     # expect b2d4f6a8c0e2
env/bin/alembic -c backend/alembic_pg.ini current  # expect b2c4e6f8a0d1
curl -s localhost:9002/health     # active_sessions must be 0 before any restart
```

**Model inventory** (which bots sit on a retiring model; the `--include-drafts`
run lists drafts too):

```sql
SELECT s.llm_model, b.status, COUNT(*) AS bots,
       GROUP_CONCAT(b.id ORDER BY b.id SEPARATOR ' ') AS bot_ids
  FROM voice_bots b JOIN voice_bot_settings s ON s.bot_id = b.id
 WHERE b.is_deleted = 0
 GROUP BY s.llm_model, b.status ORDER BY s.llm_model, b.status;

SELECT provider_code, code, status, is_default
  FROM provider_models WHERE capability = 'llm' AND is_deleted = 0 ORDER BY provider_code, code;

SELECT b.id, b.name, b.status, s.stt_model, s.tts_model,
       JSON_EXTRACT(s.llm_settings, '$.orchestration_model') AS decision_model
  FROM voice_bots b JOIN voice_bot_settings s ON s.bot_id = b.id
 WHERE b.is_deleted = 0 ORDER BY b.status, b.id;
```

Then the generator, which prints the per-bot migration SQL for THIS database
and runs no write: `env/bin/python scratchpad/prod_model_migration.py >
/tmp/model_migration.sql` (section 5).

**Loudness** (27b3be7f2 makes Sarvam `loudness` take effect on bulbul:v3 and
bulbul:v4-flash; before it only bulbul:v2 applied it, so a bot that stored a
value on v3 would change volume at deploy):

```sql
SELECT s.bot_id, b.name, b.status, s.tts_model,
       JSON_EXTRACT(s.tts_settings, '$.loudness') AS loudness, s.language_voice_map
  FROM voice_bot_settings s JOIN voice_bots b ON b.id = s.bot_id
 WHERE b.is_deleted = 0
   AND JSON_EXTRACT(s.tts_settings, '$.loudness') IS NOT NULL
   AND JSON_EXTRACT(s.tts_settings, '$.loudness') NOT IN (1, 1.0)
   AND (s.tts_model IN ('bulbul:v3', 'bulbul:v4-flash')
        OR s.language_voice_map LIKE '%bulbul:v3%' OR s.language_voice_map LIKE '%bulbul:v4-flash%');
```

Expect no rows (demo had none: its six stored 1.1 values are all bulbul:v2
clones). A row here is a bot that will get louder/quieter; set its loudness to
1 in Studio or accept the change knowingly.

Also note the prod `.env` pins: `STT_MODEL`, `TTS_MODEL`, `LLM_MODEL` (an
`.env` value overrides the new code defaults; see section 4) and that no
`ORCHESTRATION_MODEL` / `ORCHESTRATION_SERVICE_TIER` line exists (then the
defaults apply).

## 1. Preconditions

- **History rewritten on 2026-10-30.** Two MySQL dumps under `backups/` were
  purged from the branch with git filter-repo and `origin/voice-improvements`
  was force-pushed: every branch commit after 2026-07 has a new id (the
  Jeeves commits `ebd960630` / `ddfae3576` / `4462308c7` are now
  `9fb8b856e` / `15271e2bc` / `d4f91724c`; prod's `93199f3` is `65af50ebd`).
  Every clone, demo included, must `git fetch origin && git reset --hard
  origin/voice-improvements` before any further work (first step of
  `scratchpad/prod_runbook_commands.md`). The old dump commit is still
  reachable on `origin/pipecat-1.12-upgrade`.
- **Local tree clean.** `git status --porcelain` on the developer machine shows
  nothing. If another session has uncommitted work in the tree, commit or
  stash it before merging — nothing uncommitted reaches the server anyway,
  but the merge must be made from a known state.
- **One alembic head.** `env/bin/alembic -c backend/alembic.ini heads` prints
  exactly `4d8b2f6a9c1e (head)`; `-c backend/alembic_pg.ini heads` prints
  `b2c4e6f8a0d1 (head)` (no PG migration on the branch). Two heads = stop and
  merge them first.
- **Full tests green** on the merge candidate: `env/bin/python -m pytest
  tests/unit -q` (5,100+ tests, ~10 min; the two `VoiceTab.test.tsx` vitest
  failures "preview offers only the languages…" and "a language change never
  swaps out a provider…" pre-date the branch).
- `requirements.txt` changed since prod's commit (pipecat-ai 1.12.0 → 1.13.0,
  openai 3.22.1; Smart Turn and Silero come with the pipecat wheel). Python
  3.12 is what demo runs with the same pins (`env/lib/python3.12`), so the
  install is known-good on 3.12. `package.json` / `pnpm-lock.yaml` did not
  change: no `pnpm install`, but the frontend bundle must be rebuilt.
- Prod gotchas carried from the setup notes: root's interactive umask is 027 —
  files written by `git pull` as root are unreadable by `echosphere` until
  `chgrp`/`chmod` (the 2026-10-21 outage); the checkout is a non-cone sparse
  checkout (`git sparse-checkout reapply` after every merge); a nightly
  `unattended-upgrade` at ~03:30 restarts Postgres/Mongo/Redis — do not deploy
  around that window.

## 2. Merge `voice-improvements` → `main`

Checked 2026-10-30 after the rewrite: the branch's merge-base with `main` is
still `70c41bf5d`, but `origin/main` (`5565e8b76`) carries two commits the
branch never got (`873f1c6c` "Delete VoiceBot directory", 2026-07-13, and
the PR #2 merge commit), so the merge is NOT a fast-forward: it needs a
merge commit made from a fetched `origin/main` (expect the deleted
`VoiceBot/` directory to show up in the diff; resolve by keeping it deleted).
Prod's `93199f3` is no longer an ancestor of the branch either (rewritten
to `65af50ebd`), so the server pull is a reset (step 3.2), not `--ff-only`.

```bash
git fetch origin main && git checkout -B main origin/main
git merge --no-ff voice-improvements      # resolve, review the merge commit's diff
git push origin main
git checkout voice-improvements
```

Record the merged SHA (`git rev-parse --short main`); every server step below
pins to it.

## 3. Server steps, in order

Stop here if any step fails; the rollback for that step is in section 7.

1. **Back up what the rollback needs** (root, prod):
   `cp -a env env.bak-$(date +%Y%m%d)`;
   `mysqldump -h 127.0.0.1 -u echosphere_prod -p --single-transaction voice_bot > /root/echosphere-prep/voice_bot-$(date +%Y%m%d).sql`;
   note `git rev-parse HEAD`, `alembic current` (both inis) and the model
   inventory output from step 0.
2. **Pull the code** (as root, umask 022 so new files stay readable):
   ```bash
   cd /var/www/html/EchoSphere && umask 022
   git fetch --no-tags origin main </dev/null && git reset --hard <merged SHA> && git sparse-checkout reapply
   chgrp -R echosphere . && chmod -R g+rX,o-rwx . && chmod 640 .env
   ```
   Services keep running the old code until restarted — nothing changes yet.
   `reset --hard`, not `--ff-only`: prod's `93199f3` is not an ancestor of
   the rewritten branch. It only touches tracked files; `env/`, `.env` and
   `dist*` are untracked and stay. The rollback row below still works (the
   `93199f3` object is the server's own HEAD).
3. **Dependencies** (venv, Python 3.12): `env/bin/pip install -r
   requirements.txt && env/bin/pip check`. Expect pipecat-ai 1.13.0 and a clean
   `pip check`.
4. **Migrations, BEFORE any restart** — the new code reads columns the old
   schema lacks (identity_confirmation_mode: every call would silently lose
   its runtime context; the gpt-6-luna catalog row: the decision default falls
   back to the conversation model):
   ```bash
   env/bin/alembic -c backend/alembic.ini upgrade head       # b2d4f6a8c0e2 -> c2e4a6b8d0f1, d3f5b7c9e1a2, e4b6d8f0a2c4, f5b7d9e1a3c5, 4d8b2f6a9c1e
   env/bin/alembic -c backend/alembic_pg.ini upgrade head    # no-op, confirms b2c4e6f8a0d1
   ```
   The five MySQL migrations add catalog rows (bulbul:v4-flash model + persona
   voices, gpt-6-luna with pricing, loudness schema ranges) and one nullable
   column; all have downgrades.
5. **Phase 3 deploy SQL and the price fix** — `scratchpad/phase3_deploy.sql`
   was written for demo's rows: statement (1) retires demo's saaras:v4 ₹30/h
   price row `ppr_d8261a596a86` and inserts the negotiated ₹9/h row, (2) makes
   saaras:v4 the STT catalog default, (3)/(5) move three demo bots to
   saaras:v4 / gpt-6-luna, (4) deactivates gpt-4.1-nano, (6) moves two mPokket
   bots off gpt-5-mini, and (b) restores the saaras:v3 price row's start date.
   On prod only (2) and (4) are generic; (1) and (b) depend on prod's own
   price-row ids and (3)/(5)/(6) on prod's bot ids. Run on prod:
   ```sql
   -- from scratchpad/phase3_deploy.sql, statements (2) and (4) verbatim:
   UPDATE provider_models SET is_default = (code = 'saaras:v4'), updated_at = NOW()
    WHERE provider_code = 'sarvam' AND capability = 'stt' AND code IN ('saaras:v3', 'saaras:v4')
      AND is_default <> (code = 'saaras:v4');
   UPDATE provider_models SET status = 'inactive', updated_at = NOW()
    WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-4.1-nano' AND status = 'active';
   ```
   For the saaras:v4 price: `SELECT id, model_code, price, effective_from,
   status FROM provider_pricing WHERE model_code = 'saaras:v4';` — if prod
   carries the ₹30/h list row from the seed, apply statement (1) with that
   row's id in place of `ppr_d8261a596a86`; the ₹9/h row text is in the file.
   Statement (b) applies only if prod's saaras:v3 row was edited the same way
   (`effective_from > NOW()`); demo's was. The per-bot model moves are
   section 5, not this step.
6. **Clear cached bot configs**: `redis-cli -p 6380 -a "$REDIS_PASSWORD"
   --scan --pattern 'botcfg:*' | xargs -r redis-cli -p 6380 -a "$REDIS_PASSWORD" del`
   (prod Redis is the dedicated instance on 6380 with a password; `FLUSHALL`
   is renamed off there). Needed after any SQL that the API did not write.
7. **Restart** when `curl -s localhost:9002/health` shows `active_sessions: 0`:
   `systemctl restart echosphere-api echosphere-voice echosphere-mcp` and
   `echosphere-gateway` if telephony is enabled on prod (it was disabled on
   2026-10-22 pending FreeSWITCH). Then `systemctl status` ×4, 0 restarts,
   `curl -s localhost:9001/api/health/ready`, and `journalctl -u
   echosphere-api --since -5m | grep -i -E "error|traceback"` empty.
8. **Frontend** (static, no restart): as root with `umask 022`:
   `corepack pnpm exec tsc --noEmit && corepack pnpm exec vite build --outDir dist-next --emptyOutDir`,
   `chgrp -R echosphere dist-next && chmod -R u=rwX,g=rX,o=rX dist-next && setfacl -R -m u:www-data:rX dist-next && setfacl -R -d -m u:www-data:rX dist-next`,
   then `mv dist dist-prev-$(date +%Y%m%d) && mv dist-next dist`. Check the
   site serves the new bundle (`curl -s https://echosphere.aurexion.ai/ | grep -o 'index-[A-Za-z0-9_-]*\.js'` changes).
9. **Per-bot model migration** (section 5), then the same Redis clear, no
   restart needed.
10. **Smoke tests** (section 6).

## 4. What every bot gets after the deploy (code defaults) and what stays off

ON for every bot unless the bot's own settings say otherwise:

| Feature | Default | Where it is set |
|---|---|---|
| Goal Engine decision on gpt-6-luna, fast tier, lowest effort, per-bot prompt cache key | on | `ORCHESTRATION_MODEL=gpt-6-luna`, `ORCHESTRATION_SERVICE_TIER=fast` code defaults (`shared/config.py`), migration d3f5b7c9e1a2 adds the catalog row |
| Decision budget 2.5 s on gpt-6-luna (1.6 s elsewhere) | on | `_MODEL_TIMEOUT_SECONDS`; `llm_settings.orchestration_timeout_seconds` overrides |
| Reply calls at the lowest reasoning effort | on | `build_llm_provider` (`reply_reasoning_effort` overrides) — reply tier stays standard unless `reply_service_tier` is set |
| Decision prompt warm-up at call start | on | `GoalEngine.warm_up` |
| Smart Turn endpointing (auto: supported languages) | on | tenant `turn_detection.smart_turn` (default auto), `SMART_TURN_ENABLED=true` kill switch |
| No decision prefetch on Smart Turn calls | on | Phase 5 Change 2 |
| TTS socket connected at call start, start held ≤1.5 s | on | `tts_startup_connect` |
| End-call playout wait (last word no longer cut) | on | serializer playout clock |
| Identity confirmation: standard for every bot, strict for collections policy bots | on | `runtime_context_schemas.identity_confirmation_mode` (NULL = default) |
| Sarvam loudness honoured on bulbul:v3 / v4-flash | on | `_SARVAM_LOUDNESS_MODELS` — hence the step-0 query |
| saaras:v4 STT with automatic key terms | **default for new bots only** | `STT_MODEL` default is saaras:v4 in code, but a `.env` pin or the bot's `stt_model` wins; existing bots keep their stored model |

OFF unless switched on per bot (every one a key in
`voice_bot_settings.llm_settings`, see `scratchpad/phase4_settings_on.sql` for
the exact JSON): `deterministic_answers`, `speculative_reply`,
`parallel_extraction`, `early_ack_ms`, `decision_fast_output` (failed its
replay gate; leave off). Also off: bulbul:v4-flash (bots keep bulbul:v3; the
personas are in the catalog), realtime Sarvam STT (`SARVAM_REALTIME_STT`
default false), background speech guard, hybrid barge-in duck, speaker
consistency (tenant turn-detection knobs, default off).

To turn the Phase 4 switches on for chosen prod bots, copy
`scratchpad/phase4_settings_on.sql` with prod's bot ids (it writes JSON
`true` via `CAST('true' AS JSON)`; a bare `TRUE` stores `1`, which the runtime
reads as off) and keep its rollback file alongside.

## 5. Bots on retiring models

`scratchpad/prod_model_migration.py` reads the catalog and prints, per bot
still on gpt-5-mini / gpt-4o-mini / gpt-4.1-mini (published and in_review;
`--include-drafts` for the rest; `--include-gpt41` adds gpt-4.1, which has no
announced date), a guarded `UPDATE voice_bot_settings SET llm_model =
'gpt-6-luna' … WHERE bot_id = … AND llm_model = '<old>'`, the same for a
bot-chosen `orchestration_model`, the `ai_config_profiles` presets, and —
commented, for after the last bot moved — the catalog rows to deactivate. It
refuses to print anything useful until gpt-6-luna is an active catalog row
(step 3.4). `--with-reply-options` also sets `reply_service_tier=fast` and
`reply_reasoning_effort=lowest` as the demo test bots run; the effort is
already the call-time default, the fast tier costs twice the standard rate and
is not needed for the decision (which has its own tier) — leave it off unless
a bot's reply first token is measured late.

Demo's output on 2026-10-30 is checked in as
`scratchpad/prod_model_migration_demo.sql` (21 published bots to move, 19
drafts listed, 3 presets): the prod list comes from running the generator on
prod. Apply with `mysql … voice_bot < /tmp/model_migration.sql`, clear the
Redis configs, then re-run the generator: it must print 0 bots.

## 6. Smoke tests after the restart

One call per bot type; read each with `env/bin/python
scratchpad/phase3_first_call.py --bot <id>` (prod Mongo: add `--mongo-uri
mongodb://<user>:<pass>@127.0.0.1:27018/?authSource=admin --db voice_bot`)
and `journalctl -u echosphere-gateway --since -10m`.

| Bot type | Drive | Check |
|---|---|---|
| Scripted workflow (Haier / Jeeves style: intent hubs, exact lines) | greeting → "haan" → a hub answer ("aaj") → an off-script question ("refund kab milega?") → "nahin" at a yes/no hub → end | `models.decision=openai/gpt-6-luna`, `decision_warmup ok`, every turn `path=LLM decision` (or `deterministic` once the flag is on) with `budget=2,500 ms`, no `orchestration_fallback`, TTS first audio on bulbul:v3 ≤ 400 ms, the off-script question answered without leaving the node, exact lines spoken verbatim |
| MDND (semantic extraction + grounded rewrite) | the full story in one breath ("location par gaya, call kiya, guard ko diya"), then "haan" at the verify hub | all four facts captured on the first turn (`slots`), the rewrite spoken (`workflow_reply_grounded` constrained), response ≤ 5 s today / ≤ 3 s with `parallel_extraction` |
| Prompt-only / survey (Manappuram style) | identity "haan ji boliye" → three answers → a Malayalam/English switch mid-call | identity confirmed on the first reply (`identity_rule` in standard mode), replies from gpt-6-luna with `reasoning_effort=none`, no "kuch gadbad ho gayi", language switch followed by STT and TTS |
| Collections (mPokket style) | identity "haan boliye" → "haan main hi bol raha hoon" → amount question → payment promise | strict mode asks once explicitly then confirms, scripted disclosure lines unchanged, `policy_scripted_reply` events, call closes on the policy's completion |
| Any bot, end of call | let the bot say goodbye | `end_call_playout_wait` event, last word audible on the recording |

Per call also: `tts_startup_connect held_ms` small and `timed_out=false`;
`stt_keyterms sent=true` on saaras:v4 bots; no `language_unsupported`;
`usage` counters present (billing).

## 7. Rollback, per step

| Step | Rollback |
|---|---|
| 2 pull | `git reset --hard 93199f3 && git sparse-checkout reapply`, chgrp/chmod as in step 2, restart (step 7). The old code does not know the new columns/rows but ignores them: no schema rollback needed for a code-only rollback. |
| 3 pip | `rm -rf env && mv env.bak-<date> env` (the venv backup), restart. |
| 4 migrations | `env/bin/alembic -c backend/alembic.ini downgrade b2d4f6a8c0e2` — only together with a code rollback to 93199f3 (new code on the old schema loses the runtime context on every call). Downgrades drop the catalog rows and the column; a bot moved to bulbul:v4-flash or gpt-6-luna must be moved back first (section 5's generator in reverse is the saved inventory from step 0). |
| 5 Phase 3 SQL | the `-- rb|` lines in `scratchpad/phase3_deploy.sql` are the inverse of each statement: `sed -n 's/^-- rb\| //p' scratchpad/phase3_deploy.sql` — run only the lines for the statements that were applied on prod (price rows by prod's ids). |
| 6 Redis | nothing to undo; clear again after any rollback SQL. |
| 7 restart | restart again after whichever rollback above. |
| 8 frontend | `mv dist dist-<merged SHA> && mv dist-prev-<date> dist` (no restart). |
| 9 model migration | `UPDATE voice_bot_settings SET llm_model = '<old>' WHERE bot_id = … AND llm_model = 'gpt-6-luna'` per bot from the step-0 inventory (keep that output); presets likewise; a reactivated catalog row needs `status = 'active'`. The old models keep working until their shutdown dates, so this rollback is only valid before 2026-12-11 (gpt-5-mini) / 2027-01-14 (the rest). |
| Phase 4 switches | `scratchpad/phase4_settings_on_rollback.sql` pattern (JSON_REMOVE of the four keys), Redis clear. |

## 8. After the rollout

`env/bin/python scratchpad/phase0_metrics.py --days 1` the next day (prod
Mongo URI as above): decision p50 ~1.4 s on gpt-6-luna fast with few
timeouts at 2.5 s, warm-up ok on every call, `tts_cold_connect` ~0, endpoint
~150-250 ms on Smart Turn calls, the Phase 4 rows empty until their switches
are on. Keep `env.bak-*`, `dist-prev-*` and the mysqldump for a week.
