"""Workflow ask preservation under reply-language adaptation.

An input-collecting workflow step ("Could you please share your booking
ID?") delivered to a caller in another language must still ASK for that
input. The old path re-delivered it through full conversational generation,
which could replace the question with progress filler ("Please wait, I am
checking that for you") — the engine kept waiting for an answer the caller
was never asked for, deadlocking the flow.

Pinned here:
- an awaiting-input reply is adapted by CONSTRAINED translation (script
  only — no history, no persona) and spoken only when validation proves the
  ask survived;
- failed validation / provider failure falls back to the authored question
  verbatim (never filler, never silence);
- informational workflow replies (nothing awaited) keep the existing
  generation-based adaptation, so normal multilingual replies still work;
- replies already in the caller's language are spoken as authored.
"""

import asyncio

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain, validate_scripted_adaptation

ASK_EN = "I can help you with that. Could you please share your booking ID?"
ASK_HI = "क्या आप कृपया अपनी बुकिंग आईडी बता सकते हैं?"
MDND_ASK_HI = "क्या आपने delivery से पहले customer को call किया था?"
MDND_ASK_EN = "Did you call the customer before the delivery?"
FILLER_EN = "Please wait, I am checking that for you."


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0,
                      "llm_input_tokens": 0}
        self.turns = []
        self.language = "en-IN"

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


class _WorkflowStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def handle_turn_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _LLMStub:
    """Records generate() calls (constrained adaptation) and stream() calls
    (full conversational generation) separately."""

    def __init__(self, generate_reply=ASK_HI, stream_reply="जी, बताइए।",
                 fail_generate=False):
        self.generate_reply = generate_reply
        self.stream_reply = stream_reply
        self.fail_generate = fail_generate
        self.generate_calls = []
        self.stream_systems = []

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        self.generate_calls.append({"messages": messages, "system": system})
        if self.fail_generate:
            raise RuntimeError("provider down")

        reply = self.generate_reply

        class _Result:
            text = reply
            input_tokens = 80
            output_tokens = 30

        return _Result()

    def stream(self, history, *, system, temperature, max_tokens):
        self.stream_systems.append(system)
        reply = self.stream_reply

        async def _gen():
            yield reply

        return _gen()


def awaiting_ask_result(reply=ASK_EN, node_prompt="Could you please share your booking ID?"):
    return {
        "reply": reply, "done": False, "status": "collecting",
        "source": "definition", "workflowId": "wf_1", "trace": ["n_ask_booking"],
        "slots": {}, "handoffQueue": None,
        "offScript": False, "nodePrompt": node_prompt, "signal": None,
    }


def message_result(reply, done=True):
    return {
        "reply": reply, "done": done, "status": "done" if done else "collecting",
        "source": "definition", "workflowId": "wf_1", "trace": ["n_msg"],
        "slots": {}, "handoffQueue": None,
        "offScript": False, "nodePrompt": None, "signal": None,
    }


