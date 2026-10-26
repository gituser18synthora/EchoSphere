"""Sarvam TTS ``loudness`` end to end, on every bulbul model.

bulbul:v2 documents loudness 0.3-3.0. Sarvam's API reference calls it
v2-only, but bulbul:v3 and bulbul:v4-flash apply it too, with their own range:
measured 2026-10-26 on one Hindi sentence and speaker, over REST and over the
WebSocket config alike, 0.1 renders about 20 dB quieter than 1.0 and 2.5
about 6 dB louder, and anything outside 0.1-2.5 is a 400 ("For the Bulbul V3
and V4 models, loudness should be between 0.1 and 2.5"; for v4-flash also the
Sarvam playground's range).

On every model the range is the model's own catalog schema (what save-time
validation reads, never a shared constant); the value is sent on the
handshake and the REST request when set, omitted at the API default (bots
that never set it render exactly as before), recorded per reply, and kept
when a v4-flash locale falls back to v3. Delivery Energy still maps onto it
for bulbul:v2 only.
"""

import asyncio
import importlib
import json

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import shared.bot_config as bot_config
import shared.providers.tts.sarvam_ws as sarvam_ws
from backend.core.provider_catalog import validate_params
from backend.seeds import provider_catalog_seed as catalog_seed
from shared.providers.tts.delivery import (
    SARVAM_LOUDNESS_DEFAULT,
    apply_delivery_params,
    effective_loudness,
    energy_params,
    omit_default_loudness,
    resolve_engine_params,
    sarvam_accepts_loudness,
)
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings
from tests.mock_tts_servers import MockSarvamTTSServer
from tests.unit.test_sarvam_rest_tts_language_kwarg import _NewSdkTTS, _provider
from tests.unit.test_streaming_tts_providers import sarvam_settings
from tests.unit.test_tts_router_flush_finals import FakeProvider, FakeRecorder, make_router
from voice_runtime.tts_router import _Generation

# (model, a speaker of that model)
MODELS = [("bulbul:v2", "anushka"), ("bulbul:v3", "shubh"), ("bulbul:v4-flash", "shubh")]
V3_CONTRACT = ["bulbul:v3", "bulbul:v4-flash"]
# What each model's catalog schema must publish: v2 as documented, v3 and
# v4-flash as the API enforces it.
EXPECTED_RANGE = {"bulbul:v2": (0.3, 3.0), "bulbul:v3": (0.1, 2.5), "bulbul:v4-flash": (0.1, 2.5)}


@pytest.fixture(autouse=True)
def _no_warm_spare(monkeypatch):
    monkeypatch.setattr(sarvam_ws, "_WARM_SPARE", False)


async def _handshake(monkeypatch, **overrides) -> dict:
    async with MockSarvamTTSServer() as server:
        monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
        provider = SarvamWebSocketTTSProvider(sarvam_settings(**overrides))
        await provider.connect()
        await provider.close()
    return server.configs[0]


def _schema(model: str) -> dict:
    row = next(r for r in catalog_seed.PROVIDER_MODELS if r[:3] == ("sarvam", "tts", model))
    return row[8]


