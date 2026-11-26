"""Hold / wait requests, platform-wide (every tenant, every bot).

Source: DEMO calls cv_274f233cec5a and cv_1f3a067e03ab (Metropolis, 25-26 Nov
2026) and every ``hold_label_rejected`` / ``hold_acknowledged`` event on DEMO
since the corroboration guard shipped (1,841 calls, 18 + 38 events).  The
caller lines below are those calls' transcripts.

What was wrong: the decision layer labelled "जी, मुझे थोड़ा टाइम दीजिए, मैं देख
रहा हूँ।" a hold at 0.98-0.99, the brain dropped the label because the command
lexicon ("ruko", "hold on", "wait" …) did not match, and the turn went to the
workflow as an unmatched answer — "sorry, I did not understand" twice, then
the ask's fallback edge.

1. ``hold_intent``: wait WORDING (a request for time, a duration, staying on
   the line, what the caller is doing) corroborates a model label; refusal,
   "already back" and a dictated number veto it.
2. The brain guard through the REAL turn pipeline: corroborated labels are
   acknowledged and the workflow is not called; uncorroborated ones are
   rejected as before unless the tenant opted in to trusting the model.
3. The wait itself: one acknowledgement per wait, a bare "haan" after it is
   not a turn, a lost transcript does not re-ask, the next real turn ends it,
   and the first prompt after it says the bot is still on the line.
"""

import asyncio
import time

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.orchestration.hold_intent import hold_counter_evidence, hold_evidence
from shared.orchestration.phrases import canned
from shared.orchestration.router import classify_user_signal
from voice_runtime.silence_policy import SilencePolicy

from tests.unit.test_agentic_orchestration import _AgenticLLMStub, events, make_agentic_brain
from tests.unit.test_brain_collection_policy import _WorkflowStub, bot_replies, turn

INTENTS = [
    {"name": "report_status_inquiry", "description": "report status for a partner",
     "samples": ["report ka status batao"], "route": "", "confidence_threshold": 0.6},
]
HOLD_ACK = "जी बिल्कुल, मैं line पर हूँ।"
GENERATED_REPLY = "ठीक है।"          # what the streaming stub produces for a normal reply


def hold_decision(confidence=0.99, **extra):
    return {"intent": "report_status_inquiry", "signal": "hold", "decision": None,
            "scope": "in_scope", "confidence": confidence,
            "reason": "Caller asks for a little time.", "next_action": "continue_workflow",
            **extra}


def affirm_decision():
    return {"intent": None, "signal": "affirm", "decision": None, "scope": "in_scope",
            "confidence": 0.98, "reason": "acknowledges", "next_action": "continue_workflow"}


def answer_decision():
    return {"intent": "report_status_inquiry", "signal": None, "decision": None,
            "scope": "in_scope", "confidence": 0.98, "reason": "provides the value",
            "next_action": "continue_workflow"}


def make_brain(decisions, *, in_workflow=True, trust=False, grace=45.0):
    workflows = _WorkflowStub()
    brain = make_agentic_brain(context=None, intents=INTENTS,
                               llm=_AgenticLLMStub(decisions), workflows=workflows)
    brain._silence_policy = SilencePolicy(prompt_seconds=15.0, retry_seconds=15.0,
                                          max_prompts=3, hold_grace_seconds=grace)
    brain._hold_trust_decision = trust
    if in_workflow:
        brain._active_workflow = "wf-x"
        brain._workflow_ever_routed = True
    return brain, workflows


async def settle(brain):
    if brain._generation is not None:
        await brain._generation
    for _ in range(5):
        await asyncio.sleep(0)


async def say(brain, text):
    await turn(brain, text)
    await settle(brain)


async def say_after_reply_played(brain, text):
    """The previous bot reply was played out and heard before the caller
    speaks (without it the brain merges the words into the previous turn)."""
    await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await say(brain, text)


# ── 1. wording ──────────────────────────────────────────────────────────────

