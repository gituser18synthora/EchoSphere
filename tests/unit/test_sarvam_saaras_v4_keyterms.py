"""Sarvam saaras:v4 + key-term biasing: shared rules, realtime adapter,
pipeline wiring, REST transcriber and save-time validation.

Covers the contract end to end without a network:

- saaras:v3 stays the default everywhere; saaras:v4 constructs and reaches
  the runtime (Pipecat's closed model table is extended, not replaced);
- ``keyterms`` travel stt_settings → build_stt_service → handshake proxy as
  ONE JSON-encoded array, only for saaras:v4;
- v3 (and any other model) never gets the parameter, even when a stale
  configuration still carries it, and the drop is recorded;
- empty/absent keyterms are a no-op for both models;
- the documented limits (50 × 64, one term per entry) are enforced at
  save time via the model schema + shared rule set.
"""

import json

import pytest
from pipecat.services.sarvam.stt import MODEL_CONFIGS

from backend.core.provider_catalog import validate_stt_settings
from backend.seeds.provider_catalog_seed import PROVIDER_MODELS
from shared.bot_config import ResolvedBotConfig
from shared.providers.base import ProviderConfig
from shared.providers.stt.sarvam import SarvamSTT
from shared.providers.stt.sarvam_keyterms import (
    MAX_KEYTERM_CHARS,
    MAX_KEYTERMS,
    encode_keyterms_query,
    keyterm_problems,
    normalize_keyterms,
    supports_keyterms,
)
from voice_runtime import sarvam_stt
from voice_runtime.pipeline import build_batch_transcriber, build_stt_service


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [data for k, data in self.events if k == kind]


def _config(model: str, settings: dict | None = None) -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id="t", bot_id="b", bot_name="Test", version="1", published=True,
        language="hi-IN", languages=["hi-IN", "en-IN"],
        stt={
            "provider": "sarvam", "model": model, "language": "",
            "api_key_reference": "env:TEST_SARVAM_API_KEY",
            "settings": settings or {},
        },
    )


def _seed_row(code: str):
    for row in PROVIDER_MODELS:
        if row[0] == "sarvam" and row[1] == "stt" and row[2] == code:
            return row
    raise AssertionError(f"seed row {code} missing")


V3_SCHEMA = _seed_row("saaras:v3")[8]
V4_SCHEMA = _seed_row("saaras:v4")[8]


# ── shared rule set ──────────────────────────────────────────────────────────

class TestKeytermRules:
    def test_only_saaras_v4_supports_keyterms(self):
        assert supports_keyterms("saaras:v4")
        assert supports_keyterms(" SAARAS:V4 ")
        assert not supports_keyterms("saaras:v3")
        assert not supports_keyterms("saarika:v2.5")
        assert not supports_keyterms(None)

    def test_normalize_trims_dedupes_and_drops_blanks(self):
        assert normalize_keyterms([" Zepto ", "New  Delhi", "", "   ", "Zepto", 7]) == [
            "Zepto", "New Delhi",
        ]
        assert normalize_keyterms(None) == []
        assert normalize_keyterms("Zepto, New Delhi") == []

    def test_problems_follow_sarvam_limits(self):
        assert keyterm_problems(None) == []
        assert keyterm_problems(["Zepto", "New Delhi"]) == []
        assert keyterm_problems([f"t{i}" for i in range(MAX_KEYTERMS)]) == []
        assert any("at most 50" in p for p in keyterm_problems([f"t{i}" for i in range(MAX_KEYTERMS + 1)]))
        assert any("64" in p for p in keyterm_problems(["x" * (MAX_KEYTERM_CHARS + 1)]))
        assert keyterm_problems(["x" * MAX_KEYTERM_CHARS]) == []
        assert any("comma" in p for p in keyterm_problems(["Zepto, New Delhi"]))
        assert any("list" in p for p in keyterm_problems("Zepto"))
        assert any("string" in p for p in keyterm_problems(["Zepto", 3]))

    def test_duplicates_and_whitespace_do_not_count_against_limits(self):
        padded = [f" t{i} " for i in range(MAX_KEYTERMS)] + ["t0", "t1"]
        assert keyterm_problems(padded) == []

    def test_wire_encoding_is_one_json_array(self):
        wire = encode_keyterms_query(["Zepto", "New Delhi", "ज़ेप्टो"])
        assert json.loads(wire) == ["Zepto", "New Delhi", "ज़ेप्टो"]
        assert "ज़ेप्टो" in wire  # not ASCII-escaped


# ── catalog / seed ───────────────────────────────────────────────────────────

