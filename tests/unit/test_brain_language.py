"""Conversation-language following in the brain: per-utterance detection with
stability rules (supported-language clamp, minimum length, script agreement),
client notifications, and the per-turn LLM language instruction. Language
switches must never touch conversation history or session state."""

import pytest

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import (
    ConversationBrain,
    language_label,
    script_supports_language,
)


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        pass

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))


def make_brain(language="hi-IN", languages=("en-IN", "hi-IN")) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=list(languages),
        stt={"provider": "sarvam"},
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    return brain




class TestScriptHeuristics:
    def test_devanagari_utterance_is_hindi(self):
        assert script_supports_language("मुझे अपना लोन का पेमेंट करना है", "hi-IN")

    def test_pure_english_is_english(self):
        assert script_supports_language("Please tell me my account status", "en-IN")

    def test_english_words_inside_hindi_do_not_read_as_english(self):
        # Borrowed words ("account status") must not flip the language.
        assert not script_supports_language("मेरा account status क्या है", "en-IN")
        assert script_supports_language("मेरा account status क्या है", "hi-IN")

    def test_latin_only_text_never_reads_as_hindi(self):
        assert not script_supports_language("Please tell me my account status", "hi-IN")

    def test_labels(self):
        assert language_label("hi-IN") == "Hindi"
        assert language_label("en-IN") == "English"
        assert language_label("") == ""


class TestLanguageSwitching:
    async def test_hindi_to_english_switch(self):
        brain = make_brain(language="hi-IN")
        brain._history.append({"role": "user", "content": "पहला सवाल"})
        await brain._maybe_switch_language("Actually, can you tell me when it was created?", "en-IN")
        assert brain._conversation_language == "en-IN"
        assert any(getattr(f, "language", None) == "en-IN" for f in brain._pushed)
        assert {"type": "language", "language": "en-IN"} in brain._notified
        # Conversation state untouched by the switch.
        assert brain._history == [{"role": "user", "content": "पहला सवाल"}]

    async def test_switch_back_to_hindi(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Can you help me in English?", "en-IN")
        await brain._maybe_switch_language("अच्छा, और ये कैंसल कैसे होगा?", "hi-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_unsupported_language_keeps_current_and_notifies(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("ஒரு கேள்வி உள்ளது", "ta-IN")
        assert brain._conversation_language == "hi-IN"
        assert brain._pushed == []  # no voice switch
        assert any(
            n.get("name") == "language_unsupported" for n in brain._notified
        )

    async def test_single_word_never_switches(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Okay.", "en-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_script_disagreement_blocks_switch(self):
        brain = make_brain(language="hi-IN")
        # STT says English but the words are Devanagari-dominant — stay Hindi.
        await brain._maybe_switch_language("मेरा account status क्या है", "en-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_base_code_maps_to_configured_locale(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Please talk in English now", "en")
        assert brain._conversation_language == "en-IN"

    async def test_missing_language_metadata_is_ignored(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("hello there my friend", None)
        assert brain._conversation_language == "hi-IN"


class TestLanguageInstruction:
    def test_hindi_instruction(self):
        brain = make_brain(language="hi-IN")
        text = brain._language_instruction()
        assert "Hindi" in text and "Reply ONLY in Hindi" in text

    def test_follows_current_language_not_default(self):
        brain = make_brain(language="hi-IN")
        brain._conversation_language = "en-IN"
        assert "Reply ONLY in English" in brain._language_instruction()


class TestSerializerSafety:
    async def test_error_frame_serializes_safely(self):
        import json

        from pipecat.frames.frames import ErrorFrame

        from voice_runtime.serializer import RawPCMSerializer

        raw = await RawPCMSerializer().serialize(ErrorFrame(error="tts_failure:timeout"))
        message = json.loads(raw)
        assert message == {"type": "error", "message": "tts_failure:timeout"}

    async def test_error_frame_is_truncated(self):
        import json

        from pipecat.frames.frames import ErrorFrame

        from voice_runtime.serializer import RawPCMSerializer

        raw = await RawPCMSerializer().serialize(ErrorFrame(error="x" * 500))
        assert len(json.loads(raw)["message"]) == 120


class TestSessionAnnouncement:
    async def test_session_config_precedes_greeting(self):
        brain = make_brain()
        brain._client_info = {"sampleRate": 16000, "language": "hi-IN"}
        brain._pipeline_started = True
        said = []

        async def _say(text):
            said.append(text)

        brain._say = _say
        await brain._open_session()
        assert brain._notified and brain._notified[0]["type"] == "session_config"
        assert brain._notified[0]["sampleRate"] == 16000
        assert said  # greeting spoken after the announcement


class TestHinglishFollowing:
    async def test_romanized_hinglish_switches_to_hindi(self):
        # STT (translit/codemix modes) reports Hindi but writes Latin script —
        # the STT verdict plus Hinglish marker words confirm the switch.
        brain = make_brain(language="en-IN")
        await brain._maybe_switch_language(
            "haan bhai kal payment kar dunga", "hi-IN"
        )
        assert brain._conversation_language == "hi-IN"

    async def test_plain_english_misdetected_as_hindi_does_not_switch(self):
        brain = make_brain(language="en-IN")
        await brain._maybe_switch_language(
            "I will make the payment tomorrow", "hi-IN"
        )
        assert brain._conversation_language == "en-IN"

    def test_hinglish_reads_as_hindi_not_english(self):
        assert script_supports_language("haan bhai kal payment kar dunga", "hi-IN")
        assert not script_supports_language("I will pay tomorrow morning", "hi-IN")

    async def test_switch_updates_recorder_language(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language(
            "Actually, please continue in English", "en-IN"
        )
        assert brain._conversation_language == "en-IN"
        assert brain._recorder.language == "en-IN"