class TestModelSupport:
    def test_every_bulbul_model_takes_loudness(self):
        assert all(sarvam_accepts_loudness(model) for model, _ in MODELS)
        assert not sarvam_accepts_loudness(None)
        assert not sarvam_accepts_loudness("")
        assert SARVAM_LOUDNESS_DEFAULT == 1.0

    @pytest.mark.parametrize("model,_voice", MODELS)
    def test_catalog_schema_publishes_the_range_per_model(self, model, _voice):
        spec = _schema(model)["loudness"]
        assert (spec["min"], spec["max"]) == EXPECTED_RANGE[model]
        assert spec["default"] == SARVAM_LOUDNESS_DEFAULT
        assert (spec["type"], spec["step"], spec["label"]) == ("number", 0.1, "Loudness")
        assert f"{spec['min']}–{spec['max']}" in spec["help"]

    @pytest.mark.parametrize("model", V3_CONTRACT)
    def test_the_slider_sits_next_to_pace(self, model):
        keys = list(_schema(model))
        assert keys.index("loudness") == keys.index("pace") + 1

    def test_effective_value(self):
        assert effective_loudness("sarvam", "bulbul:v2", {"loudness": 1.4}) == 1.4
        assert effective_loudness("sarvam", "bulbul:v3", {"loudness": 0.5}) == 0.5
        assert effective_loudness("sarvam", "bulbul:v4-flash", {"loudness": 2.5}) == 2.5
        assert effective_loudness("sarvam", "bulbul:v3", {}) == 1.0
        assert effective_loudness("sarvam", "bulbul:v4-flash", None) == 1.0
        assert effective_loudness("elevenlabs", "eleven_flash_v2_5", {"loudness": 1.4}) is None

    def test_default_is_dropped_and_other_values_kept(self):
        assert omit_default_loudness({"loudness": 1, "pitch": 0.1}) == {"pitch": 0.1}
        assert omit_default_loudness({"loudness": 0.8}) == {"loudness": 0.8}

    @pytest.mark.parametrize("model", V3_CONTRACT)
    def test_energy_never_sets_it_on_the_v3_contract(self, model):
        # Existing v3 / v4-flash bots keep their sound: Energy fills nothing
        # there, and an explicit value passes through untouched.
        for level in (0, 25, 75, 100):
            assert energy_params("sarvam", model, level) == {}
        assert apply_delivery_params("sarvam", model, {}, energy=100) == {}
        assert apply_delivery_params(
            "sarvam", model, {"loudness": 0.6}, energy=100) == {"loudness": 0.6}


class TestHandshake:
    @pytest.mark.parametrize("model,voice", MODELS)
    async def test_carries_loudness_when_set(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice,
                                  params={"pace": 1.0, "loudness": 1.4})
        assert config["loudness"] == 1.4

    @pytest.mark.parametrize("model,voice", MODELS)
    async def test_omits_loudness_at_the_api_default(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice,
                                  params={"loudness": 1.0, "pace": 1.1})
        assert "loudness" not in config
        assert config["pace"] == 1.1

    @pytest.mark.parametrize("model,voice", MODELS)
    async def test_omits_loudness_when_unset(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice, params={"pace": 1.0})
        assert "loudness" not in config

    @pytest.mark.parametrize("model", V3_CONTRACT)
    @pytest.mark.parametrize("value", [0.1, 2.5])
    async def test_v3_contract_carries_its_range_edges(self, monkeypatch, model, value):
        config = await _handshake(monkeypatch, model=model, voice="shubh",
                                  params={"loudness": value})
        assert config["loudness"] == value

    @pytest.mark.parametrize("model", V3_CONTRACT)
    async def test_pitch_stays_bulbul_v2_only(self, monkeypatch, model):
        config = await _handshake(monkeypatch, model=model, voice="shubh",
                                  params={"pitch": 0.2, "loudness": 0.8})
        assert "pitch" not in config
        assert config["loudness"] == 0.8


class TestRest:
    @pytest.mark.parametrize("model,voice", MODELS)
    async def test_rest_sends_a_set_value_and_omits_the_default(self, monkeypatch, model, voice):
        tts = _NewSdkTTS()
        provider = _provider(monkeypatch, tts, language="hi-IN")
        provider._model = model
        provider._params = {"loudness": 1.0}
        await provider.synthesize("Hello", voice=voice, language="hi-IN")
        assert "loudness" not in tts.calls[-1]
        provider._params = {"loudness": 1.4, "pitch": 0.2}
        await provider.synthesize("Hello", voice=voice, language="hi-IN")
        assert tts.calls[-1]["loudness"] == 1.4
        assert ("pitch" in tts.calls[-1]) is (model == "bulbul:v2")


