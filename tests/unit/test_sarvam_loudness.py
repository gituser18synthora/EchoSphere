"""Sarvam TTS ``loudness`` end to end: bulbul:v2 and bulbul:v4-flash, never v3.

bulbul:v2 documents loudness 0.3-3.0. bulbul:v4-flash takes it too, with its
own range: 0.1-2.5 in the Sarvam playground, and the API rejects anything
outside it with 400 (measured 2026-10-26 on the same Hindi sentence and
speaker: 0.1 renders about 20 dB quieter than 1.0 and 2.5 about 6 dB louder,
over REST and over the WebSocket config alike). bulbul:v3 does not offer it.

On both models the range is the model's own catalog schema (what save-time
validation reads, never a shared constant); the value is sent on the
handshake and the REST request when set, omitted at the API default (bots
that never set it render exactly as before), and recorded per reply.
Delivery Energy still maps onto it for bulbul:v2 only.
"""

import asyncio
import importlib
import json

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import shared.providers.tts.sarvam_ws as sarvam_ws
from backend.core.provider_catalog import validate_params
from backend.seeds import provider_catalog_seed as catalog_seed
from shared.providers.tts.delivery import (
    SARVAM_LOUDNESS_DEFAULT,
    apply_delivery_params,
    effective_loudness,
    energy_params,
    omit_default_loudness,
    sarvam_accepts_loudness,
)
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings
from tests.mock_tts_servers import MockSarvamTTSServer
from tests.unit.test_sarvam_rest_tts_language_kwarg import _NewSdkTTS, _provider
from tests.unit.test_streaming_tts_providers import sarvam_settings
from tests.unit.test_tts_router_flush_finals import FakeProvider, FakeRecorder, make_router
from voice_runtime.tts_router import _Generation

# (model, a speaker of that model) for the models that offer loudness.
LOUDNESS_MODELS = [("bulbul:v2", "anushka"), ("bulbul:v4-flash", "shubh")]
# What each model's catalog schema must publish: v2 as documented, v4-flash
# as the playground offers and the API enforces it.
EXPECTED_RANGE = {"bulbul:v2": (0.3, 3.0), "bulbul:v4-flash": (0.1, 2.5)}


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
    def test_bulbul_v2_and_v4_flash_take_loudness_v3_never(self):
        assert sarvam_accepts_loudness("bulbul:v2")
        assert sarvam_accepts_loudness("bulbul:v4-flash")
        assert not sarvam_accepts_loudness("bulbul:v3")
        assert not sarvam_accepts_loudness(None)
        assert SARVAM_LOUDNESS_DEFAULT == 1.0

    @pytest.mark.parametrize("model,_voice", LOUDNESS_MODELS)
    def test_catalog_schema_publishes_the_range_per_model(self, model, _voice):
        spec = _schema(model)["loudness"]
        assert (spec["min"], spec["max"]) == EXPECTED_RANGE[model]
        assert spec["default"] == SARVAM_LOUDNESS_DEFAULT
        assert (spec["type"], spec["step"], spec["label"]) == ("number", 0.1, "Loudness")

    def test_bulbul_v3_schema_has_no_loudness(self):
        assert "loudness" not in _schema("bulbul:v3")

    def test_v4_flash_slider_sits_next_to_pace(self):
        keys = list(_schema("bulbul:v4-flash"))
        assert keys.index("loudness") == keys.index("pace") + 1

    def test_effective_value(self):
        assert effective_loudness("sarvam", "bulbul:v2", {"loudness": 1.4}) == 1.4
        assert effective_loudness("sarvam", "bulbul:v4-flash", {"loudness": 0.5}) == 0.5
        assert effective_loudness("sarvam", "bulbul:v4-flash", {}) == 1.0
        assert effective_loudness("sarvam", "bulbul:v4-flash", None) == 1.0
        assert effective_loudness("sarvam", "bulbul:v3", {"loudness": 1.4}) is None
        assert effective_loudness("elevenlabs", "eleven_flash_v2_5", {"loudness": 1.4}) is None

    def test_default_is_dropped_and_other_values_kept(self):
        assert omit_default_loudness({"loudness": 1, "pitch": 0.1}) == {"pitch": 0.1}
        assert omit_default_loudness({"loudness": 0.8}) == {"loudness": 0.8}

    def test_energy_never_sets_it_on_v4_flash(self):
        # Existing v4-flash bots keep their sound: Energy fills nothing there,
        # and an explicit value passes through untouched.
        for level in (0, 25, 75, 100):
            assert energy_params("sarvam", "bulbul:v4-flash", level) == {}
        assert apply_delivery_params("sarvam", "bulbul:v4-flash", {}, energy=100) == {}
        assert apply_delivery_params(
            "sarvam", "bulbul:v4-flash", {"loudness": 0.6}, energy=100) == {"loudness": 0.6}