class TestCatalogSeed:
    def test_v3_stays_default_and_v4_is_selectable_not_default(self):
        v3, v4 = _seed_row("saaras:v3"), _seed_row("saaras:v4")
        assert v3[9] is True and v3[10] == "active"
        assert v4[9] is False and v4[10] == "active"
        assert v4[4] == v3[4]  # same language codes

    def test_keyterms_exist_only_in_the_v4_schema(self):
        assert "keyterms" not in V3_SCHEMA
        spec = V4_SCHEMA["keyterms"]
        assert spec["type"] == "string_list"
        assert spec["max_items"] == MAX_KEYTERMS and spec["max_length"] == MAX_KEYTERM_CHARS
        assert "default" not in spec  # absent key == no biasing
        # Everything v3 offers, v4 offers too (a model switch keeps settings).
        assert set(V3_SCHEMA) <= set(V4_SCHEMA)


# ── save-time validation ─────────────────────────────────────────────────────

class TestSaveTimeValidation:
    def test_v4_accepts_keyterms_and_v3_rejects_them(self):
        assert validate_stt_settings(V4_SCHEMA, {"mode": "transcribe", "keyterms": ["Zepto"]}) == []
        errors = validate_stt_settings(V3_SCHEMA, {"mode": "transcribe", "keyterms": ["Zepto"]})
        assert errors and "unknown parameter 'keyterms'" in errors[0]

    def test_v4_enforces_documented_limits(self):
        assert validate_stt_settings(V4_SCHEMA, {"keyterms": [f"t{i}" for i in range(51)]})
        assert validate_stt_settings(V4_SCHEMA, {"keyterms": ["x" * 65]})
        assert validate_stt_settings(V4_SCHEMA, {"keyterms": ["a, b"]})
        assert validate_stt_settings(V4_SCHEMA, {"keyterms": "Zepto"})

    def test_absent_or_none_keyterms_are_fine_on_both_models(self):
        assert validate_stt_settings(V4_SCHEMA, {"mode": "transcribe"}) == []
        assert validate_stt_settings(V4_SCHEMA, {"keyterms": None}) == []
        assert validate_stt_settings(V3_SCHEMA, {"mode": "transcribe"}) == []

    def test_generic_string_list_rejects_blank_entries(self):
        schema = {"hints": {"type": "string_list", "max_items": 4}}
        assert validate_stt_settings(schema, {"hints": ["a", " "]})
        assert validate_stt_settings(schema, {"hints": ["a", "b"]}) == []


# ── realtime adapter + pipeline ──────────────────────────────────────────────

def _handshake_defaults(service) -> dict:
    proxy = service._sarvam_client._speech_to_text_streaming
    return getattr(proxy, "connect_defaults", {}) or {}