# DEMO turns the decision layer labelled 'hold' and the old guard rejected.
GENUINE_WAITS = [
    ("ठीक जी, मुझे थोड़ा टाइम दीजिए, मैं देख रहा हूँ।", "time_request"),          # cv_274f233cec5a
    ("जी, मुझे थोड़ा टाइम दीजिए, मैं देख रहा हूँ।", "time_request"),
    ("जी मुझे। जी मुझे थोड़ा टाइम दीजिए मैं देख रहा हूँ", "time_request"),          # cv_1f3a067e03ab
    ("तभी। नहीं, मुझे अब Can I, can I, uh, can you just give me some time so that I can "
     "check the VID?", "time_request"),
    ("थोड़ा टाइम दीजिए, मैं देख रहा हूँ।", "time_request"),                      # cv_6165ecb3b795
    ("हाँ, बताता हूँ एक मिनट।", "duration"),                                   # cv_f5500bd5d181
    ("हाय, एक मिनट, एक मिनट।", "duration"),                                   # cv_caa1d0bae579
    ("हेलो, एक मिनट।", "duration"),                                           # cv_85e1bb19cecb
    ("Thanks. Please stay on the line.", "stay_on_line"),                       # cv_76b4ae0fef7a
]
# Same family, other wordings and languages (not from a call).
MORE_WAITS = [
    ("thoda time dijiye", "time_request"), ("mujhe thoda sa time de do", "time_request"),
    ("ज़रा समय दीजिए", "time_request"), ("थोड़ा वक्त चाहिए", "time_request"),
    ("give me a minute", "time_request"), ("give me two minutes", "time_request"),
    ("I need some time", "time_request"), ("ek minute dijiye", "time_request"),
    ("one second please", "duration"), ("just a sec", "duration"), ("bas do minute", "duration"),
    ("ഒരു മിനിറ്റ്", "duration"), ("ஒரு நிமிஷம்", "duration"), ("oru nimisham", "duration"),
    ("bear with me", "stay_on_line"), ("line par hi rahiye", "stay_on_line"),
    ("കാത്തിരിക്കൂ", "stay_on_line"), ("வெயிட் பண்ணுங்க", "stay_on_line"),
    ("main dekh raha hoon", "busy_looking"), ("ढूँढ रहा हूँ", "busy_looking"),
    ("check karta hoon", "busy_looking"), ("let me check", "busy_looking"),
    ("I'm just opening the app", "busy_looking"),
    ("abhi batata hoon", "will_tell"),
]


@pytest.mark.parametrize("text, kind", GENUINE_WAITS + MORE_WAITS)
def test_wait_wording_corroborates(text, kind):
    assert hold_evidence(text) == kind
    assert hold_counter_evidence(text) is None


# Labelled 'hold' by the model on DEMO, and rightly not honoured.
@pytest.mark.parametrize("text, evidence, counter", [
    ("नहीं कट करो।", None, None),                                             # cv_f24ba10ecf03
    ("नहीं, कोई दिक्कत नहीं है, मत मत करो।", None, None),
    ("जी बस बस बस बस बस बस बस।", None, None),
    ("एक मिनट यार। हो गया।", "duration", "caller_back"),                       # already back
    ("हाँ एक सेकंड। जश्न में तो मैं। अर्जुन ने ही पॉज किया। ठीक है। हाँ जी।", "duration", "caller_back"),
    ("हलो। आने वाला है, दो मिनट में वो। बस तुम बैठो पाँच मिनट। हलो।", "duration", "caller_back"),
    # the two MDND turns the first guard was written for
    ("Koi nahi hai sir", None, None),
    ("Haan bataiye kya bol rahe ho sir", None, None),
])
def test_demo_labels_that_stay_rejected(text, evidence, counter):
    assert hold_evidence(text) == evidence
    assert hold_counter_evidence(text) == counter