class TestValidation:
    @pytest.mark.parametrize("value", [0.3, 1.0, 2.2, 3.0])
    def test_bulbul_v2_accepts_its_documented_range(self, value):
        assert validate_params(_schema("bulbul:v2"), {"loudness": value}, prefix="TTS") == []

    @pytest.mark.parametrize("value", [0.29, 3.01, -1, 10])
    def test_bulbul_v2_rejects_out_of_range(self, value):
        assert validate_params(_schema("bulbul:v2"), {"loudness": value}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.3 and 3.0."]

    @pytest.mark.parametrize("model", V3_CONTRACT)
    @pytest.mark.parametrize("value", [0.1, 0.2, 1.0, 2.5])
    def test_v3_contract_accepts_the_range_the_api_accepts(self, model, value):
        assert validate_params(_schema(model), {"loudness": value}, prefix="TTS") == []

    @pytest.mark.parametrize("model", V3_CONTRACT)
    @pytest.mark.parametrize("value", [0.09, 2.51, 3.0, -1])
    def test_v3_contract_rejects_what_the_api_rejects(self, model, value):
        # 3.0 is valid on bulbul:v2 but a 400 on v3 / v4-flash.
        assert validate_params(_schema(model), {"loudness": value},
                               prefix="Voice mapping [hi-IN]") == [
            "Voice mapping [hi-IN]: 'loudness' must be between 0.1 and 2.5."]

    def test_the_range_is_read_from_the_model_schema(self):
        # No shared constant: whatever range a catalog row publishes is the
        # one enforced for that model.
        narrowed = {"loudness": {**_schema("bulbul:v3")["loudness"], "min": 0.5, "max": 2.0}}
        assert validate_params(narrowed, {"loudness": 2.2}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.5 and 2.0."]
        assert validate_params(_schema("bulbul:v2"), {"loudness": 2.8}, prefix="TTS") == []
        assert validate_params(_schema("bulbul:v3"), {"loudness": 2.8}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.1 and 2.5."]

    @pytest.mark.parametrize("model,_voice", MODELS)
    def test_rejects_a_non_number(self, model, _voice):
        assert validate_params(_schema(model), {"loudness": "loud"}, prefix="TTS") == [
            "TTS: 'loudness' must be a number."]


class TestReplyEvent:
    async def _event(self, *, model: str, params: dict, provider_code: str = "sarvam") -> dict:
        recorder = FakeRecorder()
        router = make_router(recorder=recorder)
        provider = FakeProvider()
        provider._settings = TTSStreamSettings(provider=provider_code, model=model, voice="anushka",
                                               language="hi-IN", sample_rate=16000, params=params)
        router._generations["ctx1"] = _Generation(
            engine={"provider": provider_code, "model": model, "voice": "anushka"}, provider=provider)
        await router._finalize_generation("ctx1", failed=False)
        await asyncio.gather(*router.tasks)
        return next(e for e in recorder.events if e["kind"] == "tts_provider_used")

    @pytest.mark.parametrize("model,_voice", MODELS)
    async def test_records_the_configured_loudness(self, model, _voice):
        event = await self._event(model=model, params={"loudness": 1.4})
        assert (event["model"], event["loudness"]) == (model, 1.4)

    @pytest.mark.parametrize("model,_voice", MODELS)
    async def test_records_the_api_default_when_unset(self, model, _voice):
        event = await self._event(model=model, params={"pace": 1.1})
        assert event["loudness"] == 1.0

    async def test_records_none_for_an_engine_without_loudness(self):
        event = await self._event(model="eleven_flash_v2_5", params={"stability": 0.5},
                                  provider_code="elevenlabs")
        assert (event["model"], event["loudness"]) == ("eleven_flash_v2_5", None)


# ── v4-flash -> v3 language fallback ───────────────────────────────────────

class TestV3FallbackKeepsLoudness:
    """A v4-flash locale without a persona stays on v3 with the bot's
    settings — its loudness reaches the v3 wire and the reply event."""

    def _tts(self, monkeypatch) -> dict:
        monkeypatch.setattr(bot_config, "_engine_allowed", lambda *a, **k: None)
        tts = {"provider": "sarvam", "model": "bulbul:v4-flash", "voice": "kavya",
               "voice_name": "Kavya", "voice_gender": "female", "api_key_reference": "",
               "settings": {"loudness": 0.6, "pace": 1.05}, "language_map": {
                   # an override engine with its own validated params
                   "ta-IN": {"provider": "sarvam", "model": "bulbul:v4-flash", "voice": "ashutosh",
                             "params": {"loudness": 1.8}}}}
        bot_config._keep_unmapped_v4_locales_on_v3(
            None, tts, ["hi-IN", "en-IN", "ml-IN", "ta-IN"], {}, "bot_x")
        return tts

    def test_the_v3_engines_carry_the_value(self, monkeypatch):
        tts = self._tts(monkeypatch)
        assert "hi-IN" not in tts["language_map"]  # kavya has a Hindi persona
        for locale in ("en-IN", "ml-IN"):
            engine = tts["language_map"][locale]
            assert engine["model"] == "bulbul:v3"
            assert resolve_engine_params(tts, engine)["loudness"] == 0.6
        ta = tts["language_map"]["ta-IN"]
        assert (ta["model"], resolve_engine_params(tts, ta)["loudness"]) == ("bulbul:v3", 1.8)

    async def test_the_v3_wire_and_event_carry_it(self, monkeypatch):
        tts = self._tts(monkeypatch)
        params = resolve_engine_params(tts, tts["language_map"]["ml-IN"])
        config = await _handshake(monkeypatch, model="bulbul:v3", voice="kavya",
                                  language="ml-IN", params=params)
        assert (config["model"], config["speaker"], config["loudness"]) == ("bulbul:v3", "kavya", 0.6)
        event = await TestReplyEvent()._event(model="bulbul:v3", params=params)
        assert event["loudness"] == 0.6


# ── catalog migration ──────────────────────────────────────────────────────

class TestMigration:
    """e4b6d8f0a2c4 brings an existing catalog to the seed's schemas."""

    def _module(self):
        return importlib.import_module(
            "backend.alembic.versions.e4b6d8f0a2c4_sarvam_loudness_v3_v4_flash")

    def _engine(self, rows: list[tuple[str, str, str, dict]]):
        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(sa.text(
                "create table provider_models (id varchar(40) primary key, provider_code varchar(50), "
                "capability varchar(20), code varchar(80), params_schema text, updated_at datetime)"))
            for row_id, provider, code, schema in rows:
                conn.execute(sa.text(
                    "insert into provider_models (id, provider_code, capability, code, params_schema) "
                    "values (:id, :provider, 'tts', :code, :schema)"),
                    {"id": row_id, "provider": provider, "code": code, "schema": json.dumps(schema)})
        return engine

    def _schemas(self, engine) -> dict:
        with engine.begin() as conn:
            return {code: json.loads(blob) for code, blob in conn.execute(sa.text(
                "select code, params_schema from provider_models")).all()}

    def _run(self, engine, step: str, times: int = 1) -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                for _ in range(times):
                    getattr(self._module(), step)()

    def test_round_trip(self):
        seed = {model: _schema(model) for model, _ in MODELS}
        before = {
            "bulbul:v2": {**seed["bulbul:v2"], "loudness": {
                **seed["bulbul:v2"]["loudness"], "help": "Volume multiplier (bulbul:v2 only)."}},
            "bulbul:v3": {k: v for k, v in seed["bulbul:v3"].items() if k != "loudness"},
            "bulbul:v4-flash": {k: v for k, v in seed["bulbul:v4-flash"].items() if k != "loudness"},
        }
        other = {"stability": {"type": "number", "min": 0, "max": 1, "default": 0.5}}
        rows = [(f"pm_{i}", "sarvam", code, schema) for i, (code, schema) in enumerate(before.items())]
        engine = self._engine(rows + [("pm_el", "elevenlabs", "eleven_flash_v2_5", other)])

        self._run(engine, "upgrade", times=2)  # idempotent
        after = self._schemas(engine)
        for model in seed:
            # The same spec, in the same place, as a fresh install's seed.
            assert after[model] == seed[model], model
            assert list(after[model]) == list(seed[model]), model
        assert after["eleven_flash_v2_5"] == other
        for model in V3_CONTRACT:
            assert validate_params(after[model], {"loudness": 2.5}, prefix="TTS") == []
            assert validate_params(after[model], {"loudness": 2.51}, prefix="TTS") == [
                "TTS: 'loudness' must be between 0.1 and 2.5."]

        self._run(engine, "downgrade")
        assert self._schemas(engine) == {**before, "eleven_flash_v2_5": other}

    def test_an_existing_loudness_entry_is_left_alone(self):
        custom = {"pace": {"type": "number"}, "loudness": {"type": "number", "min": 0.5, "max": 2.0}}
        engine = self._engine([("pm_1", "sarvam", "bulbul:v3", custom),
                               ("pm_2", "sarvam", "bulbul:v4-flash", custom)])
        self._run(engine, "upgrade")
        assert self._schemas(engine) == {"bulbul:v3": custom, "bulbul:v4-flash": custom}