class TestRealtimeAdapter:
    def test_saaras_v4_is_registered_with_pipecat_as_a_v3_capability_copy(self):
        assert "saaras:v4" in MODEL_CONFIGS
        v3, v4 = MODEL_CONFIGS["saaras:v3"], MODEL_CONFIGS["saaras:v4"]
        assert v4.supports_mode and v4.supports_language and v4.supports_vad_params
        assert v4.use_translate_endpoint == v3.use_translate_endpoint is False

    async def test_default_model_is_still_saaras_v3(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        config = _config("", {"keyterms": ["Zepto"]})
        config.stt["model"] = ""
        service = build_stt_service(config)
        try:
            assert service._settings.model == "saaras:v3"
            assert service.keyterms == []
            assert "keyterms" not in _handshake_defaults(service)
        finally:
            await service.cleanup()

    async def test_v4_sends_keyterms_as_json_array_on_the_handshake(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        recorder = _Recorder()
        service = build_stt_service(
            _config("saaras:v4", {"mode": "transcribe", "keyterms": [" Zepto", "New Delhi", "Zepto", ""]}),
            recorder=recorder,
        )
        try:
            assert isinstance(service, sarvam_stt.EndpointedSarvamSTTService)
            assert service._settings.model == "saaras:v4"
            assert service._mode == "transcribe"
            assert service.keyterms == ["Zepto", "New Delhi"]
            defaults = _handshake_defaults(service)
            assert json.loads(defaults["keyterms"]) == ["Zepto", "New Delhi"]
            assert defaults["input_audio_codec"] == "pcm_s16le"
            [event] = recorder.of("stt_keyterms")
            assert event["sent"] is True and event["count"] == 2 and event["model"] == "saaras:v4"
        finally:
            await service.cleanup()

    async def test_handshake_proxy_forwards_defaults_without_overriding_explicit_kwargs(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        service = build_stt_service(_config("saaras:v4", {"keyterms": ["Zepto"]}))
        try:
            proxy = service._sarvam_client._speech_to_text_streaming
            captured: dict = {}

            class _Inner:
                def connect(self, **kwargs):
                    captured.update(kwargs)
                    return "ctx"

            proxy._client = _Inner()
            assert proxy.connect(model="saaras:v4", sample_rate="16000") == "ctx"
            assert json.loads(captured["keyterms"]) == ["Zepto"]
            assert captured["input_audio_codec"] == "pcm_s16le"
            captured.clear()
            proxy.connect(model="saaras:v4", keyterms='["Other"]')
            assert captured["keyterms"] == '["Other"]'  # explicit wins
        finally:
            await service.cleanup()

    async def test_v3_never_sends_keyterms_even_when_configured(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        recorder = _Recorder()
        service = build_stt_service(_config("saaras:v3", {"keyterms": ["Zepto"]}), recorder=recorder)
        try:
            assert service._settings.model == "saaras:v3"
            assert service.keyterms == []
            assert "keyterms" not in _handshake_defaults(service)
            [event] = recorder.of("stt_keyterms")
            assert event["sent"] is False and event["reason"] == "unsupported_model"
        finally:
            await service.cleanup()

    @pytest.mark.parametrize("settings", [{}, {"keyterms": []}, {"keyterms": None}, {"keyterms": ["", "  "]}])
    async def test_empty_keyterms_are_a_no_op_for_v4(self, monkeypatch, settings):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        recorder = _Recorder()
        service = build_stt_service(_config("saaras:v4", settings), recorder=recorder)
        try:
            assert service._settings.model == "saaras:v4"
            assert service.keyterms == []
            assert "keyterms" not in _handshake_defaults(service)
            assert recorder.of("stt_keyterms") == []
        finally:
            await service.cleanup()

    async def test_adapter_is_the_last_gate_for_unsupported_models(self, monkeypatch, caplog):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        from pipecat.services.sarvam.stt import SarvamSTTService

        service = sarvam_stt.EndpointedSarvamSTTService(
            api_key="k", sample_rate=16000, input_audio_codec="pcm_s16le",
            settings=SarvamSTTService.Settings(model="saaras:v3"),
            keyterms=["Zepto"],
        )
        try:
            assert service.keyterms == []
            assert "keyterms" not in _handshake_defaults(service)
        finally:
            await service.cleanup()

    async def test_v3_and_v4_language_and_vad_paths_are_identical(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        settings = {"mode": "translit", "high_vad_sensitivity": True, "min_speech_frames": 4}
        v3 = build_stt_service(_config("saaras:v3", settings), use_provider_vad=True)
        v4 = build_stt_service(_config("saaras:v4", {**settings, "keyterms": ["Zepto"]}), use_provider_vad=True)
        try:
            for service in (v3, v4):
                assert service._settings.language is None  # multilingual → auto-detect
                assert service._settings.vad_signals is True
                assert service._settings.high_vad_sensitivity is True
                assert service._settings.min_speech_frames == 4
                assert service._mode == "translit"
        finally:
            await v3.cleanup()
            await v4.cleanup()


# ── REST transcriber (identifier recovery) ───────────────────────────────────

class TestRestTranscriber:
    def test_rest_adapter_forwards_keyterms_only_for_v4(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        v4 = SarvamSTT(ProviderConfig(
            provider="sarvam", model="saaras:v4", api_key_reference="env:TEST_SARVAM_API_KEY",
            extra={"keyterms": ["Zepto", " Zepto", "New Delhi"]},
        ))
        v3 = SarvamSTT(ProviderConfig(
            provider="sarvam", model="saaras:v3", api_key_reference="env:TEST_SARVAM_API_KEY",
            extra={"keyterms": ["Zepto"]},
        ))
        assert v4.keyterms == ["Zepto", "New Delhi"]
        assert v3.keyterms == []

    async def test_rest_request_carries_keyterms_list_for_v4(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        calls: list[dict] = []

        class _Response:
            transcript = "zepto"
            language_code = "en-IN"
            language_probability = 0.9

        class _STT:
            async def transcribe(self, **kwargs):
                calls.append(kwargs)
                return _Response()

        for model, expect in (("saaras:v4", ["Zepto"]), ("saaras:v3", None)):
            adapter = SarvamSTT(ProviderConfig(
                provider="sarvam", model=model, api_key_reference="env:TEST_SARVAM_API_KEY",
                extra={"keyterms": ["Zepto"]},
            ))
            # ``speech_to_text`` is a lazy read-only property on the SDK client;
            # its backing slot is what the property returns once set.
            adapter._client._speech_to_text = _STT()
            result = await adapter.transcribe(b"\x00\x00" * 1600, sample_rate=16000, language="en-IN")
            assert result.text == "zepto"
            assert calls[-1]["model"] == model
            assert calls[-1].get("keyterms") == expect

    async def test_batch_transcriber_passes_keyterms_through_extra_for_v4_only(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        seen: list[ProviderConfig] = []

        class _Provider:
            def __init__(self, config):
                seen.append(config)

            async def transcribe(self, pcm, *, sample_rate, language):
                from shared.providers.base import STTResult
                return STTResult(text="ok")

        monkeypatch.setattr("voice_runtime.pipeline.get_stt_provider", lambda cfg: _Provider(cfg))
        for model, expect in (("saaras:v4", ["Zepto"]), ("saaras:v3", None)):
            transcribe = build_batch_transcriber(_config(model, {"keyterms": ["Zepto"]}))
            assert await transcribe(b"\x00\x00", 16000, "hi-IN") == "ok"
            assert seen[-1].model == model
            assert seen[-1].extra.get("keyterms") == expect
