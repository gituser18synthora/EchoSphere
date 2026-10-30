# Prod rollout: the commands, in order

Companion to `docs/PROD_ROLLOUT.md` (read that first: every step's reasoning,
expectations and rollback live there; section numbers below match it). Every
line here runs on **prod** (`aurexion-voicebot`, `/var/www/html/EchoSphere`,
services as user `echosphere`) unless marked *local*. Nothing here has been run
against prod. Replace `<merged SHA>` once step 2 is done; `$MYSQL` stands for
`mysql -h 127.0.0.1 -u echosphere_prod -p voice_bot`.

## 0. Read-only first (prod)

```bash
cd /var/www/html/EchoSphere
git status --porcelain                              # expect: nothing
git rev-parse --short HEAD                          # expect 93199f3
env/bin/alembic -c backend/alembic.ini current      # expect b2d4f6a8c0e2
env/bin/alembic -c backend/alembic_pg.ini current   # expect b2c4e6f8a0d1
env/bin/alembic -c backend/alembic.ini heads        # one head
curl -s localhost:9002/health                       # active_sessions
grep -E '^(STT_MODEL|TTS_MODEL|LLM_MODEL|ORCHESTRATION_MODEL|ORCHESTRATION_SERVICE_TIER)=' .env
```

```sql
-- model inventory (keep the output: it is the step-9 rollback list)
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
-- loudness: expect no rows
SELECT s.bot_id, b.name, b.status, s.tts_model,
       JSON_EXTRACT(s.tts_settings, '$.loudness') AS loudness, s.language_voice_map
  FROM voice_bot_settings s JOIN voice_bots b ON b.id = s.bot_id
 WHERE b.is_deleted = 0
   AND JSON_EXTRACT(s.tts_settings, '$.loudness') IS NOT NULL
   AND JSON_EXTRACT(s.tts_settings, '$.loudness') NOT IN (1, 1.0)
   AND (s.tts_model IN ('bulbul:v3', 'bulbul:v4-flash')
        OR s.language_voice_map LIKE '%bulbul:v3%' OR s.language_voice_map LIKE '%bulbul:v4-flash%');
-- saaras:v4 price row (step 5 decides on this)
SELECT id, model_code, price, effective_from, status FROM provider_pricing WHERE model_code IN ('saaras:v3', 'saaras:v4');
-- Jeeves present on prod? (decides step 10)
SELECT id, name, status FROM voice_bots WHERE id = 'bot_44a50e8c4146' AND is_deleted = 0;
```

## 1. Preconditions (*local*)

```bash
git status --porcelain                                   # nothing (commit or stash other sessions' work)
env/bin/alembic -c backend/alembic.ini heads             # exactly 4d8b2f6a9c1e (head)
env/bin/alembic -c backend/alembic_pg.ini heads          # exactly b2c4e6f8a0d1 (head)
env/bin/python -m pytest tests/unit -q                   # green (~10 min)
```

## 2. Merge (*local*)

```bash
git checkout main && git merge --ff-only voice-improvements && git push origin main
git rev-parse --short main                               # = <merged SHA>
git checkout voice-improvements
```

## 3. Backups (prod, root)

```bash
cd /var/www/html/EchoSphere
cp -a env env.bak-$(date +%Y%m%d)
mkdir -p /root/echosphere-prep
mysqldump -h 127.0.0.1 -u echosphere_prod -p --single-transaction voice_bot > /root/echosphere-prep/voice_bot-$(date +%Y%m%d).sql
git rev-parse HEAD > /root/echosphere-prep/pre-deploy-sha.txt
env/bin/alembic -c backend/alembic.ini current >> /root/echosphere-prep/pre-deploy-sha.txt
```

## 4. Pull the code (prod, root; services keep running the old code)

```bash
cd /var/www/html/EchoSphere && umask 022
git fetch --no-tags origin main </dev/null && git merge --ff-only <merged SHA> && git sparse-checkout reapply
chgrp -R echosphere . && chmod -R g+rX,o-rwx . && chmod 640 .env
git rev-parse --short HEAD                                # = <merged SHA>
```

## 5. Dependencies (prod)

```bash
env/bin/pip install -r requirements.txt && env/bin/pip check
env/bin/pip show pipecat-ai | grep -i version              # 1.13.0
```

## 6. Migrations, BEFORE any restart (prod)

