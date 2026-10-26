"""Sarvam bulbul:v4-flash: persona speakers, config-time fallback, catalog.

bulbul:v4-flash keeps bulbul:v3's request contract but its speakers are
persona ids and Sarvam rejects a v3 short name with a 400. These tests pin:

- the reviewed v3 -> persona table (same speaker, matching language tag) and
  its loud failure for a (voice, language) pair it does not cover;
- both adapters (WebSocket, REST) sending the persona with v3 parameters, and
  an unmapped voice failing before any socket is opened;
- bot config keeping an uncovered locale on bulbul:v3 with the SAME voice and
  a language warning (never a substitute speaker);
- the catalog seed rows.
"""

import pytest

import shared.bot_config as bot_config
import shared.providers.tts.sarvam_ws as sarvam_ws
from backend.seeds import provider_catalog_seed as catalog_seed
from shared.providers.base import ProviderError
from shared.providers.tts.sarvam_voices import (
    V3_TO_V4_PERSONA,
    V4_DEFAULT_PERSONA,
    NoV4Equivalent,
    is_v4_model,
    is_v4_persona,
    v4_persona_for,
    v4_speaker,
)
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from tests.mock_tts_servers import MockSarvamTTSServer
from tests.unit.test_sarvam_rest_tts_language_kwarg import _NewSdkTTS, _provider
from tests.unit.test_streaming_tts_providers import sarvam_settings


# ── persona table ───────────────────────────────────────────────────────────

class TestPersonaTable:
    def test_v3_voice_maps_to_its_reviewed_persona_per_language(self):
        assert v4_speaker("shubh", "hi-IN") == "shubh_hi_customer"
        assert v4_speaker("Shubh", "en-IN") == "shubh_enhi_companion"
        assert v4_speaker("ashutosh", "hi-IN") == "ashutosh_hi_conversational"
        assert v4_speaker("ritu", "en-IN") == "ritu_en_edtech"
        assert v4_speaker("ritu", "mr-IN") == "ritu_mr_insurance"

    def test_persona_ids_pass_through(self):
        assert v4_speaker("ritu_hi_customer", "en-IN") == "ritu_hi_customer"
        assert is_v4_persona("shubh_enhi_ads")
        assert not is_v4_persona("shubh")

    @pytest.mark.parametrize("voice,language", [
        ("ashutosh", "en-IN"),   # only a Hindi persona exists
        ("kavya", "ml-IN"),      # no Malayalam personas at all
        ("shubh", "od-IN"),      # no Odia personas at all
        ("anushka", "hi-IN"),    # bulbul:v2 speaker, no persona
    ])
    def test_uncovered_pair_fails_loudly(self, voice, language):
        assert v4_persona_for(voice, language) is None
        with pytest.raises(NoV4Equivalent, match=voice):
            v4_speaker(voice, language)

    def test_empty_voice_is_sarvams_default_persona(self):
        assert v4_speaker("", "hi-IN") == V4_DEFAULT_PERSONA == "shubh_enhi_ads"

    def test_every_entry_is_the_same_speaker_in_a_matching_language(self):
        for speaker, row in V3_TO_V4_PERSONA.items():
            for language, persona in row.items():
                name, tag, _style = persona.split("_", 2)
                assert name == speaker, persona
                assert tag in ({language, "enhi"} if language in ("hi", "en") else {language}), persona
                assert is_v4_persona(persona)
        assert not any({"ml", "od"} & set(row) for row in V3_TO_V4_PERSONA.values())

    def test_model_family(self):
        assert is_v4_model("bulbul:v4-flash")
        assert not is_v4_model("bulbul:v3")
        assert not is_v4_model(None)


# ── WebSocket adapter ───────────────────────────────────────────────────────