@pytest.mark.parametrize("text, counter", [
    ("mere paas time nahi hai", "negated"), ("wait nahi kar sakta", "negated"),
    ("I can't wait", "negated"), ("ruko mat, bolo", "negated"), ("why am I on hold", "negated"),
    ("hold kyu kar rahe ho", "negated"), ("कब तक वेट करूँ", "negated"),
    ("ek minute, 2600 7010 5230", "answer_digits"),
    ("एक मिनट… हाँ बोलिए", "caller_back"), ("ek second, haan ji", "caller_back"),
])
def test_counter_evidence(text, counter):
    assert hold_counter_evidence(text) == counter


def test_refusing_to_cut_is_not_refusing_to_wait():
    assert hold_counter_evidence("ruko mat kato") is None
    assert classify_user_signal("ruko mat kato") == "hold"


def test_ordinary_statements_carry_no_wait_wording():
    for text in ("haan sahi hai", "theek hai", "time kya hua hai", "mera report kab aayega",
                 "checking account ka balance batao", "main Rahul bol raha hoon",
                 "online payment kar diya", "ek minute mein call cut ho gaya tha"):
        assert hold_evidence(text) is None or hold_counter_evidence(text), text


def test_command_lexicon_counts_whole_words_only():
    """DEMO cv_671f59b01d2b / cv_ba7bd69eca05 were acknowledged as holds."""
    assert classify_user_signal("मेरे आउटलेट का नाम है ट्रिस्टा कार प्राइवेट लिमिटेड।") != "hold"
    assert classify_user_signal("क्लाइन पे ही जाना पड़ेगा। वो ऑन रोमेंट ही जाएगी।") != "hold"
    for text in ("एक मिनट वेट करो।", "वेट", "wait", "कृपया लाइन पर बने रहें।", "line par rahiye",
                 "एक मिनट रुको।", "हां, एक मिनट रुक जाओ", "hold on", "kat mat karo"):
        assert classify_user_signal(text) == "hold", text


# ── 2. the guard, through the real turn pipeline ────────────────────────────


@pytest.mark.parametrize("text, kind", GENUINE_WAITS)
async def test_demo_wait_requests_are_acknowledged_and_the_workflow_is_not_called(text, kind):
    brain, workflows = make_brain([hold_decision()])
    await brain._say("धन्यवाद। क्या आप please patient Visit ID, बता सकते हैं?")
    await say(brain, text)
    assert events(brain, "hold_label_rejected") == []
    assert events(brain, "hold_label_corroborated")[-1]["evidence"] == kind
    assert len(events(brain, "hold_acknowledged")) == 1
    assert bot_replies(brain)[-1] == HOLD_ACK
    assert workflows.calls == []                    # node, slots and retry budget untouched
    assert brain._active_workflow == "wf-x"
    assert brain._hold_active()
    route = events(brain, "route_decision")[-1]
    assert route["signal"] == "hold"


async def test_label_without_wording_is_still_rejected_and_measured():
    brain, workflows = make_brain([hold_decision(0.95)])
    await brain._say("ये order आपने किसको सौंपा था?")
    await say(brain, "Koi nahi hai sir")
    rejected = events(brain, "hold_label_rejected")[-1]
    assert rejected == {"source": "decision", "evidence": None, "counter": None,
                        "would_trust": True}
    assert events(brain, "hold_acknowledged") == []
    assert workflows.calls and workflows.calls[-1]["signal"] is None
    assert not brain._hold_active()


@pytest.mark.parametrize("text, counter", [
    ("एक मिनट यार। हो गया।", "caller_back"),
    ("ek minute, 2600 7010 5230", "answer_digits"),
    ("mere paas time nahi hai", "negated"),
])
async def test_counter_evidence_rejects_the_label(text, counter):
    brain, workflows = make_brain([hold_decision()])
    await say(brain, text)
    rejected = events(brain, "hold_label_rejected")[-1]
    assert rejected["counter"] == counter and rejected["would_trust"] is False
    assert events(brain, "hold_acknowledged") == []
    assert workflows.calls                          # the workflow got the turn