class TestHandshake:
    @pytest.mark.parametrize("model,voice", LOUDNESS_MODELS)
    async def test_carries_loudness_when_set(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice,
                                  params={"pace": 1.0, "loudness": 1.4})
        assert config["loudness"] == 1.4

    @pytest.mark.parametrize("model,voice", LOUDNESS_MODELS)
    async def test_omits_loudness_at_the_api_default(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice,
                                  params={"loudness": 1.0, "pace": 1.1})
        assert "loudness" not in config
        assert config["pace"] == 1.1

    @pytest.mark.parametrize("model,voice", LOUDNESS_MODELS)
    async def test_omits_loudness_when_unset(self, monkeypatch, model, voice):
        config = await _handshake(monkeypatch, model=model, voice=voice, params={"pace": 1.0})
        assert "loudness" not in config

    @pytest.mark.parametrize("value", [0.1, 2.5])
    async def test_v4_flash_carries_its_range_edges(self, monkeypatch, value):
        config = await _handshake(monkeypatch, model="bulbul:v4-flash", voice="shubh",
                                  params={"loudness": value})
        assert config["loudness"] == value
        assert config["speaker"] == "shubh_hi_customer"

    async def test_v4_flash_never_gets_v2_pitch(self, monkeypatch):
        config = await _handshake(monkeypatch, model="bulbul:v4-flash", voice="shubh",
                                  params={"pitch": 0.2, "loudness": 0.8})
        assert "pitch" not in config
        assert config["loudness"] == 0.8

    async def test_never_sent_to_bulbul_v3(self, monkeypatch):
        config = await _handshake(monkeypatch, model="bulbul:v3", voice="shubh",
                                  params={"loudness": 1.4, "pace": 1.1})
        assert "loudness" not in config
        assert config["pace"] == 1.1


class TestRest:
    @pytest.mark.parametrize("model,voice", LOUDNESS_MODELS)
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

    async def test_rest_never_sends_it_to_bulbul_v3(self, monkeypatch):
        tts = _NewSdkTTS()
        provider = _provider(monkeypatch, tts, language="hi-IN")
        provider._params = {"loudness": 1.4, "temperature": 0.3}
        await provider.synthesize("Hello", voice="shubh", language="hi-IN")
        assert tts.calls[-1]["model"] == "bulbul:v3"
        assert "loudness" not in tts.calls[-1]
        assert tts.calls[-1]["temperature"] == 0.3


