-- UNDOES scratchpad/phase4_settings_on.sql on the DEMO LIVE database (voice_bot @ 192.168.60.132).
-- Run this ONLY to turn the Phase 4 latency settings off again for the four test bots.
--
-- Removes deterministic_answers, speculative_reply, parallel_extraction and early_ack_ms from
-- voice_bot_settings.llm_settings of bot_59a84478f155, bot_44a50e8c4146, bot_450a4f7b4832 and
-- bot_f49776262f1a — only while all four still hold the values the ON file wrote (a later Studio
-- edit is never overwritten) — in one transaction. The bots then run exactly as before the ON file
-- (every setting defaults to off).
--
--   mysql -h 192.168.60.132 -u webuser -p voice_bot < scratchpad/phase4_settings_on_rollback.sql
-- then clear the four bots' cached configs:
--   for b in bot_59a84478f155 bot_44a50e8c4146 bot_450a4f7b4832 bot_f49776262f1a; do redis-cli --scan --pattern "botcfg:*$b*" | xargs -r redis-cli del; done

START TRANSACTION;
UPDATE voice_bot_settings
   SET llm_settings = JSON_REMOVE(llm_settings,
                                  '$.deterministic_answers', '$.speculative_reply',
                                  '$.parallel_extraction', '$.early_ack_ms'),
       updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832', 'bot_f49776262f1a')
   AND JSON_EXTRACT(llm_settings, '$.deterministic_answers') = CAST('true' AS JSON)
   AND JSON_EXTRACT(llm_settings, '$.speculative_reply') = CAST('true' AS JSON)
   AND JSON_EXTRACT(llm_settings, '$.parallel_extraction') = CAST('true' AS JSON)
   AND JSON_EXTRACT(llm_settings, '$.early_ack_ms') = 800;
COMMIT;
