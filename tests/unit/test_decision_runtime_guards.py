"""Runtime guards on Goal Engine decisions — driven through the REAL turn
pipeline (router → decision → routing → reply), not the parser alone.

1. A model ``hold`` label the deterministic hold lexicon does not corroborate
   is dropped: the bot answers the turn instead of acknowledging and waiting
   in silence (live MDND calls: "Koi nahi hai sir", "haan bataiye kya bol
   rahe ho sir" — the second spoken right after a hold acknowledgement, a
   hold loop). A genuine hold still gets the acknowledgement.
2. The rejected label never reaches the workflow engine as its signal, so an
   active ask cannot be parked off-script by it.
3. A wrong-number / "not me" reply the model marked ``out_of_scope`` stays in
   scope end-to-end: no goal redirect, and the ``wrong_person`` signal survives
   into the turn (the redirect path strips it).
"""

import asyncio

from voice_runtime.brain import ConversationBrain

from tests.unit.test_agentic_orchestration import (
    _AgenticLLMStub,
    events,
    make_agentic_brain,
)
from tests.unit.test_brain_collection_policy import _WorkflowStub, bot_replies, turn

INTENTS = [
    {"name": "billing_query", "description": "billing problems",
     "samples": ["bill galat aaya"], "route": "", "confidence_threshold": 0.6},
]
HOLD_DECISION = {
    "intent": None, "signal": "hold", "decision": None, "scope": "in_scope",
    "confidence": 0.9, "reason": "caller asks to wait", "next_action": "answer",
}
GENERATED_REPLY = "ठीक है।"  # what the streaming stub produces for Stage-B
# Authored goal policy that turns identity gating ON for a generic bot.
GATED_POLICY = {
    "goals": [{"id": "resolve", "description": "Resolve the caller's billing query."}],
    "identity": {"requireConfirmation": True, "subject": "Ramesh Kumar"},
}


def generic_brain(decisions, **kwargs) -> ConversationBrain:
    # No collections context: a generic bot, engine enabled by its intents.
    return make_agentic_brain(
        context=None, intents=INTENTS, llm=_AgenticLLMStub(decisions), **kwargs,
    )


async def settle(brain):
    if brain._generation is not None:
        await brain._generation
    for _ in range(5):
        await asyncio.sleep(0)


class TestHoldLabelCorroboration:
    async def test_uncorroborated_hold_is_rejected_and_the_turn_is_answered(self):
        brain = generic_brain([dict(HOLD_DECISION)])
        await brain._say("ये order आपने किसको सौंपा था?")
        await turn(brain, "Koi nahi hai sir")
        await settle(brain)
        assert events(brain, "hold_acknowledged") == []
        rejected = events(brain, "hold_label_rejected")
        assert rejected and rejected[-1]["source"] == "decision"
        assert brain._hold_requested_at is None
        assert brain._latest_caller_signal == ""
        # The turn was answered normally (Stage-B generation), not parked.
        assert bot_replies(brain)[-1] == GENERATED_REPLY

    async def test_hold_loop_after_an_acknowledgement_is_broken(self):
        brain = generic_brain([dict(HOLD_DECISION)])
        # The bot has just acknowledged a hold; the caller asks what it said.
        await brain._acknowledge_hold(brain._router.decide("ek minute ruko"))
        await turn(brain, "Haan bataiye kya bol rahe ho sir")
        await settle(brain)
        assert len(events(brain, "hold_acknowledged")) == 1  # only the first one
        assert events(brain, "hold_label_rejected")
        assert bot_replies(brain)[-1] == GENERATED_REPLY

    async def test_genuine_hold_is_still_acknowledged(self):
        brain = generic_brain([dict(HOLD_DECISION)])
        await brain._say("क्या आप delivery के लिए customer की location पर पहुंचे थे?")
        await turn(brain, "ek minute rukiye")
        await settle(brain)
        assert events(brain, "hold_label_rejected") == []
        acks = events(brain, "hold_acknowledged")
        assert acks and acks[-1]["route"] in ("chat", "clarify", "intent")
        assert brain._hold_requested_at is not None
        assert bot_replies(brain)[-1] != GENERATED_REPLY  # the canned hold ack

    async def test_rejected_hold_never_reaches_the_workflow_signal(self):
        workflows = _WorkflowStub()
        brain = generic_brain([dict(HOLD_DECISION)], workflows=workflows)
        brain._active_workflow = "wf-x"
        brain._workflow_ever_routed = True
        await turn(brain, "Koi nahi hai sir")
        await settle(brain)
        assert events(brain, "hold_acknowledged") == []
        assert workflows.calls, "the active workflow did not get the turn"
        assert workflows.calls[-1]["signal"] is None


