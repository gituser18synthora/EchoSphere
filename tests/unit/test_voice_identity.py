"""Selected TTS catalog metadata drives prompt identity and grammar."""

from shared.orchestration.voice_identity import (
    VoiceIdentity,
    adapt_authored_speaker_grammar,
    active_voice_identity,
    resolve_tts_engine,
    voice_context_values,
    voice_identity_instruction,
    voice_identity_state,
)
from voice_runtime.tts_router import StreamingTTSRouter


def test_default_voice_name_and_gender_are_catalog_driven():
    identity = active_voice_identity({
        "voice": "provider-wire-17",
        "voice_name": "Arbitrary Catalog Speaker",
        "voice_gender": "male",
    }, "hi-IN")

    assert identity == VoiceIdentity("Arbitrary Catalog Speaker", "male")
    instruction = voice_identity_instruction(identity)
    assert "Arbitrary Catalog Speaker" in instruction
    assert "grammatically male forms" in instruction


def test_female_language_override_changes_the_active_identity():
    tts = {
        "streaming": True,
        "voice_name": "Default Speaker",
        "voice_gender": "male",
        "language_map": {
            "hi-IN": {
                "voice_name": "Hindi Speaker",
                "voice_gender": "female",
            },
        },
    }

    identity = active_voice_identity(tts, "hi-IN")
    assert identity == VoiceIdentity("Hindi Speaker", "female")
    assert "grammatically female forms" in voice_identity_instruction(identity)


def test_base_locale_fallback_engine_and_identity_cannot_disagree():
    tts = {
        "streaming": True,
        "provider": "sarvam",
        "voice": "default-male",
        "voice_name": "Default Male",
        "voice_gender": "male",
        "language_map": {
            "hi": {
                "provider": "sarvam",
                "voice": "hindi-female",
                "voice_name": "Hindi Female",
                "voice_gender": "female",
            },
        },
    }

    engine = resolve_tts_engine(tts, "hi-IN")
    identity = active_voice_identity(tts, "hi-IN")
    assert engine["voice"] == "hindi-female"
    assert identity == VoiceIdentity("Hindi Female", "female")


def test_streaming_router_uses_the_same_base_locale_female_engine():
    tts = {
        "streaming": True,
        "provider": "sarvam",
        "model": "bulbul:v3",
        "voice": "default-male",
        "voice_name": "Default Male",
        "voice_gender": "male",
        "language_map": {
            "hi": {
                "provider": "sarvam",
                "model": "bulbul:v3",
                "voice": "hindi-female",
                "voice_name": "Hindi Female",
                "voice_gender": "female",
            },
        },
    }
    router = StreamingTTSRouter(
        tts_config=tts, language="hi-IN", sample_rate=16000
    )

    assert router._engine_for_language("hi-IN")["voice"] == "hindi-female"
    assert active_voice_identity(tts, "hi-IN").gender == "female"


def test_non_streaming_engine_keeps_its_actual_default_voice():
    identity = active_voice_identity({
        "streaming": False,
        "voice_name": "REST Speaker",
        "voice_gender": "female",
        "language_map": {
            "hi-IN": {"voice_name": "Unused Override", "voice_gender": "male"},
        },
    }, "hi-IN")

    assert identity == VoiceIdentity("REST Speaker", "female")


def test_prompt_values_include_canonical_name_and_compatibility_alias():
    values = voice_context_values(VoiceIdentity("Ritu", "female"))

    assert values == {
        "assistant_voice_gender": "female",
        "assistant_voice_name": "Ritu",
        "voice_speaker_gender": "female",
        "voice_speaker_name": "Ritu",
        "voice_bot_spiker_name": "Ritu",
    }


def test_runtime_instruction_overrides_authored_gender_examples():
    female = voice_identity_instruction(VoiceIdentity("Catalog Female", "female"))
    male = voice_identity_instruction(VoiceIdentity("Catalog Male", "male"))

    assert "`assistant_voice_gender = female`" in female
    assert "मैं समझ सकती हूँ" in female
    assert "never use masculine" in female
    assert "`assistant_voice_gender = male`" in male
    assert "मैं समझ सकता हूँ" in male
    assert "never use feminine" in male
    assert "overrides contrary gender forms" in female


def test_goal_engine_state_uses_the_same_catalog_identity():
    assert voice_identity_state(VoiceIdentity("Catalog Female", "female")) == {
        "assistant_voice_name": "Catalog Female",
        "assistant_voice_gender": "female",
    }


def test_unknown_catalog_gender_does_not_guess_male_or_female():
    identity = active_voice_identity({
        "voice_name": "Custom Clone",
        "voice_gender": "unknown",
    }, "hi-IN")

    assert identity.gender == "neutral"
    instruction = voice_identity_instruction(identity)
    assert "does not specify a male/female" in instruction


def test_fixed_hinglish_greeting_uses_female_catalog_grammar():
    text = (
        "नमस्कार! मैं edas की तरफ़ से Ritu bol raha hun. "
        "क्या मेरी बात Seema ji से हो रही है?"
    )

    rendered = adapt_authored_speaker_grammar(text, VoiceIdentity("Ritu", "female"))

    assert "bol rahi hun" in rendered
    # The caller-facing clause is not a first-person bot self-reference.
    assert "हो रही है" in rendered


def test_fixed_devanagari_and_future_forms_work_in_both_directions():
    female = adapt_authored_speaker_grammar(
        "मैं बोल रहा हूँ और बाद में देख लूँगा।",
        VoiceIdentity("Ritu", "female"),
    )
    male = adapt_authored_speaker_grammar(
        "मैं बोल रही हूँ और बाद में देख लूँगी।",
        VoiceIdentity("Shubh", "male"),
    )

    assert female == "मैं बोल रही हूँ और बाद में देख लूँगी।"
    assert male == "मैं बोल रहा हूँ और बाद में देख लूँगा।"

    assert adapt_authored_speaker_grammar(
        "main kal dekh lunga aur phir karunga.",
        VoiceIdentity("Ritu", "female"),
    ) == "main kal dekh lungi aur phir karungi."


def test_fixed_text_with_unknown_gender_is_not_changed():
    text = "मैं बोल रहा हूँ।"
    assert adapt_authored_speaker_grammar(
        text, VoiceIdentity("Custom", "neutral")
    ) == text