async def test_low_confidence_label_is_not_honoured_even_with_wording():
    brain, _ = make_brain([hold_decision(0.3)])
    await say(brain, "Just opening")                # cv_94c6883f95ed: confidence 0.0
    assert events(brain, "hold_acknowledged") == []
    assert events(brain, "hold_label_rejected")[-1]["evidence"] == "busy_looking"


async def test_tenant_opt_in_trusts_a_confident_label_in_an_unlisted_form():
    text = "Enikku kurachu samayam venam"           # Malayalam in Roman script: not listed
    assert hold_evidence(text) is None and classify_user_signal(text) != "hold"
    off, _ = make_brain([hold_decision(0.97)])
    await say(off, text)
    assert events(off, "hold_acknowledged") == []
    assert events(off, "hold_label_rejected")[-1]["would_trust"] is True

    on, workflows = make_brain([hold_decision(0.97)], trust=True)
    await say(on, text)
    assert events(on, "hold_label_corroborated")[-1]["evidence"] == "decision_trusted"
    assert bot_replies(on)[-1] == HOLD_ACK and workflows.calls == []


@pytest.mark.parametrize("text, confidence", [
    ("Haan bataiye kya bol rahe ho sir", 0.97),     # a question
    ("Koi nahi hai sir", 0.8),                      # not confident enough
    ("mere paas time nahi hai", 0.99),              # negated
])
async def test_opt_in_still_refuses_questions_low_confidence_and_refusals(text, confidence):
    brain, _ = make_brain([hold_decision(confidence)], trust=True)
    await say(brain, text)
    assert events(brain, "hold_acknowledged") == []
    assert events(brain, "hold_label_rejected")


# ── 3. the wait itself ──────────────────────────────────────────────────────


async def test_one_wait_is_acknowledged_once():
    """cv_6165ecb3b795: "एक मिनट रुको।" … "एक मिनट वेट करो।" six seconds apart;
    cv_db7bcc7adfb9: a carrier hold announcement, acknowledged twelve times."""
    brain, workflows = make_brain([hold_decision(), hold_decision(), hold_decision()])
    await say(brain, "एक मिनट रुको।")
    first = brain._hold_requested_at
    await say_after_reply_played(brain, "एक मिनट वेट करो।")
    await say(brain, "कृपया लाइन पर बने रहें।")
    assert len(events(brain, "hold_acknowledged")) == 1
    assert bot_replies(brain).count(HOLD_ACK) == 1
    assert [e["extended"] for e in events(brain, "hold_extended")] == [True, True]
    assert brain._hold_requested_at > first         # the quiet starts again
    assert workflows.calls == []


async def test_a_wait_cannot_be_extended_for_ever():
    brain, _ = make_brain([hold_decision(), hold_decision()], grace=45.0)
    await say(brain, "एक मिनट रुको।")
    # the first request was two-and-a-half grace periods ago; the last one 30 s ago
    now = time.monotonic()
    brain._hold_episode_started_at = now - 2.5 * 45.0
    brain._hold_requested_at = now - 30.0
    await say_after_reply_played(brain, "एक मिनट वेट करो।")
    assert events(brain, "hold_extended")[-1]["extended"] is False
    assert brain._hold_requested_at == pytest.approx(now - 30.0, abs=0.01)


async def test_bare_yes_after_the_acknowledgement_is_not_a_turn():
    """cv_9b2f1839a044: "हाँ।" then "जी।" after the acknowledgement were each
    answered and the Visit ID question asked again, twice in three seconds."""
    brain, workflows = make_brain([hold_decision(), affirm_decision(), affirm_decision()])
    await brain._say("धन्यवाद। क्या आप please patient Visit ID, बता सकते हैं?")
    await say(brain, "एक मिनट रुको।")
    replies = len(bot_replies(brain))
    await say_after_reply_played(brain, "हाँ।")
    await say(brain, "जी।")
    assert len(events(brain, "hold_ack_reply_ignored")) == 2
    assert len(bot_replies(brain)) == replies       # nothing said
    assert workflows.calls == [] and brain._hold_active()