class TestWebSocketAdapter:
    @pytest.fixture(autouse=True)
    def _no_warm_spare(self, monkeypatch):
        monkeypatch.setattr(sarvam_ws, "_WARM_SPARE", False)

    async def test_v4_flash_sends_the_persona_with_v3_parameters(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings(
                model="bulbul:v4-flash", voice="shubh", language="hi-IN",
                params={"pace": 1.1, "temperature": 0.3, "min_buffer_size": 40, "pitch": 0.2},
            ))
            await provider.connect()
            await provider.close()
        config = server.configs[0]
        assert server.queries[0]["model"] == "bulbul:v4-flash"
        assert config["speaker"] == "shubh_hi_customer"
        assert config["pace"] == 1.1 and config["temperature"] == 0.3
        assert config["enable_preprocessing"] is True
        assert "pitch" not in config  # v2-only

    async def test_v3_keeps_the_short_name(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings(model="bulbul:v3", voice="ashutosh"))
            await provider.connect()
            await provider.close()
        assert server.configs[0]["speaker"] == "ashutosh"

    async def test_unmapped_voice_fails_before_any_socket_opens(self, monkeypatch):
        async with MockSarvamTTSServer() as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            provider = SarvamWebSocketTTSProvider(sarvam_settings(
                model="bulbul:v4-flash", voice="ashutosh", language="en-IN",
            ))
            with pytest.raises(ProviderError) as info:
                await provider.connect()
            await provider.close()
        assert info.value.category == "invalid_input"
        assert server.connections == 0

# ── REST adapter ───────────────────────────────────────────────────────────

class TestRestAdapter:
    async def test_v4_flash_maps_the_persona_and_drops_v2_pitch(self, monkeypatch):
        tts = _NewSdkTTS()
        provider = _provider(monkeypatch, tts, language="hi-IN")
        provider._model = "bulbul:v4-flash"
        provider._params = {"temperature": 0.3, "pitch": 0.2, "loudness": 1.1}
        await provider.synthesize("नमस्ते", voice="kavya", language="hi-IN")
        call = tts.calls[0]
        assert call["model"] == "bulbul:v4-flash"
        assert call["speaker"] == "kavya_hi_conversational"
        assert call["temperature"] == 0.3
        # v4-flash takes loudness (0.1-2.5) like bulbul:v2; pitch is v2-only.
        assert call["loudness"] == 1.1
        assert "pitch" not in call

    async def test_v4_flash_unmapped_voice_raises_invalid_input(self, monkeypatch):
        tts = _NewSdkTTS()
        provider = _provider(monkeypatch, tts, language="en-IN")
        provider._model = "bulbul:v4-flash"
        with pytest.raises(ProviderError) as info:
            await provider.synthesize("Hello", voice="kavya", language="en-IN")
        assert info.value.category == "invalid_input"
        assert tts.calls == []


# ── bot config ─────────────────────────────────────────────────────────────

def _engine(model, voice="ashutosh", **extra):
    return {"provider": "sarvam", "model": model, "voice": voice,
            "voice_name": voice.title(), "voice_gender": "male",
            "api_key_reference": "env:SARVAM_API_KEY", **extra}


