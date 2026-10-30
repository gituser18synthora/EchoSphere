-- Model migration generated 2026-10-30 05:04 UTC from this database's catalog (scratchpad/prod_model_migration.py).
-- Retiring: gpt-5-mini (2026-12-11), gpt-4o-mini (2027-01-14), gpt-4.1-mini (2027-01-14). Target: gpt-6-luna.
-- Bots to move: 21; listed only (drafts etc.): 19; profiles: 3.
--   not moved: bot_0ded44cd4fb6 'Clone Source 16708fae4c (copy 2)' (tn_ae9d817e8fed, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_2271ef9a3b43 'Aurexion pay' (tn_b9214a9ff885, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_27ddab3867ec 'Inbound' (tn_8d62b7342bd6, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_4737132130b6 'outbound (copy)' (tn_8d62b7342bd6, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_577edc063984 'Aurexion Electronics' (tn_9cf55fc3b2f4, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_6e974e1b2f1a 'Lenovo Outbound' (tn_6a2b6c99de33, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_737668f26b18 'Frankfinn Seminar Booking (copy)' (tn_6553beac240d, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_73b47b61c1b7 'first-test' (tn_6bec2a488a0e, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_7cc607395ffd 'connect' (tn_8207602607e1, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_859c34a89a8b 'Clone Source 16708fae4c (copy 4)' (tn_ae9d817e8fed, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_8f410775e3f6 'lenovo inbound' (tn_6a2b6c99de33, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_984b9ddcf3be 'Customer Support outbound Bot' (tn_c34bbf3d1afc, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_a982982170f8 'testing inbound' (tn_6025a118a016, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_b61874fc89c4 'Metropolis Inbound' (tn_9f18fe3b4539, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_b843f6850154 'Yamaha Motor' (tn_da00fc983c81, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_e1383806a215 'Clone Source 16708fae4c (copy 3)' (tn_ae9d817e8fed, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_e34cbb16ef99 'ecom' (tn_cbc81593b333, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_f31843ce1d46 'finance' (tn_2bc96db4567e, draft) llm_model=gpt-4o-mini orchestration_model=None
--   not moved: bot_fce3e641b1da 'Clone Source 16708fae4c (copy)' (tn_ae9d817e8fed, draft) llm_model=gpt-4o-mini orchestration_model=None
START TRANSACTION;
-- bot_059e49443c76 'Frankfinn Seminar Booking' (tn_6553beac240d, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_059e49443c76' AND llm_model = 'gpt-4o-mini';
-- bot_29baa65abeb7 'Yamaha' (tn_3385569203f7, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_29baa65abeb7' AND llm_model = 'gpt-4o-mini';
-- bot_39db9985b7d5 'mPokket Collections – DPD 60-210+ (Recovery)' (tn_22a809aecf66, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_39db9985b7d5' AND llm_model = 'gpt-4o-mini';
-- bot_4b8d6fe95cb1 'Zepto OB Fee Deduction Verification (Outbound)' (tn_04250683f1b3, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_4b8d6fe95cb1' AND llm_model = 'gpt-4o-mini';
-- bot_517e3d6623ca 'Clone Source 6b61e4c1ac' (tn_0a0fa50531d3, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_517e3d6623ca' AND llm_model = 'gpt-4o-mini';
-- bot_59770d4c05a1 'Zepto MDND Support (copy)' (tn_04250683f1b3, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_59770d4c05a1' AND llm_model = 'gpt-4o-mini';
-- bot_6ce40c703bf3 'Mafil Inbound' (tn_c5a290a5e19a, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_6ce40c703bf3' AND llm_model = 'gpt-4o-mini';
-- bot_71194477c0eb 'Honasa Customer Care' (tn_620d5400d462, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_71194477c0eb' AND llm_model = 'gpt-4o-mini';
-- bot_73eeb5185d1f 'Kotak neo' (tn_0b3837e6bb5a, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_73eeb5185d1f' AND llm_model = 'gpt-4o-mini';
-- bot_7ae045e521ef 'ZEE5 Subscription Support' (tn_1597ce811424, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_7ae045e521ef' AND llm_model = 'gpt-4o-mini';
-- bot_7ed9c825644f 'mPokket Collections – DPD 30-60 (Escalation)' (tn_22a809aecf66, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_7ed9c825644f' AND llm_model = 'gpt-4o-mini';
-- bot_80487d7ce2e9 'Bot-Dev' (tn_22a809aecf66, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_80487d7ce2e9' AND llm_model = 'gpt-4o-mini';
-- bot_8326bfad57d3 'Customer Support' (tn_ff6be580b6d9, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_8326bfad57d3' AND llm_model = 'gpt-4o-mini';
-- bot_ac634648c152 'au-bank' (tn_b8897f32d4aa, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_ac634648c152' AND llm_model = 'gpt-4o-mini';
-- bot_ad8bfb391b91 'Inbound' (tn_12fd8a955be8, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_ad8bfb391b91' AND llm_model = 'gpt-4o-mini';
-- bot_b97b33667066 'mPokket Collections – DPD 8-30 (Follow-Up)' (tn_22a809aecf66, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_b97b33667066' AND llm_model = 'gpt-4o-mini';
-- bot_d0679769048d 'Clone Source 16708fae4c' (tn_ae9d817e8fed, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_d0679769048d' AND llm_model = 'gpt-4o-mini';
-- bot_e0f3fa505ed9 'Customer Support' (tn_9727e4140dc1, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_e0f3fa505ed9' AND llm_model = 'gpt-4o-mini';
-- bot_e8cf0b05bb79 'OYO Booking Support' (tn_de5cc992b1e9, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_e8cf0b05bb79' AND llm_model = 'gpt-4o-mini';
-- bot_eb128ba914eb 'Aurexion KYC Assist' (tn_c4f995308ad8, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_eb128ba914eb' AND llm_model = 'gpt-4o-mini';
-- bot_fb88841de0f7 'mPokket Collections – 0 to 07 (Male – Aditya)' (tn_22a809aecf66, published)
UPDATE voice_bot_settings SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE bot_id = 'bot_fb88841de0f7' AND llm_model = 'gpt-4o-mini';
UPDATE ai_config_profiles SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE id = 'aip_0c206dae0a0d' AND llm_model = 'gpt-4o-mini';  -- profile low_cost
UPDATE ai_config_profiles SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE id = 'aip_90a2e0a355dd' AND llm_model = 'gpt-4o-mini';  -- profile balanced
UPDATE ai_config_profiles SET llm_model = 'gpt-6-luna', updated_at = NOW() WHERE id = 'aip_96c611129cc4' AND llm_model = 'gpt-4o-mini';  -- profile low_latency
COMMIT;
-- After every bot above is moved (re-run this script: 0 bots), retire the catalog rows:
-- UPDATE provider_models SET status = 'inactive', updated_at = NOW() WHERE id = 'pm_0ab610590732' AND code = 'gpt-4.1-mini' AND status = 'active';
-- UPDATE provider_models SET status = 'inactive', updated_at = NOW() WHERE id = 'pm_d4e06022f335' AND code = 'gpt-4o-mini' AND status = 'active';
-- UPDATE provider_models SET status = 'inactive', updated_at = NOW() WHERE id = 'pm_fa1bc960e379' AND code = 'gpt-5-mini' AND status = 'active';
-- Then clear the moved bots' cached configs (see the docstring) — SQL does not invalidate the Redis cache.
