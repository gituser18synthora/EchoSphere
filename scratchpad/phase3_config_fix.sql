-- Phase 3 config fix for the DEMO LIVE database: voice_bot @ 192.168.60.132 (MySQL 8.0).
-- Prepared 2026-10-26 from read-only lookups on live (state captured 16:xx IST, last
-- write 15:46:14). Not executed by Claude.
--
-- (1) Five bots back from bulbul:v4-flash to bulbul:v3 — the bot-level model, the three
--     voice columns that hold the bot's voice (voice_bot_settings.tts_voice / .voice_id,
--     voice_bots.voice_id) and every language_voice_map entry. Each v4-flash persona goes
--     back to the bulbul:v3 voice it was derived from (reviewed table
--     shared/providers/tts/sarvam_voices.py V3_TO_V4_PERSONA):
--       vp-sv4-shubh_hi_customer          -> vp-sv-shubh
--       vp-sv4-ashutosh_hi_conversational -> vp-sv-ashutosh
--       vp-sv4-kavya_hi_conversational    -> vp-sv-kavya
--     tts_settings stay as they are (v3 and v4-flash share one parameter schema).
-- (2) reply_service_tier=fast + reply_reasoning_effort=lowest for bot_ab466ddf3f19 and
--     Manappuram (both on gpt-6-luna without them since their 15:34 / 15:46 Studio saves).
--
-- Every statement only touches rows still in the captured state, so a second run changes
-- 0 rows and a concurrent Studio edit is never overwritten. The voice statements apply
-- only while all three target bulbul:v3 voices exist and are active (a map entry has no
-- foreign key to fall back on), so a missing one moves no bot at all. The updates run in
-- one transaction: any error ends the client before COMMIT and nothing is applied.
--
-- Run:      mysql -h 192.168.60.132 -u webuser -p voice_bot < scratchpad/phase3_config_fix.sql
-- Then clear the five bots' cached configs (the API does this on a Studio save, SQL
-- does not; otherwise they apply within 5 minutes):
--   for b in bot_59a84478f155 bot_44a50e8c4146 bot_450a4f7b4832 bot_ab466ddf3f19 bot_f49776262f1a; do redis-cli --scan --pattern "botcfg:*$b*" | xargs -r redis-cli del; done
-- Rollback: see the ROLLBACK block at the bottom (one command extracts and runs it).
--
-- Bots (captured on live 2026-10-26):
--   bot_59a84478f155  Zepto MDND Support  en-IN shubh persona, hi-IN ashutosh persona
--   bot_44a50e8c4146  Jeeves              en-IN / hi-IN shubh persona
--   bot_450a4f7b4832  Haier               en-IN / hi-IN kavya persona
--   bot_ab466ddf3f19  Outbound            en-IN / hi-IN shubh persona
--   bot_f49776262f1a  Manappuram          en-IN / hi-IN kavya persona, ml-IN already bulbul:v3 kavya
--   all five: bot-level bulbul:v4-flash / vp-sv4-shubh_hi_customer in all three voice columns


-- Pre-check (read-only): the three bulbul:v3 voices the bots go back to (expect 3 rows).
SELECT id, status, CAST(model_codes AS CHAR) AS model_codes
  FROM voice_profiles WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya') AND is_deleted = 0;

START TRANSACTION;

-- (1a) Bot-level model and voice.
UPDATE voice_bot_settings
   SET tts_model = 'bulbul:v3',
       tts_voice = 'vp-sv-shubh',
       voice_id = CASE WHEN voice_id = 'vp-sv4-shubh_hi_customer' THEN 'vp-sv-shubh' ELSE voice_id END,
       updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND tts_provider = 'sarvam' AND tts_model = 'bulbul:v4-flash'
   AND tts_voice = 'vp-sv4-shubh_hi_customer'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;

UPDATE voice_bots
   SET voice_id = 'vp-sv-shubh', updated_at = NOW()
 WHERE id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
              'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND voice_id = 'vp-sv4-shubh_hi_customer'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;


-- (1b) language_voice_map entries: one statement per (locale, persona); each moves an
--      entry only while it is still that v4-flash persona.
UPDATE voice_bot_settings
   SET language_voice_map = JSON_SET(language_voice_map,
         '$."hi-IN".model', 'bulbul:v3', '$."hi-IN".voice', 'vp-sv-shubh'), updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."hi-IN".model')) = 'bulbul:v4-flash'
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."hi-IN".voice')) = 'vp-sv4-shubh_hi_customer'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;

UPDATE voice_bot_settings
   SET language_voice_map = JSON_SET(language_voice_map,
         '$."hi-IN".model', 'bulbul:v3', '$."hi-IN".voice', 'vp-sv-ashutosh'), updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."hi-IN".model')) = 'bulbul:v4-flash'
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."hi-IN".voice')) = 'vp-sv4-ashutosh_hi_conversational'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;

UPDATE voice_bot_settings
   SET language_voice_map = JSON_SET(language_voice_map,
         '$."hi-IN".model', 'bulbul:v3', '$."hi-IN".voice', 'vp-sv-kavya'), updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."hi-IN".model')) = 'bulbul:v4-flash'
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."hi-IN".voice')) = 'vp-sv4-kavya_hi_conversational'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;

UPDATE voice_bot_settings
   SET language_voice_map = JSON_SET(language_voice_map,
         '$."en-IN".model', 'bulbul:v3', '$."en-IN".voice', 'vp-sv-shubh'), updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."en-IN".model')) = 'bulbul:v4-flash'
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."en-IN".voice')) = 'vp-sv4-shubh_hi_customer'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;