def make_brain(workflow_engine, llm, *, tts=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="en-IN", languages=["en-IN", "hi-IN"],
        stt={"provider": "sarvam"}, tts=tts or {}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        workflow_engine=workflow_engine, finalize_grace=0.05,
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
    # The caller switched to Hindi; the workflow is active and authored in
    # the bot's default English.
    brain._conversation_language = "hi-IN"
    brain._active_workflow = "oyo_booking_support_journey"
    return brain


class TestValidateScriptedAdaptation:
    def test_faithful_translation_passes(self):
        assert validate_scripted_adaptation(ASK_EN, ASK_HI, "hi-IN")

    def test_filler_without_the_question_fails(self):
        # Wrong language AND no question mark — both gates reject it.
        assert not validate_scripted_adaptation(ASK_EN, FILLER_EN, "hi-IN")

    def test_right_language_but_question_dropped_fails(self):
        assert not validate_scripted_adaptation(
            ASK_EN, "कृपया प्रतीक्षा करें, मैं देख रही हूँ।", "hi-IN"
        )

    def test_numbers_must_survive_verbatim(self):
        script = "Your booking 601001 is confirmed. Shall I email the voucher?"
        assert not validate_scripted_adaptation(
            script, "आपकी बुकिंग 601002 कन्फर्म है। क्या मैं वाउचर भेज दूँ?", "hi-IN"
        )
        assert validate_scripted_adaptation(
            script, "आपकी बुकिंग 601001 कन्फर्म है। क्या मैं वाउचर भेज दूँ?", "hi-IN"
        )

    def test_empty_or_bloated_output_fails(self):
        assert not validate_scripted_adaptation(ASK_EN, "", "hi-IN")
        assert not validate_scripted_adaptation(ASK_EN, "क्या? " * 200, "hi-IN")


class TestAwaitingAskAdaptation:
    async def test_ask_is_adapted_by_constrained_translation(self):
        llm = _LLMStub(generate_reply=ASK_HI)
        brain = make_brain(_WorkflowStub(awaiting_ask_result()), llm)

        await brain._handle_turn("मेरी बुकिंग कन्फर्म है क्या?")

        # Constrained call only — no conversational generation ran.
        assert len(llm.generate_calls) == 1
        assert llm.stream_systems == []
        call = llm.generate_calls[0]
        assert call["messages"] == [{"role": "user", "content": ASK_EN}]
        assert "Hindi" in call["system"]
        assert "please wait" in call["system"]  # the named failure mode
        # The adapted ask was spoken and recorded.
        assert brain._history[-1]["content"] == ASK_HI
        events = dict(brain._recorder.events)
        assert events.get("workflow_reply_language_adapted", {}).get("mode") \
            == "constrained_translation"

    async def test_adapted_ask_uses_selected_female_speaker_grammar(self):
        # This is the exact failure shape seen in cv_f6c638d74ab2: the TTS
        # voice was Shreya, but constrained workflow translation emitted
        # masculine first-person Hindi.
        masculine = (
            "मैं इसमें मदद कर सकता हूँ। "
            "क्या आप कृपया अपनी बुकिंग आईडी बता सकते हैं?"
        )
        llm = _LLMStub(generate_reply=masculine)
        brain = make_brain(
            _WorkflowStub(awaiting_ask_result()), llm,
            tts={"voice_name": "Shreya", "voice_gender": "female"},
        )

        await brain._handle_turn("मुझे अपनी बुकिंग देखनी है")

        assert "assistant_voice_gender = female" in llm.generate_calls[0]["system"]
        assert brain._history[-1]["content"] == (
            "मैं इसमें मदद कर सकती हूँ। "
            "क्या आप कृपया अपनी बुकिंग आईडी बता सकते हैं?"
        )

    async def test_hindi_workflow_ask_adapts_to_english_without_hindi_bias(self):
        """Regression for cv_9f9ef085d1d9.

        The caller switched to English while a Hindi-authored workflow was
        active.  Hindi gender examples appended after the translation command
        made the model answer in Hindi, validation rejected it, and the
        original Hindi question was spoken.  English adaptation must receive
        locale-safe speaker instructions.
        """
        llm = _LLMStub(generate_reply=MDND_ASK_EN)
        brain = make_brain(
            _WorkflowStub(awaiting_ask_result(
                reply=MDND_ASK_HI, node_prompt=MDND_ASK_HI,
            )),
            llm,
            tts={"voice_name": "Shubh", "voice_gender": "male"},
        )
        brain._config.language = "hi-IN"
        brain._conversation_language = "en-IN"

        await brain._handle_turn("Yes, I called.")

        system = llm.generate_calls[0]["system"]
        assert "natural spoken English" in system
        assert "required response language is en-IN" in system
        assert "मैं समझ सकता हूँ" not in system
        assert brain._history[-1]["content"] == MDND_ASK_EN
        events = dict(brain._recorder.events)
        assert events.get("workflow_reply_language_adapted", {}).get("mode") \
            == "constrained_translation"

    async def test_invalid_adaptation_falls_back_to_authored_ask(self):
        llm = _LLMStub(generate_reply=FILLER_EN)
        brain = make_brain(_WorkflowStub(awaiting_ask_result()), llm)

        await brain._handle_turn("मेरी बुकिंग कन्फर्म है क्या?")

        # The authored question was spoken verbatim — never the filler.
        assert brain._history[-1]["content"] == ASK_EN
        assert "workflow_ask_adaptation_fallback" in brain._recorder.event_kinds()

    async def test_provider_failure_falls_back_to_authored_ask(self):
        llm = _LLMStub(fail_generate=True)
        brain = make_brain(_WorkflowStub(awaiting_ask_result()), llm)

        await brain._handle_turn("मेरी बुकिंग कन्फर्म है क्या?")

        assert brain._history[-1]["content"] == ASK_EN
        assert "workflow_ask_adaptation_fallback" in brain._recorder.event_kinds()

    async def test_matching_language_is_spoken_as_authored(self):
        llm = _LLMStub()
        brain = make_brain(
            _WorkflowStub(awaiting_ask_result(reply=ASK_HI, node_prompt=ASK_HI)),
            llm,
        )

        await brain._handle_turn("हाँ जी")

        assert llm.generate_calls == []
        assert llm.stream_systems == []
        assert brain._history[-1]["content"] == ASK_HI

    async def test_informational_reply_keeps_generation_adaptation(self):
        """A workflow message with nothing awaited still adapts through the
        existing generation path — normal multilingual replies unchanged."""
        llm = _LLMStub(stream_reply="धन्यवाद, आपका दिन शुभ हो!")
        brain = make_brain(
            _WorkflowStub(message_result("Thank you for calling OYO. Have a great day!")),
            llm,
        )

        await brain._handle_turn("बस इतना ही, धन्यवाद")

        assert llm.generate_calls == []          # constrained path not used
        assert len(llm.stream_systems) == 1      # generation-based delivery
        assert "Scripted step" in llm.stream_systems[0]
        assert brain._history[-1]["content"] == "धन्यवाद, आपका दिन शुभ हो!"
