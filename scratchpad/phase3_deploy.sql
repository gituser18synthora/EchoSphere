-- Phase 3 deploy SQL for the DEMO LIVE database: voice_bot @ 192.168.60.132 (MySQL 8.0).
-- Prepared 2026-10-26 from read-only lookups on live. Not executed by Claude.
--
-- Preconditions
--   * code at 5bd9c441f or later on the server (git pull), so the runtime knows
--     gpt-6-luna, reply_service_tier and reply_reasoning_effort;
--   * `env/bin/alembic -c backend/alembic.ini upgrade head` has run: statements (5)
--     and (6) only touch bots when the gpt-6-luna catalog row exists.
--
-- Every statement is idempotent: a second run changes 0 rows.
-- TTS is not touched: every bot stays on bulbul:v3.
--
-- Run:      mysql -h 192.168.60.132 -u webuser -p voice_bot < scratchpad/phase3_deploy.sql
-- Rollback: see the ROLLBACK block at the bottom (one command extracts and runs it).
--
-- Bots (looked up on live 2026-10-26):
--   MDND    bot_59a84478f155  Zepto MDND Support (published)  stt saaras:v3, llm gpt-4o-mini
--   Haier   bot_450a4f7b4832  Customer Support   (published)  stt saaras:v3, llm gpt-4.1
--   Jeeves  bot_44a50e8c4146  Customer Support   (published)  stt saaras:v3, llm gpt-4o-mini
--   mPokket bot_eb5738c9da88  Collections 0-7 (Female, Priya) (published), llm gpt-5-mini
--   mPokket bot_c2453561ef8c  Collections DPD 0-7 (Early Overdue) (published), llm gpt-5-mini


-- (1) saaras:v4 price: the negotiated Rs 9/hour replaces the Rs 30/hour list row.
--     Convention (shared/models/billing_models.py ProviderPricing): retire the old
--     row, add a new one; usage events keep the price they were costed with.
UPDATE provider_pricing
   SET status = 'inactive', updated_at = NOW()
 WHERE provider_code = 'sarvam' AND capability = 'stt' AND model_code = 'saaras:v4'
   AND component = 'audio_seconds' AND status = 'active' AND is_deleted = 0
   AND unit_price <> 9;

INSERT INTO provider_pricing
       (id, provider_code, capability, model_code, component, unit, unit_price,
        currency_code, effective_from, status, sort_order, created_at, updated_at, is_deleted)
SELECT 'ppr_p3_saarasv4_inr9', 'sarvam', 'stt', 'saaras:v4', 'audio_seconds', 'per_hour', 9,
       'INR', '2026-10-06 00:00:00', 'active', 0, NOW(), NOW(), 0
  FROM DUAL
 WHERE NOT EXISTS (SELECT 1 FROM provider_pricing
                    WHERE provider_code = 'sarvam' AND capability = 'stt'
                      AND model_code = 'saaras:v4' AND component = 'audio_seconds'
                      AND status = 'active' AND is_deleted = 0 AND unit_price = 9)
   AND NOT EXISTS (SELECT 1 FROM provider_pricing WHERE id = 'ppr_p3_saarasv4_inr9');


-- (2) STT catalog default -> saaras:v4. Affects only what the Studio preselects for
--     NEW bots; bots without an explicit model follow .env STT_MODEL (saaras:v3).
UPDATE provider_models
   SET is_default = (code = 'saaras:v4'), updated_at = NOW()
 WHERE provider_code = 'sarvam' AND capability = 'stt'
   AND code IN ('saaras:v3', 'saaras:v4')
   AND is_default <> (code = 'saaras:v4');


-- (3) saaras:v4 for the three demo test bots only. Every other bot keeps its model.
UPDATE voice_bot_settings
   SET stt_model = 'saaras:v4', updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_450a4f7b4832', 'bot_44a50e8c4146')
   AND stt_provider = 'sarvam' AND stt_model = 'saaras:v3';


-- (4) gpt-4.1-nano: shut down by OpenAI on 2026-10-23. No bot or AI profile uses it.
UPDATE provider_models
   SET status = 'inactive', updated_at = NOW()
 WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-4.1-nano'
   AND status <> 'inactive';


