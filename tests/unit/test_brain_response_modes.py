"""Workflow response modes in the live voice brain.

The workflow engine decides WHAT happened; the node's response mode decides
only how the reply is worded. Pinned here:
- fixed replies speak the authored text with ZERO generation calls;
- exact replies are never paraphrased or language-adapted;
- grounded informational replies stream through normal generation (one call)
  with the authored text as the provider-failure fallback;
- grounded replies that pause on a question use ONE constrained validated
  call: filler that drops the pending ask, missing must-include literals, a
  wrong-language reply or a provider failure all fall back to the authored
  text — never silence, never an unasked question;
- a grounded turn never makes more than one response-generation call.
"""

import asyncio

from shared.bot_config import ResolvedBotConfig
from shared.providers.base import ProviderError
from voice_runtime.brain import ConversationBrain


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
    """Counts constrained generate() and streamed stream() calls."""

    def __init__(self, generate_reply="", stream_reply="Generated reply.",
                 fail_generate=False, fail_stream=False):
        self.generate_reply = generate_reply
        self.stream_reply = stream_reply
        self.fail_generate = fail_generate
        self.fail_stream = fail_stream
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
        fail = self.fail_stream

        async def _gen():
            if fail:
                raise ProviderError("stub", "timeout", "provider down")
            yield reply

        return _gen()


