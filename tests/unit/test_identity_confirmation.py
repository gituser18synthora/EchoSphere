"""Identity confirmation modes (standard | strict) and the re-ask ladder.

The live case (cv_6d46f8de094d, Manappuram bot_f49776262f1a, 2026-10-26): the
greeting asked "क्या मेरी बात gaurav जी से हो रही है?", the caller said "हाँ
जी बोलिए।" and then "हाँ बोलिए।", and the decision LLM judged both ambiguous —
the bot asked "क्या मेरी बात गौरव जी से हो रही है?" twice more, word for word.
Standard mode confirms on the first reply; strict mode asks ONE explicit,
differently worded question and confirms on the second; neither ever asks the
same identity question a third time, and negations / other-person cues still
deny.
"""

import asyncio

import pytest

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.goal_engine import _WARMUP_MAX_TOKENS, GoalEngine
from shared.orchestration.identity_confirmation import (
    STANDARD,
    STRICT,
    decision_prompt_rule,
    resolve_identity_confirmation_mode,
)
from shared.runtime_context import build_runtime_context
from voice_runtime.brain import ConversationBrain
from voice_runtime.call_policy import CollectionCallPolicy, classify_identity_answer
from voice_runtime.identity_confirmation import (
    AFFIRM,
    CONFIRM,
    EXPLICIT_REASK,
    GO_AHEAD,
    SELF_IDENTIFIED,
    IdentityConfirmation,
    classify_identity_reply,
    identity_reask,
    identity_reply_outcome,
)

from tests.unit.test_agentic_orchestration import (
    IDENTITY_QUESTION,
    _AgenticLLMStub,
    events,
    make_agentic_brain,
)
from tests.unit.test_brain_collection_policy import (
    GRACE,
    _RecorderStub,
    bot_replies,
    snapshot,
    turn,
)

GREETING = (
    "नमस्ते! मैं Manappuram Finance Limited से Kavya बोल रही हूँ। ये call quality "
    "और training purposes के लिए record की जा रही है। क्या मेरी बात "
    "{customer_name} जी से हो रही है?"
)
SPOKEN_ASK = "क्या मेरी बात gaurav जी से हो रही है?"
LLM_REASK = "क्या मेरी बात गौरव जी से हो रही है?"
AMBIGUOUS = {
    "intent": "caller_go_ahead", "signal": "affirm", "decision": "ambiguous",
    "scope": "in_scope", "confidence": 0.98,
    "next_action": "ask_identity_confirmation", "needs_clarification": True,
    "response_text": LLM_REASK,
}

STANDARD_CONFIRMS = (
    "haan", "haan ji", "ji", "ji haan", "haan boliye", "ji boliye",
    "haan bol raha hoon", "haan bol rahi hoon", "main hi bol raha hoon",
    "haan main hoon", "हाँ जी बोलिए।", "हाँ बोलिए।", "हाँ।", "जी हाँ",
    "हाँ, मैं ही बोल रहा हूँ।", "yes speaking", "yes go ahead",
)
NEVER_CONFIRM = (
    "nahi", "जी नहीं", "नहीं नहीं", "galat number", "गलत नंबर है",
    "main unka bhai bol raha hoon", "मैं उनका बेटा बोल रहा हूँ",
    "haan, main unki wife hoon", "haan main Rahul bol raha hoon",
    "हाँ जी?", "कौन बोल रहा है?", "बोल रहा", "बोलिए।", "I mean बोल रहा हूँ।",
)


class TestModes:
    def test_collections_defaults_to_strict_and_everything_else_to_standard(self):
        assert resolve_identity_confirmation_mode(None, "collections") == STRICT
        assert resolve_identity_confirmation_mode(None, "generic") == STANDARD
        assert resolve_identity_confirmation_mode(None, None) == STANDARD
        assert resolve_identity_confirmation_mode("", "collections") == STRICT

    def test_a_bot_setting_wins_over_the_default(self):
        assert resolve_identity_confirmation_mode("standard", "collections") == STANDARD
        assert resolve_identity_confirmation_mode("STRICT", "generic") == STRICT
        assert resolve_identity_confirmation_mode("loose", "collections") == STRICT


