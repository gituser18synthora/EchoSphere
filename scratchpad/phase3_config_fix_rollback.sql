-- UNDOES scratchpad/phase3_config_fix.sql on the DEMO LIVE database (voice_bot @ 192.168.60.132).
-- Do NOT run this after applying the fix unless you want the five bots back on
-- bulbul:v4-flash and the reply options removed: on 2026-10-26 it was run 59 s after the
-- fix (18:02:13 IST) and silently put everything back.
--
-- Restores the exact state captured 2026-10-26 (16:xx IST, unchanged at 18:50), only
-- while the fixed values are still in place (a later Studio edit is never overwritten),
-- in one transaction:
--   mysql -h 192.168.60.132 -u webuser -p voice_bot < scratchpad/phase3_config_fix_rollback.sql
-- then clear the five bots' cached configs:
--   for b in bot_59a84478f155 bot_44a50e8c4146 bot_450a4f7b4832 bot_ab466ddf3f19 bot_f49776262f1a; do redis-cli --scan --pattern "botcfg:*$b*" | xargs -r redis-cli del; done

START TRANSACTION;
UPDATE voice_bot_settings
   SET llm_settings = JSON_REMOVE(llm_settings, '$.reply_service_tier', '$.reply_reasoning_effort'),
       updated_at = NOW()
 WHERE bot_id IN ('bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_service_tier')) = 'fast'
   AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.reply_reasoning_effort')) = 'lowest';
UPDATE voice_bot_settings
   SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-shubh_hi_customer", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-ashutosh_hi_conversational", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
 WHERE bot_id = 'bot_59a84478f155'
   AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-shubh", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-ashutosh", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
UPDATE voice_bot_settings
   SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-shubh_hi_customer", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-shubh_hi_customer", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
 WHERE bot_id IN ('bot_44a50e8c4146', 'bot_ab466ddf3f19')
   AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-shubh", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-shubh", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
UPDATE voice_bot_settings
   SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
 WHERE bot_id = 'bot_450a4f7b4832'
   AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
UPDATE voice_bot_settings
   SET language_voice_map = CAST('{"en-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v4-flash", "voice": "vp-sv4-kavya_hi_conversational", "provider": "sarvam"}, "ml-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON), updated_at = NOW()
 WHERE bot_id = 'bot_f49776262f1a'
   AND language_voice_map = CAST('{"en-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "hi-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "ml-IN": {"model": "bulbul:v3", "voice": "vp-sv-kavya", "provider": "sarvam"}, "default": "hi-IN"}' AS JSON);
UPDATE voice_bots SET voice_id = 'vp-sv4-shubh_hi_customer', updated_at = NOW()
 WHERE id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
              'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND voice_id = 'vp-sv-shubh';
UPDATE voice_bot_settings
   SET tts_model = 'bulbul:v4-flash', tts_voice = 'vp-sv4-shubh_hi_customer',
       voice_id = CASE WHEN voice_id = 'vp-sv-shubh' THEN 'vp-sv4-shubh_hi_customer' ELSE voice_id END,
       updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832',
                  'bot_ab466ddf3f19', 'bot_f49776262f1a')
   AND tts_provider = 'sarvam' AND tts_model = 'bulbul:v3' AND tts_voice = 'vp-sv-shubh';
COMMIT;