class TestConfigFallback:
    def test_uncovered_locale_stays_on_v3_with_the_same_voice(self, monkeypatch):
        monkeypatch.setattr(bot_config, "_engine_allowed", lambda *a, **k: None)
        tts = {**_engine("bulbul:v4-flash", settings={"pace": 1.05}), "language_map": {
            "hi-IN": {**_engine("bulbul:v4-flash"), "params": {}},
            "en-IN": {**_engine("bulbul:v4-flash"), "params": {"pace": 0.9}},
        }}
        warnings: dict = {}
        bot_config._keep_unmapped_v4_locales_on_v3(None, tts, ["hi-IN", "en-IN"], warnings, "bot_x")
        assert tts["language_map"]["hi-IN"]["model"] == "bulbul:v4-flash"
        en = tts["language_map"]["en-IN"]
        assert (en["model"], en["voice"], en["params"]) == ("bulbul:v3", "ashutosh", {"pace": 0.9})
        assert set(warnings) == {"en-IN"} and "ashutosh" in warnings["en-IN"]

    def test_default_engine_locale_gets_a_v3_entry_with_its_settings(self, monkeypatch):
        monkeypatch.setattr(bot_config, "_engine_allowed", lambda *a, **k: None)
        tts = {**_engine("bulbul:v4-flash", voice="kavya", settings={"pace": 1.05}),
               "language_map": {}}
        warnings: dict = {}
        bot_config._keep_unmapped_v4_locales_on_v3(None, tts, ["hi-IN", "ml-IN"], warnings, "bot_x")
        assert "hi-IN" not in tts["language_map"]
        ml = tts["language_map"]["ml-IN"]
        assert (ml["model"], ml["voice"], ml["params"]) == ("bulbul:v3", "kavya", {"pace": 1.05})
        assert set(warnings) == {"ml-IN"}

    def test_v3_unavailable_leaves_a_warning_only(self, monkeypatch):
        monkeypatch.setattr(bot_config, "_engine_allowed", lambda *a, **k: "inactive")
        tts = {**_engine("bulbul:v4-flash"), "language_map": {}}
        warnings: dict = {}
        bot_config._keep_unmapped_v4_locales_on_v3(None, tts, ["en-IN"], warnings, "bot_x")
        assert tts["language_map"] == {}
        assert "will fail" in warnings["en-IN"]

    def test_v3_and_mapped_engines_are_untouched(self, monkeypatch):
        monkeypatch.setattr(bot_config, "_engine_allowed", lambda *a, **k: None)
        tts = {**_engine("bulbul:v3"), "language_map": {"hi-IN": _engine("bulbul:v4-flash", "shubh")}}
        warnings: dict = {}
        bot_config._keep_unmapped_v4_locales_on_v3(None, tts, ["hi-IN", "en-IN"], warnings, "bot_x")
        assert tts["language_map"] == {"hi-IN": _engine("bulbul:v4-flash", "shubh")}
        assert warnings == {}


# ── catalog seed ───────────────────────────────────────────────────────────

def test_catalog_seed_keeps_v3_the_tts_default_and_offers_v4_flash():
    rows = {r[2]: r for r in catalog_seed.PROVIDER_MODELS if r[:2] == ("sarvam", "tts")}
    v4, v3 = rows["bulbul:v4-flash"], rows["bulbul:v3"]
    assert v3[9] is True and v3[10] == "active"
    assert v4[9] is False and v4[10] == "active"
    assert "ml-IN" not in v4[4] and "od-IN" not in v4[4] and "ml-IN" in v3[4]
    # The v3 contract plus loudness, which v4-flash offers and v3 does not.
    assert set(v4[8]) == set(catalog_seed._SARVAM_TTS_V3_SCHEMA) | {"loudness"}
    assert catalog_seed._SARVAM_TTS_V3_SCHEMA["pace"]["help"].startswith("Speech speed")
    assert catalog_seed._PROFILE_REMAP["tts"] == ("sarvam", "bulbul:v3")


# ── catalog migration ──────────────────────────────────────────────────────