def make_brain(workflow_engine, llm, language="en-IN") -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="en-IN", languages=["en-IN", "hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
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
    brain._conversation_language = language
    brain._active_workflow = "modes_flow"
    return brain


def wf_result(reply, *, mode="fixed", directives=(), must_include=(),
              node_prompt=None, done=True):
    return {
        "reply": reply, "done": done,
        "status": "done" if done else "collecting",
        "source": "definition", "workflowId": "wf_1", "trace": ["n_x"],
        "slots": {}, "handoffQueue": None,
        "offScript": False, "nodePrompt": node_prompt, "signal": None,
        "responseMode": mode,
        "responseDirectives": list(directives),
        "responseMustInclude": list(must_include),
    }


AUTHORED = "Your booking is confirmed in our system. Details or voucher?"
GOAL = "Confirm the booking is confirmed and offer details or voucher."


class TestFixedAndExact:
    async def test_fixed_reply_makes_no_generation_call(self):
        llm = _LLMStub()
        brain = make_brain(_WorkflowStub(wf_result("Authored text.")), llm)

        await brain._handle_turn("hello")

        assert llm.generate_calls == [] and llm.stream_systems == []
        assert brain._history[-1]["content"] == "Authored text."

    async def test_exact_reply_is_never_adapted_even_cross_language(self):
        """An exact node spoken to a Hindi caller stays verbatim: no
        constrained translation, no generation — wording integrity wins."""
        llm = _LLMStub()
        exact = "Never share your OTP, CVV or PIN on this call."
        brain = make_brain(
            _WorkflowStub(wf_result(exact, mode="exact")), llm, language="hi-IN",
        )

        await brain._handle_turn("ठीक है")

        assert llm.generate_calls == [] and llm.stream_systems == []
        assert brain._history[-1]["content"] == exact


class TestGroundedInformational:
    async def test_streams_once_with_directive_and_script(self):
        llm = _LLMStub(stream_reply="Great news — your booking is confirmed!")
        brain = make_brain(
            _WorkflowStub(wf_result(AUTHORED, mode="llm_grounded",
                                    directives=[GOAL])),
            llm,
        )

        await brain._handle_turn("is my booking okay?")

        assert llm.generate_calls == []          # constrained path unused
        assert len(llm.stream_systems) == 1      # exactly ONE generation call
        system = llm.stream_systems[0]
        assert "Deliver the call flow's outcome" in system
        assert GOAL in system
        assert AUTHORED in system
        assert "Never claim an action succeeded" in system
        assert brain._history[-1]["content"] == (
            "Great news — your booking is confirmed!"
        )
        events = dict(brain._recorder.events)
        assert events.get("workflow_reply_grounded", {}).get("mode") == "generation"

    async def test_provider_failure_speaks_the_authored_fallback(self):
        llm = _LLMStub(fail_stream=True)
        brain = make_brain(
            _WorkflowStub(wf_result(AUTHORED, mode="llm_grounded",
                                    directives=[GOAL])),
            llm,
        )

        await brain._handle_turn("is my booking okay?")

        assert brain._history[-1]["content"] == AUTHORED
        assert "orchestration_fallback_reply" in brain._recorder.event_kinds()


class TestGroundedConstrained:
    PROMPT = "Is there anything else I can help you with today?"
    SCRIPT = ("Done! I've emailed your booking voucher. "
              "Is there anything else I can help you with today?")

    def _brain(self, llm, must_include=(), language="en-IN"):
        return make_brain(
            _WorkflowStub(wf_result(
                self.SCRIPT, mode="llm_grounded",
                directives=["Tell the caller the voucher email succeeded."],
                must_include=must_include,
                node_prompt=self.PROMPT, done=False,
            )),
            llm, language=language,
        )

    async def test_valid_generation_is_spoken_once(self):
        llm = _LLMStub(
            generate_reply="Your voucher is on its way to your email — "
                           "anything else I can help with?",
        )
        brain = self._brain(llm)

        await brain._handle_turn("yes send it")

        assert len(llm.generate_calls) == 1
        assert llm.stream_systems == []          # exactly ONE generation call
        system = llm.generate_calls[0]["system"]
        assert self.PROMPT in system
        assert "MUST end by asking" in system
        assert brain._history[-1]["content"].startswith("Your voucher is on its way")
        events = dict(brain._recorder.events)
        assert events.get("workflow_reply_grounded", {}).get("mode") == "constrained"

    async def test_filler_that_drops_the_question_falls_back(self):
        llm = _LLMStub(generate_reply="Please wait, I am sending it now.")
        brain = self._brain(llm)

        await brain._handle_turn("yes send it")

        assert brain._history[-1]["content"] == self.SCRIPT
        events = dict(brain._recorder.events)
        assert events.get("workflow_grounded_fallback", {}).get("reason") == "validation"

    async def test_missing_must_include_literal_falls_back(self):
        llm = _LLMStub(generate_reply="All sent — anything else?")
        brain = self._brain(llm, must_include=["voucher"])

        await brain._handle_turn("yes send it")

        assert brain._history[-1]["content"] == self.SCRIPT

    async def test_provider_failure_falls_back_to_authored(self):
        llm = _LLMStub(fail_generate=True)
        brain = self._brain(llm)

        await brain._handle_turn("yes send it")

        assert brain._history[-1]["content"] == self.SCRIPT
        events = dict(brain._recorder.events)
        assert events.get("workflow_grounded_fallback", {}).get("reason") == "provider"

    async def test_english_caller_gets_hindi_authored_ask_in_english(self):
        """Regression for cv_cc0a08046e2f (Zepto MDND handover step).

        The caller switched to English mid-workflow; the node is
        ``llm_grounded`` with a Hindi authored question. The instruction must
        name English BEFORE the authored text and must not demand the Hindi
        question "exactly", so a valid English rewrite is spoken instead of
        falling back to Hindi (which then pulled the caller back to Hindi).
        """
        hindi_ask = "ये order आपने किसको सौंपा था — customer को, guard को, या किसी और को?"
        english = "Who did you hand the order over to — the customer, the guard, or someone else?"
        llm = _LLMStub(generate_reply=english)
        brain = make_brain(
            _WorkflowStub(wf_result(
                hindi_ask, mode="llm_grounded",
                directives=["Ask only for the actual handover recipient. "
                            "Natural Hinglish, one short question only."],
                node_prompt=hindi_ask, done=False,
            )),
            llm, language="en-IN",
        )

        await brain._handle_turn("Yes, I called and customer told me leave my product.")

        assert len(llm.generate_calls) == 1
        system = llm.generate_calls[0]["system"]
        assert system.index("conversation language is English") < system.index(hindi_ask)
        assert "expressed in natural spoken English" in system
        assert "asking exactly this" not in system
        assert system.rstrip().endswith("Respond in natural spoken English.") or \
            "Respond in natural spoken English." in system
        assert brain._history[-1]["content"] == english
        events = dict(brain._recorder.events)
        assert events.get("workflow_reply_grounded", {}).get("mode") == "constrained"

    async def test_hindi_caller_gets_hindi_or_the_authored_text(self):
        """A Hindi-language turn accepts a Devanagari grounded reply; a
        wrong-language one fails the check and speaks the authored text."""
        hindi = _LLMStub(
            generate_reply="आपका वाउचर ईमेल कर दिया गया है — और कुछ मदद चाहिए?",
        )
        brain = self._brain(hindi, language="hi-IN")
        await brain._handle_turn("हाँ भेज दीजिए")
        assert brain._history[-1]["content"].startswith("आपका वाउचर")

        english = _LLMStub(generate_reply="Sent it — anything else?")
        brain2 = self._brain(english, language="hi-IN")
        await brain2._handle_turn("हाँ भेज दीजिए")
        assert brain2._history[-1]["content"] == self.SCRIPT