```bash
env/bin/alembic -c backend/alembic.ini upgrade head        # b2d4f6a8c0e2 -> ... -> 4d8b2f6a9c1e
env/bin/alembic -c backend/alembic_pg.ini upgrade head     # no-op, stays b2c4e6f8a0d1
env/bin/alembic -c backend/alembic.ini current             # 4d8b2f6a9c1e
```

## 7. Phase 3 SQL (prod; the generic statements only)

```sql
UPDATE provider_models SET is_default = (code = 'saaras:v4'), updated_at = NOW()
 WHERE provider_code = 'sarvam' AND capability = 'stt' AND code IN ('saaras:v3', 'saaras:v4')
   AND is_default <> (code = 'saaras:v4');
UPDATE provider_models SET status = 'inactive', updated_at = NOW()
 WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-4.1-nano' AND status = 'active';
```

Only if step 0 showed the seed's ₹30/h saaras:v4 row: statement (1) of
`scratchpad/phase3_deploy.sql` with prod's row id in place of
`ppr_d8261a596a86`; statement (b) only if prod's saaras:v3 row has
`effective_from > NOW()`.

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

Model migration (bots on gpt-5-mini / gpt-4o-mini / gpt-4.1-mini):

```bash
env/bin/python scratchpad/prod_model_migration.py > /tmp/model_migration.sql   # SELECTs only; inspect the file
$MYSQL < /tmp/model_migration.sql
redis-cli -p 6380 -a "$REDIS_PASSWORD" --scan --pattern 'botcfg:*' | xargs -r redis-cli -p 6380 -a "$REDIS_PASSWORD" del
env/bin/python scratchpad/prod_model_migration.py | grep "Bots to move"          # expect 0
```

Jeeves structured summary + address capture, only if step 0 found
`bot_44a50e8c4146` on prod (the setup scripts write through the API, dry run
first; both must be re-run after any bot import):

```bash
export ECHOSPHERE_API_BASE=https://echosphere.aurexion.ai/api/v1 JEEVES_SETUP_EMAIL=<super admin> JEEVES_SETUP_PASSWORD=<password>
env/bin/python jeeves/setup/03_summary_intents.py            # dry run
env/bin/python jeeves/setup/03_summary_intents.py --apply
env/bin/python jeeves/setup/04_address_capture.py            # dry run
env/bin/python jeeves/setup/04_address_capture.py --apply    # saves a new workflow version: publish a release pinning it
```

Phase 4 switches stay off; to enable them for chosen bots use
`scratchpad/phase4_settings_on.sql` with prod's bot ids (JSON `true` via
`CAST('true' AS JSON)`), then the Redis clear again.

## 11. Smoke tests (prod; one call per bot type, see runbook section 6)

```bash
env/bin/python scratchpad/phase3_first_call.py --bot <id> --mongo-uri 'mongodb://<user>:<pass>@127.0.0.1:27018/?authSource=admin' --db voice_bot
journalctl -u echosphere-gateway --since -10m --no-pager
```

## 12. Rollback (prod; only the step that failed, see runbook section 7)

```bash
# code
git reset --hard 93199f3 && git sparse-checkout reapply && chgrp -R echosphere . && chmod -R g+rX,o-rwx . && chmod 640 .env
# venv
rm -rf env && mv env.bak-<date> env
# schema (only together with the code rollback)
env/bin/alembic -c backend/alembic.ini downgrade b2d4f6a8c0e2
# Phase 3 SQL inverses, run only the ones applied
sed -n 's/^-- rb| //p' scratchpad/phase3_deploy.sql
# frontend
mv dist dist-<merged SHA> && mv dist-prev-<date> dist
# after any of the above
redis-cli -p 6380 -a "$REDIS_PASSWORD" --scan --pattern 'botcfg:*' | xargs -r redis-cli -p 6380 -a "$REDIS_PASSWORD" del
systemctl restart echosphere-api echosphere-voice echosphere-mcp
```

Model migration rollback is per bot from the step-0 inventory:
`UPDATE voice_bot_settings SET llm_model = '<old>' WHERE bot_id = '<id>' AND llm_model = 'gpt-6-luna';`
(valid only before the models' shutdown dates).

## 13. Next day (prod)

```bash
env/bin/python scratchpad/phase0_metrics.py --days 1 --mongo-uri 'mongodb://<user>:<pass>@127.0.0.1:27018/?authSource=admin' --db voice_bot
```
