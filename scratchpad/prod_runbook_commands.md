# Prod rollout: the commands, in order

Companion to `docs/PROD_ROLLOUT.md` (read that first: every step's reasoning,
expectations and rollback live there). Every line here runs on **prod**
(`aurexion-voicebot`, `/var/www/html/EchoSphere`, services as user
`echosphere`) unless marked *local*. Replace `<merged SHA>` with the `main`
tip you deploy. `$MYSQL` stands for `mysql -h 127.0.0.1 -u webuser -p voice_bot`
(the prod DB user is `webuser`).

Prod facts from the read-only step 0 (2026-10-30): HEAD `93199f3`, alembic
`b2d4f6a8c0e2` / `b2c4e6f8a0d1`, 0 active sessions; four published bots, all
on gpt-4o-mini / saaras:v3 / bulbul:v3 with no decision-model override —
`bot_44a50e8c4146` (Customer Support = Jeeves), `bot_4b8d6fe95cb1` (Zepto OB),
`bot_59770d4c05a1` (Zepto MDND copy), `bot_59a84478f155` (Zepto MDND); no
loudness rows; no gpt-6-luna catalog row yet (gpt-4o-mini default, gpt-5.6-*
rows inactive); `provider_pricing` has `unit_price` / `selling_price` columns
and saaras:v4 is already active at ₹9/hour (no price fix needed).

## Before anything: reset demo (and every clone) to the rewritten branch

`voice-improvements` was rewritten on 2026-10-30 (the database dumps were
purged from its history) and force-pushed: every branch commit after 2026-07
has a new id. Demo runs the old `072c32aa8` (now `b169069c3`); nothing on
disk changes until the reset, and the services keep the old code until the
restart. The same reset applies to any developer clone.

```bash
# demo (echosphere-live), as root
cd /var/www/html/EchoSphere && git status --porcelain       # expect nothing
git fetch origin && git reset --hard origin/voice-improvements
git rev-parse --short HEAD
curl -s localhost:9002/health                                 # wait for active_sessions: 0
systemctl restart echosphere-api echosphere-voice echosphere-gateway
# any developer clone
git fetch origin && git checkout voice-improvements && git reset --hard origin/voice-improvements
```

## 0. Read-only first (prod) — done 2026-10-30, facts above

```bash
cd /var/www/html/EchoSphere
git status --porcelain                              # expect: nothing
git rev-parse --short HEAD                          # 93199f3
env/bin/alembic -c backend/alembic.ini current      # b2d4f6a8c0e2
env/bin/alembic -c backend/alembic_pg.ini current   # b2c4e6f8a0d1
curl -s localhost:9002/health                       # active_sessions
grep -E '^(STT_MODEL|TTS_MODEL|LLM_MODEL|ORCHESTRATION_MODEL|ORCHESTRATION_SERVICE_TIER)=' .env   # a pin here overrides the code defaults
```

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
SELECT id, model_code, unit_price, selling_price, effective_from, status
  FROM provider_pricing WHERE model_code IN ('saaras:v3', 'saaras:v4');