class TestReplyRule:
    @pytest.mark.parametrize("text", STANDARD_CONFIRMS)
    def test_standard_confirms_affirm_led_replies(self, text):
        kind = classify_identity_reply(text, "gaurav")
        assert identity_reply_outcome(kind, mode=STANDARD, explicit_asked=False) == CONFIRM

    @pytest.mark.parametrize("text", NEVER_CONFIRM)
    @pytest.mark.parametrize("mode", (STANDARD, STRICT))
    @pytest.mark.parametrize("explicit_asked", (False, True))
    def test_negation_other_person_name_or_fragment_never_confirms(
        self, text, mode, explicit_asked,
    ):
        kind = classify_identity_reply(text, "gaurav")
        assert kind is None
        assert identity_reply_outcome(
            kind, mode=mode, explicit_asked=explicit_asked,
        ) is None

    @pytest.mark.parametrize("text", ("haan boliye", "ji boliye", "haan bolo",
                                      "हाँ जी बोलिए।", "हाँ बोलिए।"))
    def test_strict_go_ahead_earns_one_explicit_reask(self, text):
        kind = classify_identity_reply(text, "gaurav")
        assert kind == GO_AHEAD
        assert identity_reply_outcome(kind, mode=STRICT, explicit_asked=False) == EXPLICIT_REASK
        # Any affirm to the explicit question confirms.
        assert identity_reply_outcome(kind, mode=STRICT, explicit_asked=True) == CONFIRM

    @pytest.mark.parametrize("text", (
        "haan main Gaurav bol raha hoon", "हाँ मैं गौरव बोल रहा हूँ", "ji main hi hoon",
        "गौरव बात कर रहा हूँ, आगे बोलो", "this is Gaurav",
    ))
    def test_strict_confirms_a_stated_identity_immediately(self, text):
        kind = classify_identity_reply(text, "Gaurav")
        assert kind == SELF_IDENTIFIED
        assert identity_reply_outcome(kind, mode=STRICT, explicit_asked=False) == CONFIRM

    def test_strict_plain_yes_confirms(self):
        kind = classify_identity_reply("ji haan", "gaurav")
        assert kind == AFFIRM
        assert identity_reply_outcome(kind, mode=STRICT, explicit_asked=False) == CONFIRM

    @pytest.mark.parametrize("text", (
        "main unka bhai bol raha hoon", "मैं उनका बेटा बोल रहा हूँ", "his wife here",
    ))
    def test_regex_fallback_denies_someone_answering_for_the_person(self, text):
        assert classify_identity_answer(text, None) == "deny"

    def test_address_terms_are_not_a_relative(self):
        assert classify_identity_answer("हाँ भाई हाँ", None) == "confirm"


class TestReaskLadder:
    @pytest.mark.parametrize("locale", ("hi-IN", "en-IN", "ml-IN", "ta-IN"))
    @pytest.mark.parametrize("name", ("Gaurav", ""))
    def test_three_distinct_explicit_rungs(self, locale, name):
        rungs = [identity_reask(i, locale, name) for i in range(3)]
        assert len(set(rungs)) == 3
        tracker = IdentityConfirmation(mode=STANDARD, subject_name=name)
        for _ in rungs:
            text = tracker.next_reask(locale)
            assert text in rungs
            # Every rung is recognised as the identity question it asks.
            assert tracker.is_identity_ask(text)
        assert tracker.next_reask(locale) == ""

    def test_hindi_rung_zero_is_the_explicit_question(self):
        assert identity_reask(0, "hi-IN", "Gaurav") == "क्या आप ही Gaurav जी बोल रहे हैं?"

    def test_the_same_wording_is_rephrased_on_its_third_ask(self):
        tracker = IdentityConfirmation(mode=STANDARD, subject_name="gaurav")
        tracker.observe_bot("नमस्ते! " + SPOKEN_ASK)
        # The name in another script is the same wording.
        assert tracker.rephrase_repeated(LLM_REASK, "hi-IN") == ""
        tracker.observe_bot(LLM_REASK)
        assert tracker.times_asked(SPOKEN_ASK) == 2
        # A courtesy word in front does not make it a new wording.
        rephrased = tracker.rephrase_repeated("ठीक है। जी, " + LLM_REASK, "hi-IN")
        assert rephrased == "ठीक है। " + identity_reask(0, "hi-IN", "gaurav")

    def test_a_rung_already_asked_twice_is_skipped(self):
        tracker = IdentityConfirmation(mode=STANDARD, subject_name="Gaurav")
        rung0 = identity_reask(0, "hi-IN", "Gaurav")
        tracker.observe_bot(rung0)
        tracker.observe_bot(rung0)
        assert tracker.next_reask("hi-IN") == identity_reask(1, "hi-IN", "Gaurav")