class TestScopeGuardEndToEnd:
    async def test_wrong_number_marked_out_of_scope_is_not_redirected(self):
        brain = generic_brain([{
            "intent": None, "signal": "wrong_person", "decision": "unrelated",
            "scope": "out_of_scope", "confidence": 0.9,
            "reason": "caller is not the customer", "next_action": "end_call",
        }])
        await brain._say("क्या मेरी बात Rohan जी से हो रही है?")
        await turn(brain, "Wrong number")
        await settle(brain)
        assert events(brain, "scope_redirect") == []
        decided = events(brain, "orchestration_decision")
        assert decided and decided[-1]["scope"] == "in_scope"
        assert decided[-1]["signal"] == "wrong_person"
        assert brain._latest_caller_signal == "wrong_person"
        rows = events(brain, "orchestration_turn")
        assert rows and rows[-1]["scope"] == "in_scope"

    async def test_rescued_wrong_person_denies_identity_on_a_gated_bot(self):
        # A generic bot with authored identity gating (GoalSession): the
        # rescued decision carries 'denied', so the gate records a denial —
        # never a confirmation.
        brain = make_agentic_brain(
            context=None, goal_policy=GATED_POLICY,
            llm=_AgenticLLMStub([{
                "intent": None, "signal": "wrong_person", "decision": "denied",
                "scope": "out_of_scope", "confidence": 0.9,
                "reason": "wrong number", "next_action": "ask_identity_confirmation",
            }]),
        )
        assert brain._goal_session is not None
        assert brain._goal_session.identity_state == "unconfirmed"
        await brain._say("क्या मेरी बात Ramesh Kumar जी से हो रही है?")
        await turn(brain, "Wrong number")
        await settle(brain)
        assert events(brain, "scope_redirect") == []
        assert brain._goal_session.identity_state == "denied"

    async def test_contradictory_wrong_person_confirmed_never_confirms_identity(self):
        # The model said wrong_person AND decision 'confirmed' while out of
        # scope (seen live: "Actually main customer nahi hoon"). The guard
        # refuses to rescue it: scope rules keep today's redirect, the gate
        # stays unconfirmed, and a non-customer is never confirmed.
        brain = make_agentic_brain(
            context=None, goal_policy=GATED_POLICY,
            llm=_AgenticLLMStub([{
                "intent": None, "signal": "wrong_person", "decision": "confirmed",
                "scope": "out_of_scope", "confidence": 0.9,
                "reason": "not the customer", "next_action": "end_call",
            }]),
        )
        await brain._say("क्या मेरी बात Ramesh Kumar जी से हो रही है?")
        await turn(brain, "Actually main customer nahi hoon")
        await settle(brain)
        assert brain._goal_session.identity_state == "unconfirmed"
        decided = events(brain, "orchestration_decision")
        assert decided and decided[-1]["scope"] == "out_of_scope"
        assert decided[-1]["decision"] != "confirmed"
        assert events(brain, "scope_redirect")

    async def test_in_scope_wrong_person_confirmed_never_confirms_a_gated_bot(self):
        # In scope, the model contradicts itself: signal wrong_person AND
        # decision 'confirmed'. A wrong-number / "not me" signal negates the
        # identity gate; the gate must record a denial, never a confirmation.
        brain = make_agentic_brain(
            context=None, goal_policy=GATED_POLICY,
            llm=_AgenticLLMStub([{
                "intent": None, "signal": "wrong_person", "decision": "confirmed",
                "scope": "in_scope", "confidence": 0.9,
                "reason": "says they are not the customer", "next_action": "end_call",
            }]),
        )
        await brain._say("क्या मेरी बात Ramesh Kumar जी से हो रही है?")
        await turn(brain, "Actually main customer nahi hoon")
        await settle(brain)
        assert brain._goal_session.identity_state == "denied"
        decided = events(brain, "orchestration_decision")
        assert decided and decided[-1]["decision"] == "denied"
        assert decided[-1]["signal"] == "wrong_person"

    async def test_in_scope_wrong_person_confirmed_never_verifies_a_collections_caller(self):
        from tests.unit.test_brain_collection_policy import snapshot

        brain = make_agentic_brain(
            context=snapshot(),
            llm=_AgenticLLMStub([{
                "intent": None, "signal": "wrong_person", "decision": "confirmed",
                "scope": "in_scope", "confidence": 0.9,
                "reason": "wrong number", "next_action": "end_call",
            }]),
        )
        await brain._say("क्या मेरी बात Ramesh Kumar जी से हो रही है?")
        await turn(brain, "Wrong number, Ramesh naam ka koi nahi hai yahan")
        await settle(brain)
        assert brain._policy.wrong_party is True
        assert brain._policy.verified is False

    async def test_legitimate_confirmation_still_confirms_a_gated_bot(self):
        brain = make_agentic_brain(
            context=None, goal_policy=GATED_POLICY,
            llm=_AgenticLLMStub([{
                "intent": "identity_confirmation", "signal": "affirm", "decision": "confirmed",
                "scope": "in_scope", "confidence": 0.95,
                "reason": "caller confirmed", "next_action": "continue_workflow",
            }]),
        )
        await brain._say("क्या मेरी बात Ramesh Kumar जी से हो रही है?")
        await turn(brain, "haan ji, Ramesh bol raha hoon")
        await settle(brain)
        assert brain._goal_session.identity_state == "confirmed"

    async def test_genuinely_off_goal_turn_is_still_redirected(self):
        brain = generic_brain([{
            "intent": None, "signal": None, "decision": None,
            "scope": "out_of_scope", "confidence": 0.9,
            "reason": "asks for a joke", "next_action": "redirect_to_goal",
        }])
        await turn(brain, "koi joke sunao yaar")
        await settle(brain)
        assert events(brain, "scope_redirect")