def test_v4_flash_migration_round_trip():
    import importlib
    import json

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = importlib.import_module(
        "backend.alembic.versions.c2e4a6b8d0f1_sarvam_bulbul_v4_flash_catalog")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "create table provider_models (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), code varchar(80), display_name varchar(150), description text, "
            "languages text, codecs text, sample_rates text, streaming int, params_schema text, "
            "is_default int, status varchar(20), sort_order int, created_at datetime, "
            "updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table provider_pricing (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), model_code varchar(80), component varchar(40), unit varchar(30), "
            "unit_price numeric, currency_code varchar(3), effective_from datetime, status varchar(20), "
            "sort_order int, created_at datetime, updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table voice_profiles (id varchar(40) primary key, source varchar(20), "
            "name varchar(100), gender varchar(10), languages text, locale varchar(15), "
            "accent varchar(100), styles text, latency_ms int, premium int, sample_text text, "
            "provider varchar(100), provider_voice_id varchar(100), speaking_rate float, "
            "pitch float, model_codes text, provider_settings text, is_default int, "
            "status varchar(20), sort_order int, created_at datetime, updated_at datetime, "
            "is_deleted int default 0)"))
        conn.execute(sa.text("create table currencies (code varchar(3) primary key)"))
        conn.execute(sa.text("insert into currencies (code) values ('INR')"))
        # Long-lived database: bulbul:v3 default, and the start-up seed of
        # 683187650 already created bulbul:v4-flash as a SECOND default.
        for pid, code in (("pm_v3", "bulbul:v3"), ("pm_v4", "bulbul:v4-flash")):
            conn.execute(sa.text(
                "insert into provider_models (id, provider_code, capability, code, is_default, "
                "status, is_deleted) values (:id, 'sarvam', 'tts', :code, 1, 'active', 0)"),
                {"id": pid, "code": code})
        conn.execute(sa.text(
            "insert into voice_profiles (id, provider, provider_voice_id, model_codes) "
            "values ('vp-sv-shubh', 'sarvam', 'shubh', '[\"bulbul:v3\"]')"))
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # idempotent
        defaults = dict(conn.execute(sa.text(
            "select code, is_default from provider_models where capability='tts'")).all())
        assert defaults == {"bulbul:v3": 1, "bulbul:v4-flash": 0}
        price = conn.execute(sa.text(
            "select unit_price, currency_code from provider_pricing where model_code='bulbul:v4-flash'")).all()
        assert [(float(p), c) for p, c in price] == [(2.0, "INR")]
        personas = conn.execute(sa.text(
            "select id, provider_voice_id, model_codes, languages, locale from voice_profiles "
            "where id like 'vp-sv4-%' order by sort_order")).all()
        assert len(personas) == len(module._PERSONA_VOICES) == 36
        assert personas[0][:2] == ("vp-sv4-shubh_hi_customer", "shubh_hi_customer")
        for _id, wire, codes, languages, locale in personas:
            assert json.loads(codes) == ["bulbul:v4-flash"]
            assert "ml-IN" not in json.loads(languages) and len(json.loads(languages)) == 9
            assert locale in ("hi-IN", "en-IN")
        shubh_v3 = conn.execute(sa.text(
            "select model_codes from voice_profiles where id='vp-sv-shubh'")).scalar()
        assert json.loads(shubh_v3) == ["bulbul:v3"]   # v3 voices are not touched
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.downgrade()
        assert conn.execute(sa.text(
            "select count(*) from voice_profiles where id like 'vp-sv4-%'")).scalar() == 0
        assert conn.execute(sa.text(
            "select count(*) from provider_models where code='bulbul:v4-flash'")).scalar() == 0
        assert conn.execute(sa.text("select count(*) from provider_pricing")).scalar() == 0
        assert conn.execute(sa.text("select count(*) from voice_profiles")).scalar() == 1


def test_v4_flash_migration_on_an_empty_catalog_creates_a_non_default_row():
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = importlib.import_module(
        "backend.alembic.versions.c2e4a6b8d0f1_sarvam_bulbul_v4_flash_catalog")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "create table provider_models (id varchar(40) primary key, provider_code varchar(50), "
            "capability varchar(20), code varchar(80), display_name varchar(150), description text, "
            "languages text, codecs text, sample_rates text, streaming int, params_schema text, "
            "is_default int, status varchar(20), sort_order int, created_at datetime, "
            "updated_at datetime, is_deleted int default 0)"))
        conn.execute(sa.text(
            "create table voice_profiles (id varchar(40) primary key, source varchar(20), "
            "name varchar(100), gender varchar(10), languages text, locale varchar(15), "
            "accent varchar(100), styles text, latency_ms int, premium int, sample_text text, "
            "provider varchar(100), provider_voice_id varchar(100), speaking_rate float, "
            "pitch float, model_codes text, provider_settings text, is_default int, "
            "status varchar(20), sort_order int, created_at datetime, updated_at datetime, "
            "is_deleted int default 0)"))
        conn.execute(sa.text("create table currencies (code varchar(3) primary key)"))
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()  # fresh install: no currencies yet, so no price row
        row = conn.execute(sa.text(
            "select status, is_default, sort_order from provider_models where code='bulbul:v4-flash'")).one()
        assert tuple(row) == ("active", 0, 1)
        assert conn.execute(sa.text(
            "select count(*) from voice_profiles where id like 'vp-sv4-%'")).scalar() == 36