async def test_a_real_answer_ends_the_wait_and_runs_normally():
    brain, workflows = make_brain([hold_decision(), answer_decision()])
    await say(brain, "थोड़ा टाइम दीजिए, मैं देख रहा हूँ।")
    assert brain._hold_active()
    await say_after_reply_played(brain, "haan sahi hai, likh lijiye")
    assert events(brain, "hold_ack_reply_ignored") == []
    assert events(brain, "hold_ended")[-1]["reason"] == "caller_turn"
    assert brain._hold_requested_at is None         # no stale quiet on later silences
    assert workflows.calls                          # the workflow got the answer


async def test_bare_yes_long_after_the_acknowledgement_is_a_turn_again():
    brain, workflows = make_brain([hold_decision(), affirm_decision()])
    await say(brain, "एक मिनट रुको।")
    brain._hold_acknowledged_at = time.monotonic() - 20.0     # the caller comes back later
    await say_after_reply_played(brain, "हाँ।")
    assert events(brain, "hold_ack_reply_ignored") == []
    assert events(brain, "hold_ended")


async def test_lost_transcript_during_a_wait_does_not_ask_again():
    """cv_9b2f1839a044: "माफ़ कीजिए, समझ नहीं आया … Visit ID?" two seconds after
    "मैं line पर हूँ"."""
    brain, _ = make_brain([hold_decision()])
    await brain._say("धन्यवाद। क्या आप please patient Visit ID, बता सकते हैं?")
    await say(brain, "एक मिनट रुको।")
    replies = len(bot_replies(brain))
    brain._pending_missing_stt_generation = 12
    assert await brain._resume_missing_stt_final() is False
    assert events(brain, "stt_final_missing_ignored")[-1] == {"generation": 12,
                                                              "reason": "hold_active"}
    assert events(brain, "stt_repeat_requested") == []
    assert len(bot_replies(brain)) == replies


async def test_lost_transcript_outside_a_wait_still_asks_again():
    brain, _ = make_brain([])
    await brain._say("धन्यवाद। क्या आप please patient Visit ID, बता सकते हैं?")
    brain._pending_missing_stt_generation = 12
    assert await brain._resume_missing_stt_final() is True
    assert events(brain, "stt_repeat_requested")


async def test_first_prompt_after_the_wait_says_the_bot_is_still_on_the_line():
    brain, _ = make_brain([hold_decision()], grace=0.05)
    await say(brain, "थोड़ा टाइम दीजिए, मैं देख रहा हूँ।")
    await brain._on_silence_timeout()
    prompt = events(brain, "silence_prompt")[-1]
    assert prompt["attempt"] == 1 and prompt["after_hold"] is True
    assert bot_replies(brain)[-1] == canned("hold_checkin", "hi-IN")
    assert events(brain, "hold_ended")[-1]["reason"] == "grace_spent"
    # the ladder after it is the normal one
    await brain._on_silence_timeout()
    assert events(brain, "silence_prompt")[-1]["after_hold"] is False
    assert bot_replies(brain)[-1] == canned("silence_check_2", "hi-IN")


async def test_silence_without_a_wait_keeps_the_usual_first_prompt():
    brain, _ = make_brain([])
    await brain._on_silence_timeout()
    assert events(brain, "silence_prompt")[-1]["after_hold"] is False
    assert bot_replies(brain)[-1] == canned("silence_check_1", "hi-IN")


def test_hold_checkin_phrase_exists_in_every_language():
    for language in ("en-IN", "hi-IN", "ml-IN", "ta-IN"):
        assert canned("hold_checkin", language).strip()
    assert "line" in canned("hold_checkin", "en-IN")


# ── tenant settings ─────────────────────────────────────────────────────────


