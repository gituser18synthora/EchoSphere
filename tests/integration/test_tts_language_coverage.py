"""A bot language with no voice mapping of its own must not fail silently.

``resolve_language_engine`` sends any language WITHOUT a per-language override
to the default TTS engine. The voice-settings validator used to check only the
default locale and the languages that do have an override, so a bot could save
[en-IN, ml-IN] on ElevenLabs Flash v2.5 — which speaks neither Malayalam nor
any other Indic language beyond Hindi and Tamil — and then produce a 1008
``unsupported_language`` frame (no audio at all) the first time the caller
switched to Malayalam. The combination is now reported at save time.
"""

import uuid

import pytest
from sqlalchemy import select

from backend.core.provider_catalog import validate_voice_settings
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import BotLanguage, SupportedLanguage, VoiceBot, VoiceBotSetting

pytestmark = pytest.mark.integration

_SUFFIX = uuid.uuid4().hex[:8]


@pytest.fixture()
def elevenlabs_bot():
    """en-IN + ml-IN bot whose default engine is ElevenLabs Flash v2.5."""
    session = get_sessionmaker()()
    previous = {}
    for code in ("en-IN", "ml-IN"):
        row = session.scalar(
            select(SupportedLanguage).where(SupportedLanguage.code == code)
        )
        previous[code] = row.enabled
        row.enabled = True
    bot = VoiceBot(
        id=new_id("bot"), tenant_id="tn-001", name=f"LangCoverage {_SUFFIX}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(bot)
    session.flush()
    session.add(BotLanguage(bot_id=bot.id, language_code="en-IN"))
    session.add(BotLanguage(bot_id=bot.id, language_code="ml-IN"))
    session.add(VoiceBotSetting(
        id=new_id("vbs"), bot_id=bot.id, tenant_id="tn-001",
        stt_provider="sarvam", stt_model="saaras:v3",
        tts_provider="elevenlabs", tts_model="eleven_flash_v2_5",
        tts_voice="vp-el-niraj", llm_provider="mock", llm_model="mock",
        language_voice_map={"default": "en-IN"},
    ))
    session.commit()
    yield bot.id, session
    session.query(BotLanguage).filter(BotLanguage.bot_id == bot.id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == bot.id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == bot.id).delete()
    for code, enabled in previous.items():
        row = session.scalar(
            select(SupportedLanguage).where(SupportedLanguage.code == code)
        )
        row.enabled = enabled
    session.commit()
    session.close()


def _payload(**overrides):
    payload = {
        "stt_provider": "sarvam", "stt_model": "saaras:v3",
        "tts_provider": "elevenlabs", "tts_model": "eleven_flash_v2_5",
        "tts_voice": "vp-el-niraj",
        "language_voice_map": {"default": "en-IN"},
    }
    payload.update(overrides)
    return payload


class TestDefaultEngineLanguageCoverage:
    def test_unmapped_language_the_default_engine_cannot_speak_is_reported(
        self, elevenlabs_bot
    ):
        bot_id, session = elevenlabs_bot
        bot = session.get(VoiceBot, bot_id)
        errors, warnings = validate_voice_settings(session, bot, _payload())
        # A saveable configuration — but the operator is told what live calls
        # will do with it.
        assert errors == []
        coverage = [w for w in warnings if "does not support" in w]
        assert len(coverage) == 1
        # Only the language the engine really cannot speak is named — English
        # is one of Flash v2.5's 32 languages.
        assert "ml-IN" in coverage[0] and "en-IN" not in coverage[0]

    def test_a_per_language_override_clears_the_warning(self, elevenlabs_bot):
        bot_id, session = elevenlabs_bot
        bot = session.get(VoiceBot, bot_id)
        errors, warnings = validate_voice_settings(session, bot, _payload(
            language_voice_map={
                "default": "en-IN",
                "ml-IN": {"provider": "sarvam", "model": "bulbul:v3",
                          "voice": "vp-sv-ritu"},
            },
        ))
        assert errors == []
        assert not [w for w in warnings if "does not support ml-IN" in w]

    def test_a_default_engine_that_speaks_every_language_warns_about_nothing(
        self, elevenlabs_bot
    ):
        bot_id, session = elevenlabs_bot
        bot = session.get(VoiceBot, bot_id)
        errors, warnings = validate_voice_settings(session, bot, _payload(
            tts_provider="sarvam", tts_model="bulbul:v3", tts_voice="vp-sv-shubh",
        ))
        assert errors == []
        assert not [w for w in warnings if "does not support" in w]