class TestDecisionPrompt:
    def test_each_mode_states_its_rule(self):
        standard = decision_prompt_rule(STANDARD)
        assert "'haan boliye', 'ji boliye'" in standard and "is decision confirmed" in standard
        assert "galat number" in standard and "main unka bhai/beta" in standard
        assert "'doctor', 'sir'" in standard
        strict = decision_prompt_rule(STRICT)
        assert "'haan boliye', 'ji boliye', 'haan bolo') is ambiguous" in strict
        assert "'haan ho rahi hai'" in strict
        # Strict leaves denials to the base decision rule: restating them
        # there turned "हाँ डॉक्टर बोल रहे हैं" into a denial (bench).
        assert "galat number" not in strict
        explicit = decision_prompt_rule(STRICT, explicit_asked=True)
        assert explicit.startswith(" The bot has already asked explicitly")
        assert "is decision confirmed (signal affirm), also when the yes is followed only by" in explicit

    def test_the_static_decision_prompt_never_carries_it(self):
        # Bench 2026-10-27: as a system-prompt line the rule moved 25% of
        # unrelated decisions; it rides on the identity turns' live state.
        assert "Identity rule" not in GoalEngine(llm=object())._build_system()

    async def test_collections_decides_under_the_strict_rule(self):
        brain = make_agentic_brain(context=snapshot(), llm=_AgenticLLMStub([]))
        assert brain._policy.identity_mode == STRICT
        await brain._say(IDENTITY_QUESTION)
        pending = brain._orchestration_state()["pending_question"]
        assert pending.endswith(decision_prompt_rule(STRICT))
        await turn(brain, "हाँ जी बोलिए।")
        pending = brain._orchestration_state()["pending_question"]
        assert pending.endswith(decision_prompt_rule(STRICT, explicit_asked=True))


# ── the Manappuram pattern on a generic (prompt-driven) bot ──────────────────


def _survey_brain(llm, *, mode=None, runtime_context=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Survey", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You run a feedback survey.",
        greeting=GREETING,
        intents=[{"name": "caller_go_ahead", "samples": ["haan boliye"],
                  "route": "", "confidence_threshold": 0.6}],
    )
    runtime_context = runtime_context or build_runtime_context(
        tenant_id="tn-x", bot_id="bot-x",
        field_definitions=[{"key": "customer_name", "type": "string"}],
        payload={"customer_name": "gaurav"}, payload_source="test",
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        runtime_context=runtime_context,
        finalize_grace=GRACE, complete_endpoint=GRACE,
    )
    brain._pushed, brain._notified = [], []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    if mode is not None:
        brain._identity.mode = mode
    return brain


def _decision_calls(llm):
    return [c for c in llm.generate_calls if c["max_tokens"] != _WARMUP_MAX_TOKENS]


