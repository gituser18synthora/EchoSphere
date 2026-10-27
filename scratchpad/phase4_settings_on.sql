-- Phase 4 latency settings ON for the four test bots, DEMO LIVE database: voice_bot @ 192.168.60.132
-- (MySQL 8.0). Prepared 2026-10-27 from a read-only lookup on demo (rows last written 13:43:31 IST,
-- none of the keys below present). Not executed by Claude.
--
-- Turns on, in voice_bot_settings.llm_settings, for exactly these bots:
--   bot_59a84478f155  Zepto MDND Support
--   bot_44a50e8c4146  Jeeves
--   bot_450a4f7b4832  Haier
--   bot_f49776262f1a  Manappuram
-- the keys
--   deterministic_answers = true   (Change 1: short answers to a paused workflow question skip the decision)
--   speculative_reply     = true   (Change 3: the reply LLM starts with the decision)
--   parallel_extraction   = true   (Change 4: MDND fact readers with the decision; streamed grounded rewrite)
--   early_ack_ms          = 800    (Change 5: one short acknowledgement when nothing plays 800 ms after dispatch)
-- decision_fast_output (Change 2) is deliberately NOT set: on the 307-turn replay it moved a third of
-- the decisions and lowered agreement with the live decisions, for ~30 ms.
--
-- The booleans are written as JSON true (CAST('true' AS JSON)): a bare TRUE would store the number 1,
-- which the runtime does not treat as on.
--
-- Guarded: a bot is updated only while none of the four keys exists (the captured state), so a second
-- run changes 0 rows and a later Studio edit of any of them is never overwritten. One transaction: an
-- error ends the client before COMMIT and nothing is applied.
--
-- Run after the code is pulled (no migration; the code before Phase 4 ignores these keys), before the
-- restart:
--   mysql -h 192.168.60.132 -u webuser -p voice_bot < scratchpad/phase4_settings_on.sql
-- Expected: 4 rows changed. Then clear the four bots' cached configs (the API does this on a Studio save,
-- SQL does not; otherwise they apply within 5 minutes):
--   for b in bot_59a84478f155 bot_44a50e8c4146 bot_450a4f7b4832 bot_f49776262f1a; do redis-cli --scan --pattern "botcfg:*$b*" | xargs -r redis-cli del; done

START TRANSACTION;
UPDATE voice_bot_settings
   SET llm_settings = JSON_SET(llm_settings,
                               '$.deterministic_answers', CAST('true' AS JSON),
                               '$.speculative_reply', CAST('true' AS JSON),
                               '$.parallel_extraction', CAST('true' AS JSON),
                               '$.early_ack_ms', 800),
       updated_at = NOW()
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832', 'bot_f49776262f1a')
   AND JSON_TYPE(llm_settings) = 'OBJECT'
   AND JSON_CONTAINS_PATH(llm_settings, 'one',
                          '$.deterministic_answers', '$.speculative_reply',
                          '$.parallel_extraction', '$.early_ack_ms') = 0;
SELECT bot_id,
       JSON_EXTRACT(llm_settings, '$.deterministic_answers') AS deterministic_answers,
       JSON_EXTRACT(llm_settings, '$.speculative_reply') AS speculative_reply,
       JSON_EXTRACT(llm_settings, '$.parallel_extraction') AS parallel_extraction,
       JSON_EXTRACT(llm_settings, '$.early_ack_ms') AS early_ack_ms
  FROM voice_bot_settings
 WHERE bot_id IN ('bot_59a84478f155', 'bot_44a50e8c4146', 'bot_450a4f7b4832', 'bot_f49776262f1a');
COMMIT;