-- (5) Reply model gpt-6-luna on the fast tier, lowest reasoning effort, for the three
--     test bots. These options cover every call on the conversation model: replies,
--     grounded rewrites, the workflow fact extractor (MDND) and intent classification.
UPDATE voice_bot_settings
   SET llm_provider = 'openai',
       llm_model = 'gpt-6-luna',
       llm_settings = JSON_SET(COALESCE(llm_settings, JSON_OBJECT()),
                               '$.reply_service_tier', 'fast',
                               '$.reply_reasoning_effort', 'lowest'),
       updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_450a4f7b4832', 'bot_44a50e8c4146')
   AND EXISTS (SELECT 1 FROM provider_models
                WHERE provider_code = 'openai' AND capability = 'llm'
                  AND code = 'gpt-6-luna' AND status = 'active' AND is_deleted = 0)
   AND NOT (llm_model <=> 'gpt-6-luna'
            AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_service_tier')) <=> 'fast'
            AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_reasoning_effort')) <=> 'lowest');


-- (6) The two published bots still on gpt-5-mini (shutdown 2026-12-11) -> gpt-6-luna.
--     Standard tier as asked; reply_reasoning_effort 'lowest' because gpt-6-luna's
--     default effort (medium) is too slow for a live call.
UPDATE voice_bot_settings
   SET llm_model = 'gpt-6-luna',
       llm_settings = JSON_SET(COALESCE(llm_settings, JSON_OBJECT()),
                               '$.reply_reasoning_effort', 'lowest'),
       updated_at = NOW()
 WHERE bot_id IN ('bot_eb5738c9da88', 'bot_c2453561ef8c')
   AND llm_model = 'gpt-5-mini'
   AND EXISTS (SELECT 1 FROM provider_models
                WHERE provider_code = 'openai' AND capability = 'llm'
                  AND code = 'gpt-6-luna' AND status = 'active' AND is_deleted = 0);


-- OPTIONAL, not requested: remove the leading "-- " to run.
-- (a) The two OYO DRAFT bots also on gpt-5-mini:
-- UPDATE voice_bot_settings
--    SET llm_model = 'gpt-6-luna',
--        llm_settings = JSON_SET(COALESCE(llm_settings, JSON_OBJECT()), '$.reply_reasoning_effort', 'lowest'),
--        updated_at = NOW()
--  WHERE bot_id IN ('bot_99177674902a', 'bot_78b6aa83d94a') AND llm_model = 'gpt-5-mini';
-- (b) Billing defect: the saaras:v3 price row ppr_389f50214bcd was edited on 2026-10-15
--     17:36 to start on 2027-01-30, so every saaras:v3 call since then is
--     'missing_price' (237 usage events). This restores its original start date:
-- UPDATE provider_pricing SET effective_from = '2026-07-27 10:47:26', updated_at = NOW()
--  WHERE id = 'ppr_389f50214bcd' AND effective_from > NOW();


-- Verification (read-only)
SELECT b.id, b.name, s.stt_model, s.llm_model,
       JSON_UNQUOTE(JSON_EXTRACT(s.llm_settings, '$.reply_service_tier')) AS reply_tier,
       JSON_UNQUOTE(JSON_EXTRACT(s.llm_settings, '$.reply_reasoning_effort')) AS reply_effort,
       s.tts_model
  FROM voice_bot_settings s JOIN voice_bots b ON b.id = s.bot_id
 WHERE s.bot_id IN ('bot_59a84478f155', 'bot_450a4f7b4832', 'bot_44a50e8c4146',
                    'bot_eb5738c9da88', 'bot_c2453561ef8c');
SELECT provider_code, capability, code, status, is_default
  FROM provider_models
 WHERE (provider_code = 'sarvam' AND capability = 'stt')
    OR (provider_code = 'openai' AND code IN ('gpt-4.1-nano', 'gpt-6-luna'));
SELECT id, unit_price, status, effective_from
  FROM provider_pricing
 WHERE provider_code = 'sarvam' AND model_code = 'saaras:v4' AND is_deleted = 0;
SELECT stt_model, COUNT(*) AS bots FROM voice_bot_settings GROUP BY stt_model;


-- >>> ROLLBACK (not run with the file: every line below is a comment).
-- Extract and run it with:
--   sed -n 's/^-- rb| //p' scratchpad/phase3_deploy.sql | mysql -h 192.168.60.132 -u webuser -p voice_bot
-- Then clear the botcfg Redis keys of the five bots (see the deploy notes).
-- rb| UPDATE voice_bot_settings
-- rb|    SET llm_model = 'gpt-5-mini',
-- rb|        llm_settings = JSON_REMOVE(llm_settings, '$.reply_reasoning_effort'),
-- rb|        updated_at = NOW()
-- rb|  WHERE bot_id IN ('bot_eb5738c9da88', 'bot_c2453561ef8c') AND llm_model = 'gpt-6-luna';
-- rb| UPDATE voice_bot_settings
-- rb|    SET llm_model = CASE bot_id WHEN 'bot_450a4f7b4832' THEN 'gpt-4.1' ELSE 'gpt-4o-mini' END,
-- rb|        llm_settings = JSON_REMOVE(llm_settings, '$.reply_service_tier', '$.reply_reasoning_effort'),
-- rb|        updated_at = NOW()
-- rb|  WHERE bot_id IN ('bot_59a84478f155', 'bot_450a4f7b4832', 'bot_44a50e8c4146')
-- rb|    AND llm_model = 'gpt-6-luna';
-- rb| UPDATE provider_models SET status = 'active', updated_at = NOW()
-- rb|  WHERE provider_code = 'openai' AND capability = 'llm' AND code = 'gpt-4.1-nano'
-- rb|    AND status <> 'active';
-- rb| UPDATE voice_bot_settings SET stt_model = 'saaras:v3', updated_at = NOW()
-- rb|  WHERE bot_id IN ('bot_59a84478f155', 'bot_450a4f7b4832', 'bot_44a50e8c4146')
-- rb|    AND stt_model = 'saaras:v4';
-- rb| UPDATE provider_models SET is_default = (code = 'saaras:v3'), updated_at = NOW()
-- rb|  WHERE provider_code = 'sarvam' AND capability = 'stt'
-- rb|    AND code IN ('saaras:v3', 'saaras:v4') AND is_default <> (code = 'saaras:v3');
-- rb| DELETE FROM provider_pricing WHERE id = 'ppr_p3_saarasv4_inr9';
-- rb| UPDATE provider_pricing SET status = 'active', updated_at = NOW()
-- rb|  WHERE id = 'ppr_d8261a596a86' AND status <> 'active';
-- <<< ROLLBACK