class TestValidation:
    @pytest.mark.parametrize("value", [0.3, 1.0, 2.2, 3.0])
    def test_bulbul_v2_accepts_its_documented_range(self, value):
        assert validate_params(_schema("bulbul:v2"), {"loudness": value}, prefix="TTS") == []

    @pytest.mark.parametrize("value", [0.29, 3.01, -1, 10])
    def test_bulbul_v2_rejects_out_of_range(self, value):
        assert validate_params(_schema("bulbul:v2"), {"loudness": value}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.3 and 3.0."]

    @pytest.mark.parametrize("value", [0.1, 0.2, 1.0, 2.5])
    def test_bulbul_v4_flash_accepts_its_range(self, value):
        assert validate_params(_schema("bulbul:v4-flash"), {"loudness": value}, prefix="TTS") == []

    @pytest.mark.parametrize("value", [0.09, 2.51, 3.0, -1])
    def test_bulbul_v4_flash_rejects_what_the_api_rejects(self, value):
        # 3.0 is valid on bulbul:v2 but a 400 on v4-flash.
        assert validate_params(_schema("bulbul:v4-flash"), {"loudness": value},
                               prefix="Voice mapping [hi-IN]") == [
            "Voice mapping [hi-IN]: 'loudness' must be between 0.1 and 2.5."]

    def test_bulbul_v3_rejects_it_as_unknown(self):
        assert validate_params(_schema("bulbul:v3"), {"loudness": 1.2}, prefix="Voice mapping [hi-IN]") == [
            "Voice mapping [hi-IN]: unknown parameter 'loudness'."]

    def test_the_range_is_read_from_the_model_schema(self):
        # No shared constant: whatever range a catalog row publishes is the
        # one enforced for that model.
        narrowed = {"loudness": {**_schema("bulbul:v4-flash")["loudness"], "min": 0.5, "max": 2.0}}
        assert validate_params(narrowed, {"loudness": 2.2}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.5 and 2.0."]
        assert validate_params(_schema("bulbul:v2"), {"loudness": 2.8}, prefix="TTS") == []
        assert validate_params(_schema("bulbul:v4-flash"), {"loudness": 2.8}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.1 and 2.5."]

    @pytest.mark.parametrize("model,_voice", LOUDNESS_MODELS)
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

    @pytest.mark.parametrize("model,_voice", LOUDNESS_MODELS)
    async def test_records_the_configured_loudness(self, model, _voice):
        event = await self._event(model=model, params={"loudness": 1.4})
        assert (event["model"], event["loudness"]) == (model, 1.4)

    @pytest.mark.parametrize("model,_voice", LOUDNESS_MODELS)
    async def test_records_the_api_default_when_unset(self, model, _voice):
        event = await self._event(model=model, params={"pace": 1.1})
        assert event["loudness"] == 1.0

    async def test_records_none_for_bulbul_v3(self):
        event = await self._event(model="bulbul:v3", params={"temperature": 0.3, "loudness": 1.4})
        assert (event["model"], event["loudness"]) == ("bulbul:v3", None)

    async def test_records_none_for_an_engine_without_loudness(self):
        event = await self._event(model="eleven_flash_v2_5", params={"stability": 0.5},
                                  provider_code="elevenlabs")
        assert (event["model"], event["loudness"]) == ("eleven_flash_v2_5", None)


# ── catalog migration ──────────────────────────────────────────────────────

class TestMigration:
    """e4b6d8f0a2c4 brings an existing catalog to the seed's schemas."""

    def _module(self):
        return importlib.import_module(
            "backend.alembic.versions.e4b6d8f0a2c4_sarvam_loudness_v4_flash")

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
        seed = {model: _schema(model) for model in ("bulbul:v2", "bulbul:v3", "bulbul:v4-flash")}
        before = {
            "bulbul:v2": {**seed["bulbul:v2"], "loudness": {
                **seed["bulbul:v2"]["loudness"], "help": "Volume multiplier (bulbul:v2 only)."}},
            "bulbul:v3": seed["bulbul:v3"],
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
        assert "loudness" not in after["bulbul:v3"]
        assert after["eleven_flash_v2_5"] == other
        assert validate_params(after["bulbul:v4-flash"], {"loudness": 2.5}, prefix="TTS") == []
        assert validate_params(after["bulbul:v4-flash"], {"loudness": 2.51}, prefix="TTS") == [
            "TTS: 'loudness' must be between 0.1 and 2.5."]

        self._run(engine, "downgrade")
        assert self._schemas(engine) == {**before, "eleven_flash_v2_5": other}

    def test_an_existing_loudness_entry_is_left_alone(self):
        custom = {"pace": {"type": "number"}, "loudness": {"type": "number", "min": 0.5, "max": 2.0}}
        engine = self._engine([("pm_1", "sarvam", "bulbul:v4-flash", custom)])
        self._run(engine, "upgrade")
        assert self._schemas(engine)["bulbul:v4-flash"] == custom