class TestGenericBot:
    async def test_standard_confirms_on_the_first_reply_without_the_decision_llm(self):
        llm = _AgenticLLMStub([AMBIGUOUS, AMBIGUOUS])
        brain = _survey_brain(llm)
        assert brain._policy is None and brain._identity.mode == STANDARD
        assert brain._identity.subject_name == "gaurav"
        await brain._say(GREETING)
        await turn(brain, "हाँ जी बोलिए।")
        assert brain._identity.confirmed
        assert _decision_calls(llm) == []
        assert events(brain, "deterministic_fast_path")[-1]["rule"] == "identity_confirmed"
        # The reply is generated under the bot prompt, told identity is done.
        assert "# Identity confirmed" in llm.calls[-1]["system"]
        replies = bot_replies(brain)
        assert replies[-1] == "ठीक है।"
        assert all(LLM_REASK not in r for r in replies)

    async def test_strict_confirms_on_the_second_reply(self):
        llm = _AgenticLLMStub([AMBIGUOUS, AMBIGUOUS])
        brain = _survey_brain(llm, mode=STRICT)
        await brain._say(GREETING)
        await turn(brain, "हाँ जी बोलिए।")
        assert not brain._identity.confirmed
        assert bot_replies(brain)[-1] == "क्या आप ही gaurav जी बोल रहे हैं?"
        await turn(brain, "हाँ बोलिए।")
        assert brain._identity.confirmed
        assert _decision_calls(llm) == []
        assert [e["outcome"] for e in events(brain, "identity_rule")] == [
            "identity_explicit_reask", "identity_confirmed",
        ]

    async def test_ambiguous_decisions_never_repeat_the_same_question(self):
        llm = _AgenticLLMStub([AMBIGUOUS] * 3)
        brain = _survey_brain(llm)
        await brain._say(GREETING)
        for reply in ("hello hello", "हम्म", "अच्छा"):
            await turn(brain, reply)
        asked = bot_replies(brain)[1:]
        assert asked == [identity_reask(i, "hi-IN", "gaurav") for i in range(3)]
        assert all(LLM_REASK not in text for text in asked)
        assert not brain._identity.confirmed

    async def test_someone_answering_for_the_person_stays_denied(self):
        llm = _AgenticLLMStub([{
            "signal": "wrong_person", "decision": "denied", "scope": "in_scope",
            "confidence": 0.95, "next_action": "answer",
        }])
        brain = _survey_brain(llm)
        await brain._say(GREETING)
        await turn(brain, "main unka bhai bol raha hoon")
        assert len(_decision_calls(llm)) == 1
        assert brain._identity.denied and not brain._identity.confirmed
        assert not events(brain, "deterministic_fast_path")

    async def test_the_decision_sees_the_pending_identity_question(self):
        llm = _AgenticLLMStub([AMBIGUOUS])
        brain = _survey_brain(llm)
        await brain._say(GREETING)
        await turn(brain, "hello hello")
        live_state = _decision_calls(llm)[0]["messages"][-1]["content"]
        assert "identity confirmation" in live_state
        assert decision_prompt_rule(STANDARD) in live_state


# ── the Manappuram pattern on a collections bot (strict by default) ──────────