```

## 1. Preconditions (*local*) — done 2026-10-30

Tree clean, one alembic head per DB (`4d8b2f6a9c1e`, `b2c4e6f8a0d1`), full
`tests/unit` green on the merged tree.

## 2. Merge (*local*) — done 2026-10-30

`origin/main` had two commits the branch lacked (no file changes), so the
merge is a merge commit: `e428f2fa6c` (parents `5565e8b7` origin/main and
`8d64dd70` voice-improvements), no conflicts, merged tree identical to the
branch's. `<merged SHA>` below is the `main` tip pushed after it.

```bash
git fetch origin main && git log --oneline -3 origin/main      # the tip to deploy
```

## 3. Backups (prod, root)

```bash
cd /var/www/html/EchoSphere
cp -a env env.bak-$(date +%Y%m%d)
mkdir -p /root/echosphere-prep
mysqldump -h 127.0.0.1 -u webuser -p --single-transaction voice_bot > /root/echosphere-prep/voice_bot-$(date +%Y%m%d).sql
git rev-parse HEAD > /root/echosphere-prep/pre-deploy.txt                                  # 93199f3…
env/bin/alembic -c backend/alembic.ini current >> /root/echosphere-prep/pre-deploy.txt      # b2d4f6a8c0e2
```

## 4. Pull the code (prod, root; services keep running the old code)

`reset --hard`, not `--ff-only`: prod's `93199f3` is not an ancestor of the
rewritten branch. It touches tracked files only; `env/`, `.env` and `dist*`
are untracked and stay.

```bash
cd /var/www/html/EchoSphere && umask 022
git fetch --no-tags origin main </dev/null && git reset --hard <merged SHA> && git sparse-checkout reapply
chgrp -R echosphere . && chmod -R g+rX,o-rwx . && chmod 640 .env
git rev-parse --short HEAD                                # = <merged SHA>
git status --porcelain                                    # expect nothing
```

## 5. Dependencies (prod)

```bash
env/bin/pip install -r requirements.txt && env/bin/pip check
env/bin/pip show pipecat-ai | grep -i version              # 1.13.0
```

## 6. Migrations, BEFORE any restart (prod)

`d3f5b7c9e1a2` adds the gpt-6-luna catalog row prod does not have; without it
the decision engine falls back to the conversation model.

```bash
env/bin/alembic -c backend/alembic.ini upgrade head        # b2d4f6a8c0e2 -> ... -> 4d8b2f6a9c1e
env/bin/alembic -c backend/alembic_pg.ini upgrade head     # no-op, stays b2c4e6f8a0d1
env/bin/alembic -c backend/alembic.ini current             # 4d8b2f6a9c1e
```

```sql
SELECT code, status, is_default FROM provider_models
 WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-6-luna';   -- expect one active row
```

## 7. Phase 3 catalog SQL (prod; the two generic statements only)

No price fix on prod: saaras:v4 is already active at ₹9/hour. Both statements
are guarded and change nothing if already so.

```sql
UPDATE provider_models SET is_default = (code = 'saaras:v4'), updated_at = NOW()
 WHERE provider_code = 'sarvam' AND capability = 'stt' AND code IN ('saaras:v3', 'saaras:v4')
   AND is_default <> (code = 'saaras:v4');
UPDATE provider_models SET status = 'inactive', updated_at = NOW()
 WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-4.1-nano' AND status = 'active';
```

## 8. Redis, then restart (prod)

```bash
redis-cli -p 6380 -a "$REDIS_PASSWORD" --scan --pattern 'botcfg:*' | xargs -r redis-cli -p 6380 -a "$REDIS_PASSWORD" del
curl -s localhost:9002/health                              # wait for active_sessions: 0
systemctl restart echosphere-api echosphere-voice echosphere-mcp
systemctl restart echosphere-gateway                        # only if telephony is enabled on prod
systemctl status echosphere-api echosphere-voice echosphere-mcp echosphere-gateway --no-pager | grep -E "Active|restarts"
curl -s localhost:9001/api/health/ready
journalctl -u echosphere-api -u echosphere-voice --since -5m --no-pager | grep -i -E "error|traceback"   # expect nothing
```

## 9. Frontend (prod, root, no restart)

```bash
cd /var/www/html/EchoSphere && umask 022
corepack pnpm exec tsc --noEmit && corepack pnpm exec vite build --outDir dist-next --emptyOutDir
chgrp -R echosphere dist-next && chmod -R u=rwX,g=rX,o=rX dist-next
setfacl -R -m u:www-data:rX dist-next && setfacl -R -d -m u:www-data:rX dist-next
mv dist dist-prev-$(date +%Y%m%d) && mv dist-next dist
curl -s https://echosphere.aurexion.ai/ | grep -o 'index-[A-Za-z0-9_-]*\.js'     # a new bundle name
```

## 10. Per-bot config that the code does not carry (prod)

### 10a. The four published bots: gpt-4o-mini → gpt-6-luna, fast reply tier, lowest effort

Guarded on the current model, so a second run changes 0 rows. The decision
model needs no row: with no `orchestration_model` override the code default
(gpt-6-luna, fast tier, 2.5 s budget) applies.

```sql
START TRANSACTION;
UPDATE voice_bot_settings
   SET llm_model    = 'gpt-6-luna',
       llm_settings = JSON_SET(COALESCE(llm_settings, JSON_OBJECT()),
                               '$.reply_service_tier', 'fast',
                               '$.reply_reasoning_effort', 'lowest'),
       updated_at   = NOW()
 WHERE bot_id IN ('bot_44a50e8c4146', 'bot_4b8d6fe95cb1', 'bot_59770d4c05a1', 'bot_59a84478f155')
   AND llm_model = 'gpt-4o-mini';                                 -- expect 4 rows