def test_hold_wait_time_is_the_platform_value_until_the_tenant_changes_it(monkeypatch):
    from shared.turn_detection import TURN_DETECTION_BOUNDS, TURN_DETECTION_DEFAULTS
    from voice_runtime import pipeline

    assert TURN_DETECTION_BOUNDS["silence_hold_grace_seconds"] == (15.0, 120.0)
    for transport in TURN_DETECTION_DEFAULTS.values():
        assert transport["silence_hold_grace_seconds"] == 45.0
        assert transport["hold_trust_decision_enabled"] == 0.0     # opt-in

    class _Settings:
        silence_prompt_seconds = 15.0
        silence_retry_seconds = 15.0
        silence_max_prompts = 3
        silence_hold_grace_seconds = 60.0           # platform override (env)

    monkeypatch.setattr(pipeline, "get_settings", lambda: _Settings())
    resolve = pipeline.resolve_silence_policy
    assert resolve({}).hold_grace_seconds == 60.0
    assert resolve({"silence_hold_grace_seconds": 45.0}).hold_grace_seconds == 60.0   # default
    assert resolve({"silence_hold_grace_seconds": 90.0}).hold_grace_seconds == 90.0   # tenant
    assert resolve({"silence_hold_grace_seconds": 500.0}).hold_grace_seconds == 120.0  # clamped
    assert resolve({"silence_hold_grace_seconds": "junk"}).hold_grace_seconds == 60.0


# ── no model reading this turn (decision / classifier failed or timed out) ───


class _NoDecisionLLM(_AgenticLLMStub):
    """Stage A never answers: the turn is read by regex alone."""

    async def generate(self, messages, **kwargs):
        raise asyncio.TimeoutError()


def make_unread_brain(**kwargs):
    workflows = _WorkflowStub()
    brain = make_agentic_brain(context=None, intents=INTENTS, llm=_NoDecisionLLM([]),
                               workflows=workflows, **kwargs)
    brain._active_workflow = "wf-x"
    brain._workflow_ever_routed = True
    return brain, workflows


@pytest.mark.parametrize("text, kind", [
    ("जी मुझे थोड़ा टाइम दीजिए मैं देख रहा हूँ", "time_request"),      # LOCAL voice run 2026-11-26
    ("can you just give me some time so that I can check the VID?", "time_request"),
    ("Thanks. Please stay on the line.", "stay_on_line"),
])
async def test_explicit_wait_request_stands_without_a_model_reading(text, kind):
    assert classify_user_signal(text) != "hold"
    brain, workflows = make_unread_brain()
    await brain._say("कृपया अपनी पूरी Terminal ID, उसमें आने वाले letters के साथ बताइए।")
    await say(brain, text)
    assert events(brain, "hold_label_corroborated")[-1] == {"source": "wording", "evidence": kind}
    assert bot_replies(brain)[-1] == HOLD_ACK
    assert workflows.calls == []


@pytest.mark.parametrize("text", [
    "main dekh raha hoon",                 # an ordinary sentence without a label
    "हाँ, बताता हूँ एक मिनट।",
    "mujhe kal ka time de do",             # an appointment, not a pause
    "शाम 5 बजे का time दीजिए",
    "visit ka time dijiye",
    "2 din ka time de do",
    "mere paas time nahi hai",
    "thoda time dijiye, 2600 7010 5230",   # carries the answer
    "kitna time lagega?",                  # a question about time
    "how much time will it take?",
])
async def test_softer_or_other_time_wording_needs_the_model(text):
    brain, workflows = make_unread_brain()
    await say(brain, text)
    assert events(brain, "hold_acknowledged") == []
    assert events(brain, "hold_label_corroborated") == []
    assert workflows.calls                 # handled as a normal turn


def test_explicit_wait_request_kinds():
    from shared.orchestration.hold_intent import explicit_wait_request

    assert explicit_wait_request("thoda time dijiye") == "time_request"
    assert explicit_wait_request("line par rahiye") == "stay_on_line"
    for text in ("एक मिनट", "let me check", "abhi batata hoon", "payment ke liye thoda time chahiye",
                 "kaun sa time doge", "I need some time to pay"):
        assert explicit_wait_request(text) is None, text