UPDATE voice_bot_settings
   SET language_voice_map = JSON_SET(language_voice_map,
         '$."en-IN".model', 'bulbul:v3', '$."en-IN".voice', 'vp-sv-kavya'), updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."en-IN".model')) = 'bulbul:v4-flash'
   AND JSON_UNQUOTE(JSON_EXTRACT(language_voice_map, '$."en-IN".voice')) = 'vp-sv4-kavya_hi_conversational'
   AND (SELECT COUNT(*) FROM voice_profiles
         WHERE id IN ('vp-sv-shubh', 'vp-sv-ashutosh', 'vp-sv-kavya')
           AND status = 'active' AND is_deleted = 0) = 3;


-- (2) Reply options on the fast tier, lowest reasoning effort ("none" on gpt-6-luna).
UPDATE voice_bot_settings
   SET llm_settings = JSON_SET(COALESCE(llm_settings, JSON_OBJECT()),
                               '$.reply_service_tier', 'fast',
                               '$.reply_reasoning_effort', 'lowest'),
       updated_at = NOW()
 WHERE bot_id IN ('bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND NOT (JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_service_tier')) <=> 'fast'
            AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_reasoning_effort')) <=> 'lowest');

COMMIT;


-- Verification (read-only): no v4-flash left on these bots; reply options in place.
SELECT s.bot_id, s.tts_model, s.tts_voice, s.voice_id, b.voice_id AS bot_voice_id,
       CAST(s.language_voice_map AS CHAR) AS language_voice_map,
       JSON_UNQUOTE(JSON_EXTRACT(s.llm_settings, '$.reply_service_tier')) AS reply_tier,
       JSON_UNQUOTE(JSON_EXTRACT(s.llm_settings, '$.reply_reasoning_effort')) AS reply_effort
  FROM voice_bot_settings s JOIN voice_bots b ON b.id = s.bot_id
 WHERE s.bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                    'bot_ab466ddf3f19', 'bot_f49776262f1a');
SELECT COUNT(*) AS v4_flash_references_left
  FROM voice_bot_settings s JOIN voice_bots b ON b.id = s.bot_id
 WHERE s.bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                    'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND (s.tts_model = 'bulbul:v4-flash' OR s.tts_voice LIKE 'vp-sv4-%' OR s.voice_id LIKE 'vp-sv4-%'
        OR b.voice_id LIKE 'vp-sv4-%' OR CAST(s.language_voice_map AS CHAR) LIKE '%v4-flash%'
        OR CAST(s.language_voice_map AS CHAR) LIKE '%vp-sv4-%');


-- >>> ROLLBACK (not run with the file: every line below is a comment).
-- Extract and run it with:
--   sed -n 's/^-- rb| //p' scratchpad/phase3_config_fix.sql | mysql -h 192.168.60.132 -u webuser -p voice_bot
-- then clear the five bots' botcfg Redis keys (line above). It restores the captured
-- state only while the fixed values are still in place, in one transaction.
-- rb| START TRANSACTION;
-- rb| UPDATE voice_bot_settings
-- rb|    SET llm_settings = JSON_REMOVE(llm_settings, '$.reply_service_tier', '$.reply_reasoning_effort'),
-- rb|        updated_at = NOW()
-- rb|  WHERE bot_id IN ('bot_ab466ddf3f19', 'bot_f49776262f1a')
-- rb|    AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_service_tier')) = 'fast'
-- rb|    AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_reasoning_effort')) = 'lowest';
-- rb| UPDATE voice_bot_settings
-- rb|    SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-shubh_hi_customer", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-ashutosh_hi_conversational", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
-- rb|  WHERE bot_id = 'bot_59a84478f155'
-- rb|    AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-shubh", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-ashutosh", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
-- rb| UPDATE voice_bot_settings
-- rb|    SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-shubh_hi_customer", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-shubh_hi_customer", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
-- rb|  WHERE bot_id IN ('bot_44a50e8c4146', 'bot_ab466ddf3f19')
-- rb|    AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-shubh", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-shubh", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
-- rb| UPDATE voice_bot_settings
-- rb|    SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
-- rb|  WHERE bot_id = 'bot_450a4f7b4832'
-- rb|    AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
-- rb| UPDATE voice_bot_settings
-- rb|    SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "ml-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
-- rb|  WHERE bot_id = 'bot_f49776262f1a'
-- rb|    AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "ml-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
-- rb| UPDATE voice_bots SET voice_id = 'vp-sv4-shubh_hi_customer', updated_at = NOW()
-- rb|  WHERE id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
-- rb|               'bot_ab466ddf3f19', 'bot_f49776262f1a')
-- rb|    AND voice_id = 'vp-sv-shubh';
-- rb| UPDATE voice_bot_settings
-- rb|    SET tts_model = 'bulbul:v4-flash', tts_voice = 'vp-sv4-shubh_hi_customer',
-- rb|        voice_id = CASE WHEN voice_id = 'vp-sv-shubh' THEN 'vp-sv4-shubh_hi_customer' ELSE voice_id END,
-- rb|        updated_at = NOW()
-- rb|  WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
-- rb|                   'bot_ab466ddf3f19', 'bot_f49776262f1a')
-- rb|    AND tts_provider = 'sarvam' AND tts_model = 'bulbul:v3' AND tts_voice = 'vp-sv-shubh';
-- rb| COMMIT;
-- <<< ROLLBACK