SELECT bot_id, llm_model,
       JSON_EXTRACT(llm_settings, '$.reply_service_tier')     AS tier,
       JSON_EXTRACT(llm_settings, '$.reply_reasoning_effort') AS effort
  FROM voice_bot_settings
 WHERE bot_id IN ('bot_44a50e8c4146', 'bot_4b8d6fe95cb1', 'bot_59770d4c05a1', 'bot_59a84478f155');
COMMIT;
```

```bash
redis-cli -p 6380 -a "$REDIS_PASSWORD" --scan --pattern 'botcfg:*' | xargs -r redis-cli -p 6380 -a "$REDIS_PASSWORD" del
env/bin/python scratchpad/prod_model_migration.py | grep -E "Bots to move|not moved|profile"   # expect "Bots to move: 0"; drafts/presets are listed, move them only if wanted
```

### 10b. Jeeves (bot_44a50e8c4146): structured summary + address capture

The setup scripts write through the API, dry run first; both must be re-run
after any bot import. `04` saves a new workflow version: publish a release
pinning it afterwards. If `04`'s dry run refuses ("workflow lacks the
expected nodes"), prod's Jeeves workflow predates v22: import the current
Jeeves package (the one demo runs, workflow v24 / release v0.1.18) in Studio
first, then run 03 and 04.

```bash
export ECHOSPHERE_API_BASE=https://echosphere.aurexion.ai/api/v1 JEEVES_SETUP_EMAIL=<super admin> JEEVES_SETUP_PASSWORD=<password>
env/bin/python jeeves/setup/03_summary_intents.py            # dry run
env/bin/python jeeves/setup/03_summary_intents.py --apply
env/bin/python jeeves/setup/04_address_capture.py            # dry run
env/bin/python jeeves/setup/04_address_capture.py --apply
```

Phase 4 switches stay off; to enable them for chosen bots use
`scratchpad/phase4_settings_on.sql` with these bot ids (JSON `true` via
`CAST('true' AS JSON)`), then the Redis clear again.

## 11. Smoke tests (prod; one call per bot, runbook section 6)

Jeeves and Zepto OB are scripted workflows; the two MDND bots are the
semantic-extraction flow.

```bash
env/bin/python scratchpad/phase3_first_call.py --bot <id> --mongo-uri 'mongodb://<user>:<pass>@127.0.0.1:27018/?authSource=admin' --db voice_bot
journalctl -u echosphere-gateway --since -10m --no-pager
```

## 12. Rollback (prod; only the step that failed, runbook section 7)

```bash
# code
git reset --hard 93199f3 && git sparse-checkout reapply && chgrp -R echosphere . && chmod -R g+rX,o-rwx . && chmod 640 .env
# venv
rm -rf env && mv env.bak-<date> env
# schema (only together with the code rollback)
env/bin/alembic -c backend/alembic.ini downgrade b2d4f6a8c0e2
# catalog statements of step 7, inverse
#   UPDATE provider_models SET is_default = (code = 'saaras:v3'), updated_at = NOW() WHERE provider_code = 'sarvam' AND capability = 'stt' AND code IN ('saaras:v3', 'saaras:v4');
#   UPDATE provider_models SET status = 'active', updated_at = NOW() WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-4.1-nano';
# frontend
mv dist dist-<merged SHA> && mv dist-prev-<date> dist
# after any of the above
redis-cli -p 6380 -a "$REDIS_PASSWORD" --scan --pattern 'botcfg:*' | xargs -r redis-cli -p 6380 -a "$REDIS_PASSWORD" del
systemctl restart echosphere-api echosphere-voice echosphere-mcp
```

The four bots back to gpt-4o-mini (valid before 2027-01-14 only):

```sql
UPDATE voice_bot_settings
   SET llm_model = 'gpt-4o-mini',
       llm_settings = JSON_REMOVE(llm_settings, '$.reply_service_tier', '$.reply_reasoning_effort'),
       updated_at = NOW()
 WHERE bot_id IN ('bot_44a50e8c4146', 'bot_4b8d6fe95cb1', 'bot_59770d4c05a1', 'bot_59a84478f155')
   AND llm_model = 'gpt-6-luna';
```

## 13. Next day (prod)

```bash
env/bin/python scratchpad/phase0_metrics.py --days 1 --mongo-uri 'mongodb://<user>:<pass>@127.0.0.1:27018/?authSource=admin' --db voice_bot
```
