"""Bot config resolution: default call language + Sarvam voice wiring.

Regression for the "Sarvam TTS produces no audio" failure: a bot without an
explicit language_voice_map default used to resolve language="en" (bare),
which the Sarvam WS API 422-rejects — the greeting and every reply produced
zero audio. The default language must come from the bot's own configured
languages (bot_languages) and the stored voice-profile id must resolve to the
provider wire speaker code.
"""

import uuid

import pytest

from shared.bot_config import _load_config_sync
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import BotLanguage, VoiceBot, VoiceBotSetting

pytestmark = pytest.mark.integration

_SUFFIX = uuid.uuid4().hex[:8]


@pytest.fixture()
def sarvam_bot():
    """A tenant bot configured for Sarvam TTS with two languages, no map."""
    session = get_sessionmaker()()
    bot = VoiceBot(
        id=new_id("bot"), tenant_id="tn-001", name=f"CfgLang {_SUFFIX}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(bot)
    session.flush()
    # Deliberately inserted out of alphabetical order.
    session.add(BotLanguage(bot_id=bot.id, language_code="hi-IN"))
    session.add(BotLanguage(bot_id=bot.id, language_code="en-IN"))
    vbs = VoiceBotSetting(
        id=new_id("vbs"), bot_id=bot.id, tenant_id="tn-001",
        tts_provider="sarvam", tts_model="bulbul:v3", tts_voice="vp-sv-shubh",
        stt_provider="mock", stt_model="mock", llm_provider="mock", llm_model="mock",
    )
    session.add(vbs)
    session.commit()
    bot_id = bot.id
    yield bot_id, session
    session.query(BotLanguage).filter(BotLanguage.bot_id == bot_id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == bot_id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == bot_id).delete()
    session.commit()
    session.close()


class TestDefaultLanguageResolution:
    def test_language_comes_from_bot_languages_not_bare_en(self, sarvam_bot):
        bot_id, _ = sarvam_bot
        config = _load_config_sync(bot_id, require_published=False)
        # Deterministic: first configured language (alphabetical), never "en".
        assert config.language == "en-IN"
        # The stored voice-profile id resolved to the Sarvam wire speaker code.
        assert config.tts["provider"] == "sarvam"
        assert config.tts["voice"] == "shubh"

    def test_language_voice_map_default_still_wins(self, sarvam_bot):
        bot_id, session = sarvam_bot
        vbs = session.query(VoiceBotSetting).filter(
            VoiceBotSetting.bot_id == bot_id
        ).one()
        vbs.language_voice_map = {"default": "hi-IN"}
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.language == "hi-IN"

    def test_bot_without_languages_keeps_legacy_default(self, sarvam_bot):
        bot_id, session = sarvam_bot
        session.query(BotLanguage).filter(BotLanguage.bot_id == bot_id).delete()
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.language == "en"  # normalized to en-IN by the provider layer


class TestPerLanguageVoiceResolution:
    """Deterministic voice priority: per-language entry → configured default
    voice when it supports the locale → configured fallback engine → warning.
    The user-selected voice is never silently replaced."""

    def test_configured_languages_and_display_names_exposed(self, sarvam_bot):
        bot_id, _ = sarvam_bot
        config = _load_config_sync(bot_id, require_published=False)
        assert config.languages == ["en-IN", "hi-IN"]
        assert config.tts["voice_name"] == "Shubh"

    def test_default_voice_covers_supported_locales_without_map(self, sarvam_bot):
        bot_id, _ = sarvam_bot
        config = _load_config_sync(bot_id, require_published=False)
        # Shubh supports hi-IN and en-IN: no synthetic map entries, no warnings.
        assert config.language_warnings == {}
        assert "hi-IN" not in config.tts["language_map"]
        assert "en-IN" not in config.tts["language_map"]

    def test_per_language_entry_wins_over_default(self, sarvam_bot):
        bot_id, session = sarvam_bot
        vbs = session.query(VoiceBotSetting).filter(
            VoiceBotSetting.bot_id == bot_id
        ).one()
        vbs.language_voice_map = {
            "hi-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": "vp-sv-ritu"},
        }
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        entry = config.tts["language_map"]["hi-IN"]
        assert entry["voice"] == "ritu"  # provider wire code, not the profile id
        assert entry["voice_name"] == "Ritu"

    def test_incompatible_locale_uses_configured_fallback(self, sarvam_bot):
        bot_id, session = sarvam_bot
        # en-US is not in the Sarvam voice's language list; the explicitly
        # configured ElevenLabs fallback voice (language-agnostic) covers it.
        session.add(BotLanguage(bot_id=bot_id, language_code="en-US"))
        vbs = session.query(VoiceBotSetting).filter(
            VoiceBotSetting.bot_id == bot_id
        ).one()
        vbs.fallback_provider = "elevenlabs"
        vbs.fallback_model = "eleven_flash_v2_5"
        vbs.fallback_voice = "vp-el-anvi"
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        entry = config.tts["language_map"]["en-US"]
        assert entry["provider"] == "elevenlabs"
        assert entry["voice_name"] == "Anvi"
        assert config.language_warnings == {}

    def test_uncovered_locale_becomes_warning_never_a_substitute(self, sarvam_bot):
        bot_id, session = sarvam_bot
        session.add(BotLanguage(bot_id=bot_id, language_code="en-US"))
        session.commit()  # no fallback engine configured
        config = _load_config_sync(bot_id, require_published=False)
        assert "en-US" in config.language_warnings
        assert "No compatible voice" in config.language_warnings["en-US"]
        # No invented engine: the locale is simply absent from the map.
        assert "en-US" not in config.tts["language_map"]


class TestModelStreamingResolution:
    """The catalog's realtime-streaming capability rides along in the resolved
    snapshot so the runtime picks the WebSocket router or the segmented REST
    service per model (ElevenLabs eleven_v3 is REST-only)."""

    def test_streaming_model_resolves_true(self, sarvam_bot):
        bot_id, _ = sarvam_bot
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["streaming"] is True  # bulbul:v3 streams

    def test_eleven_v3_resolves_false_and_flash_true(self, sarvam_bot):
        bot_id, session = sarvam_bot
        vbs = session.query(VoiceBotSetting).filter(
            VoiceBotSetting.bot_id == bot_id
        ).one()
        vbs.tts_provider = "elevenlabs"
        vbs.tts_model = "eleven_v3"
        vbs.tts_voice = "vp-el-monika"
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["provider"] == "elevenlabs"
        assert config.tts["model"] == "eleven_v3"
        assert config.tts["streaming"] is False

        vbs.tts_model = "eleven_flash_v2_5"
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["model"] == "eleven_flash_v2_5"
        assert config.tts["streaming"] is True
