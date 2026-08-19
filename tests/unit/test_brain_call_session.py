"""Per-call session behavior of the ConversationBrain.

- Fragment aggregation: straggler STT finals debounce/merge into ONE turn —
  including finals that land while the previous fragment's reply is already
  generating, and fragments that already earned a canned clarification.
- Placeholder safety: nothing spoken (canned, greeting, or streamed LLM
  tokens) may contain unresolved template placeholders.
- Per-call caching: the immutable system prompt is assembled once per call,
  and every piece of per-call state is dropped on cleanup.
"""

import asyncio
from datetime import datetime, timezone

from pipecat.frames.frames import TextFrame, TranscriptionFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain

GRACE = 0.05


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


    def event_kinds(self):
        return [kind for kind, _ in self.events]


class _StreamingLLMStub:
    """Yields fixed token lists; records the system prompt it was given."""

    def __init__(self, tokens):
        self._tokens = tokens
        self.calls = []
        self.last_stream_usage = None

    def stream(self, history, *, system, temperature, max_tokens):
        self.calls.append({"history": [dict(m) for m in history], "system": system})

        async def _gen():
            for token in self._tokens:
                yield token

        return _gen()


def make_brain(*, llm=None, call_context=None, system_prompt="You are Test.",
               language="hi-IN", tts=None, languages=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=languages or [language],
        stt={"provider": "sarvam"}, tts=tts or {}, system_prompt=system_prompt,
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        call_context=call_context, finalize_grace=GRACE,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    return brain


def transcript(text, language="hi-IN"):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t", language=language)


async def settle_turn():
    await asyncio.sleep(GRACE * 2)
    for _ in range(5):
        await asyncio.sleep(0)


def user_history(brain):
    return [m["content"] for m in brain._history if m["role"] == "user"]


class TestFragmentAggregation:
    async def test_orphan_finals_within_grace_merge_into_one_turn(self):
        # One utterance, finalized as two STT segments with no open VAD turn
        # (quiet speech): exactly ONE turn runs, with the joined text.
        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await brain.process_frame(
            transcript("नहीं, मैं पार्ट पेमेंट भी"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("नहीं कर सकता अभी"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == ["नहीं, मैं पार्ट पेमेंट भी नहीं कर सकता अभी"]

    async def test_straggler_final_merges_into_in_flight_generation(self):
        # The reply for fragment 1 is still generating when fragment 2 lands:
        # the generation is cancelled, the partial user turn rewound, and the
        # turn re-runs ONCE with the complete utterance. History and the call
        # transcript both record a single user message.
        brain = make_brain()
        generated = []
        release = asyncio.Event()

        async def _generate(text, decision, started, extra_system=""):
            generated.append(text)
            if not release.is_set():
                await asyncio.sleep(30)

        brain._generate_reply = _generate
        await brain.process_frame(
            transcript("मुझे समझ नहीं आ रहा पेमेंट कैसे"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert generated == ["मुझे समझ नहीं आ रहा पेमेंट कैसे"]

        release.set()
        await brain.process_frame(
            transcript("करना है मोबाइल ऐप से"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        merged = "मुझे समझ नहीं आ रहा पेमेंट कैसे करना है मोबाइल ऐप से"
        assert generated == ["मुझे समझ नहीं आ रहा पेमेंट कैसे", merged]
        assert user_history(brain) == [merged]
        assert [t.text for t in brain._recorder.turns if t.role == "user"] == [merged]
        assert "turn_merged_late_final" in brain._recorder.event_kinds()
        # The client's live transcript already showed the fragment: the merge
        # must retract it, or the merged turn repeats the same words on screen.
        rewinds = [n for n in brain._notified if n.get("type") == "turn_rewound"]
        assert [r["user_text"] for r in rewinds] == ["मुझे समझ नहीं आ रहा पेमेंट कैसे"]

    async def test_clarify_fragment_is_rewound_when_utterance_completes(self):
        # A too-short SIGNAL-LESS fragment gets the canned clarification;
        # when the rest of the utterance arrives the exchange is rewound: the
        # LLM sees ONE complete user message and no clarify message pollutes
        # the history. (A short fragment that carries a semantic signal — a
        # bare "नहीं" refusal, "haan" — now routes to the LLM instead.)
        brain = make_brain()
        generated = []

        async def _generate(text, decision, started, extra_system=""):
            generated.append(text)

        brain._generate_reply = _generate
        await brain.process_frame(transcript("मेरा मतलब,"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        # Fragment routed to CLARIFY: canned line spoken, nothing generated.
        assert generated == []
        clarify_texts = [n["text"] for n in brain._notified if n.get("type") == "bot_text"]
        assert len(clarify_texts) == 1

        await brain.process_frame(
            transcript("मैं पार्ट पेमेंट भी नहीं कर सकता"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        merged = "मेरा मतलब, मैं पार्ट पेमेंट भी नहीं कर सकता"
        assert generated == [merged]
        assert user_history(brain) == [merged]
        assert all(m["content"] != clarify_texts[0] for m in brain._history)
        assert "clarify_fragment_merged" in brain._recorder.event_kinds()
        # The rewound clarify exchange is retracted from the client's live
        # transcript too — both the fragment and the spoken clarification.
        rewinds = [n for n in brain._notified if n.get("type") == "turn_rewound"]
        assert rewinds == [{
            "type": "turn_rewound",
            "user_text": "मेरा मतलब,",
            "bot_text": clarify_texts[0],
        }]

    async def test_resumed_speech_cancels_pending_finalize(self):
        # The caller starts speaking again during the grace window: nothing
        # runs until the (single) turn closes with the full buffer.
        brain = make_brain()
        handled = []

        async def _handle(text):
            handled.append(text)

        brain._handle_turn = _handle
        await brain.process_frame(transcript("मैं अभी"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == []  # buffer held for the reopened turn
        assert brain._pending_segments == ["मैं अभी"]


class TestPlaceholderSafety:
    async def test_say_uses_the_stored_turn_timestamp_in_the_live_payload(self):
        brain = make_brain()
        record = await brain._say("Namaste!")

        expected = (
            datetime.fromtimestamp(record.timestamp, tz=timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        payload = next(n for n in brain._notified if n.get("type") == "bot_text")
        assert payload["at"] == expected

    async def test_say_strips_unresolved_placeholders(self):
        brain = make_brain()
        record = await brain._say("क्या मैं [aapka naam] से बात कर रहा हूं?")
        assert record is not None
        assert "[" not in record.text and "aapka naam" not in record.text
        spoken = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
        assert spoken and all("[" not in s for s in spoken)

    async def test_say_resolves_call_context_values(self):
        brain = make_brain(call_context={"customer_name": "Ravi"})
        record = await brain._say("Namaste {{customer_name}} ji!")
        assert record.text == "Namaste Ravi ji!"

    async def test_say_resolves_selected_voice_name_without_tenant_data(self):
        brain = make_brain(tts={
            "voice_name": "Ritu", "voice_gender": "female",
        })
        record = await brain._say(
            "Namaste, main {voice_speaker_name} hoon. "
            "Legacy: {voice_bot_spiker_name}."
        )
        assert record.text == "Namaste, main Ritu hoon. Legacy: Ritu."

    async def test_say_adapts_fixed_greeting_to_selected_female_voice(self):
        brain = make_brain(tts={
            "voice_name": "Ritu", "voice_gender": "female",
        })
        record = await brain._say(
            "नमस्कार! मैं edas की तरफ़ से {voice_speaker_name} "
            "bol raha hun. क्या मेरी बात Seema ji से हो रही है?"
        )

        assert record.text == (
            "नमस्कार! मैं edas की तरफ़ से Ritu bol rahi hun. "
            "क्या मेरी बात Seema ji से हो रही है?"
        )

    def test_selected_voice_gender_is_added_to_every_llm_turn(self):
        brain = make_brain(tts={
            "voice_name": "Ritu", "voice_gender": "female",
        })
        instruction = brain._language_instruction()
        assert "Selected speaker name: Ritu" in instruction
        assert "grammatically female forms" in instruction
        assert "assistant_voice_gender = female" in instruction
        assert "मैं समझ सकती हूँ" in instruction

    def test_selected_voice_gender_reaches_stage_a_decision_context(self):
        brain = make_brain(tts={
            "voice_name": "Ritu", "voice_gender": "female",
        })

        state = brain._orchestration_state()

        assert state["assistant_voice_name"] == "Ritu"
        assert state["assistant_voice_gender"] == "female"

    async def test_generated_reply_is_not_gender_post_processed(self):
        brain = make_brain(tts={
            "voice_name": "Ritu", "voice_gender": "female",
        })

        record = await brain._say(
            "मैं समझ सकता हूँ।", authored=False,
        )

        # Generated grammar must be correct because of LLM context, not a
        # response mutation hidden in the TTS delivery path.
        assert record.text == "मैं समझ सकता हूँ।"

    def test_language_voice_switch_changes_the_llm_grammar_identity(self):
        brain = make_brain(
            language="hi-IN", languages=["hi-IN", "en-IN"],
            tts={
                "streaming": True,
                "voice_name": "Hindi Male", "voice_gender": "male",
                "language_map": {
                    "en-IN": {
                        "voice_name": "English Female", "voice_gender": "female",
                    },
                },
            },
        )
        assert "grammatically male forms" in brain._language_instruction()
        brain._conversation_language = "en-IN"
        assert "grammatically female forms" in brain._language_instruction()

    async def test_streamed_reply_never_contains_placeholders(self):
        llm = _StreamingLLMStub(
            ["क्या मैं [aapka ", "naam] से बात ", "कर रहा हूं?"]
        )
        brain = make_brain(llm=llm)
        await brain.process_frame(
            transcript("मेरा नाम आपको कैसे पता चलेगा बताइए"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        spoken = "".join(f.text for f in brain._pushed if isinstance(f, TextFrame))
        assert spoken == "क्या मैं से बात कर रहा हूं?"
        assert brain._history[-1]["role"] == "assistant"
        assert "[" not in brain._history[-1]["content"]


class TestPerCallPromptCache:
    def test_static_system_is_assembled_once_with_context_rules(self):
        brain = make_brain(system_prompt="Persona prompt.",
                           call_context={"customer_name": "Ravi"})
        assert brain._static_system.startswith("Persona prompt.")
        assert "# Natural voice conversation" in brain._static_system
        assert "customer_name: Ravi" in brain._static_system

    def test_empty_context_states_absence_explicitly(self):
        # Without this, "use the customer name from the call context" prompts
        # make the LLM invent bracket placeholders in browser test calls.
        brain = make_brain()
        assert "No customer-specific values" in brain._static_system
        assert "never speak placeholder text" in brain._static_system

    def test_prompt_placeholders_resolve_from_call_context(self):
        brain = make_brain(system_prompt="Customer: {{customer_name}}.",
                           call_context={"customer_name": "Ravi"})
        assert brain._static_system.startswith("Customer: Ravi.")

    async def test_generation_reuses_cached_prompt(self):
        llm = _StreamingLLMStub(["ठीक है।"])
        brain = make_brain(llm=llm)
        cached = brain._static_system
        # A later config edit (or accidental mutation) must not leak into the
        # live call: the call keeps the snapshot taken at start.
        brain._config.system_prompt = "MUTATED AFTER CALL START"
        for text in ("पेमेंट के बारे में बताइए ज़रा", "ठीक है फिर आगे बताइए अब"):
            await brain.process_frame(transcript(text), FrameDirection.DOWNSTREAM)
            await settle_turn()
        assert len(llm.calls) == 2
        for call in llm.calls:
            assert call["system"].startswith(cached)
            assert "MUTATED" not in call["system"]
        # The language suffix is cached per conversation language.
        assert brain._language_instruction_cache

    async def test_cleanup_drops_all_per_call_state(self):
        brain = make_brain(call_context={"customer_name": "Ravi"})
        await brain._say("Namaste!")
        brain._pending_segments.append("stray")
        await brain.cleanup()
        assert brain._history == []
        assert brain._pending_segments == []
        assert brain._call_context == {}
        assert brain._static_system == ""
        assert brain._last_bot_reply == ""
        assert brain._language_instruction_cache == {}

    def test_two_calls_share_nothing(self):
        config_kwargs = dict(system_prompt="Same bot.", call_context={"k": "v1"})
        first = make_brain(**config_kwargs)
        second = make_brain(system_prompt="Same bot.", call_context={"k": "v2"})
        first._history.append({"role": "user", "content": "private"})
        assert second._history == []
        assert first._call_context != second._call_context