class TestCollectionsBot:
    async def test_strict_by_default_confirms_on_the_second_reply(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "हाँ जी बोलिए।")
        assert not brain._policy.verified
        assert bot_replies(brain)[-1] == "क्या आप ही Ramesh Kumar जी बोल रहे हैं?"
        await turn(brain, "हाँ बोलिए।")
        assert brain._policy.verified
        assert _decision_calls(llm) == []
        assert [e["rule"] for e in events(brain, "deterministic_fast_path")] == [
            "identity_explicit_reask", "identity_confirmed",
        ]

    async def test_standard_confirms_on_the_first_reply(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        brain._policy.identity_mode = STANDARD
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "हाँ जी बोलिए।")
        assert brain._policy.verified
        assert _decision_calls(llm) == []

    async def test_a_stated_identity_confirms_immediately_in_strict(self):
        brain = make_agentic_brain(context=snapshot(), llm=_AgenticLLMStub([]))
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "हाँ मैं Ramesh बोल रहा हूँ")
        assert brain._policy.verified

    async def test_a_relative_is_the_wrong_party_without_the_decision(self):
        # Decision LLM down: the regex fallback denies the other-person cue.
        from tests.unit.test_brain_collection_policy import _StreamingLLMStub

        brain = make_agentic_brain(context=snapshot(), llm=_StreamingLLMStub())
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "मैं उनका बेटा बोल रहा हूँ")
        assert not brain._policy.verified
        assert brain._policy.wrong_party

    async def test_the_reasks_never_repeat_and_end_in_the_unverified_close(self):
        ambiguous = {
            "intent": "identity_confirmation", "decision": "ambiguous",
            "scope": "in_scope", "confidence": 0.7,
            "next_action": "ask_identity_confirmation",
            "response_text": IDENTITY_QUESTION,
        }
        llm = _AgenticLLMStub([ambiguous] * 4)
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await brain._say(IDENTITY_QUESTION)
        for reply in ("hello hello", "हम्म", "अच्छा", "क्या"):
            await turn(brain, reply)
        asked = bot_replies(brain)[1:4]
        assert asked == [identity_reask(i, "hi-IN", "Ramesh Kumar") for i in range(3)]
        assert IDENTITY_QUESTION not in bot_replies(brain)[1:]
        assert brain._policy.evaluate_completion()[1] == "identity_could_not_be_verified"


def test_policy_strict_go_ahead_waits_for_the_explicit_question_on_regex_too():
    policy = CollectionCallPolicy(context=snapshot(), language="hi-IN")
    policy.observe_bot(IDENTITY_QUESTION)
    # Not readable by the rule (an address term), so the regex decides — and
    # in strict mode a bare go-ahead never confirms on regex alone.
    policy.observe_user("हाँ जी बताइए ना भाई", "affirm")
    assert not policy.verified and policy.identity_unclear_count == 1
    plan = policy.plan_turn("हाँ जी बताइए ना भाई", "affirm")
    assert plan.scripted_reply == identity_reask(0, "hi-IN", "Ramesh Kumar")
    policy.observe_bot(plan.scripted_reply)
    policy.observe_user("हाँ जी बताइए ना भाई", "affirm")
    assert policy.verified


class TestWhatCountsAsTheIdentityQuestion:
    @pytest.mark.parametrize("text", (
        "Is this a good time to talk?", "Are you interested in the offer?",
        "क्या आप अभी बात कर सकते हैं?", "आपका experience कैसा रहा?",
    ))
    def test_ordinary_questions_are_not_identity_asks(self, text):
        for name in ("gaurav", ""):
            tracker = IdentityConfirmation(mode=STANDARD, subject_name=name)
            assert not tracker.is_identity_ask(text)
            tracker.observe_bot(text)
            tracker.observe_bot(text)
            assert tracker.rephrase_repeated(text, "en-IN") == ""

    def test_the_asked_name_makes_it_an_identity_question(self):
        tracker = IdentityConfirmation(mode=STANDARD, subject_name="Gaurav Sharma")
        assert tracker.is_identity_ask("Hello! Is this Gaurav speaking?")
        assert tracker.is_identity_ask("नमस्ते। क्या मेरी बात गौरव शर्मा जी से हो रही है?")
        assert not tracker.is_identity_ask("क्या मेरी बात रमेश जी से हो रही है? ठीक है।")

    def test_a_role_without_a_name_still_counts(self):
        tracker = IdentityConfirmation(mode=STANDARD, subject_name="")
        assert tracker.is_identity_ask("क्या मैं delivery partner से बात कर रहा हूँ?")


class TestGenericBotOrdinaryYes:
    async def test_a_yes_to_another_question_is_judged_by_the_decision(self):
        llm = _AgenticLLMStub([{"signal": "affirm", "scope": "in_scope",
                                "confidence": 0.9, "next_action": "answer"}])
        brain = _survey_brain(llm)
        await brain._say("Is this a good time to talk?")
        await turn(brain, "haan ji")
        assert len(_decision_calls(llm)) == 1
        assert not brain._identity.confirmed
        assert not events(brain, "deterministic_fast_path")
