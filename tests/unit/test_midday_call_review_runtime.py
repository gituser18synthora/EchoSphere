"""18 November review (DEMO, 18 calls after 06:37 UTC): shared runtime changes.

* a negation always beats the natural affirmatives of the identifier gate;
* "the letter comes at the end" amends the pending identifier;
* "Eight for Alpha" is the letter A;
* missing-STT recovery is bounded;
* the opt-in TTS safety net speaks raw identifier digits as English words;
* a reply that produced no audio is retried once by the TTS router;
* the opt-in unheard-reply guard repeats a reply instead of consuming the
  yes/no spoken over it, once per reply.
"""

import asyncio
import time

import pytest
from pipecat.frames.frames import TextFrame

from shared.audio.text import english_identifier_digits, sanitize_for_tts
from shared.orchestration.identifier_confirmation import (
    amend_identifier_suffix, confirmation_answer, identifier_sequence, identifier_suffix,
)
from shared.providers.base import ProviderError
from shared.providers.tts.streaming import TTSStreamEvent
from shared.turn_detection import TURN_DETECTION_DEFAULTS
from tests.unit.test_brain_workflow_routing import make_brain as make_workflow_brain
from tests.unit.test_identifier_capture_runtime import frame_in, make_brain
from tests.unit.test_tts_router_flush_finals import KEY, FakeProvider, FakeRecorder, make_router
from voice_runtime.frames import ReplyTTSCompleteFrame, ReplyTTSInterruptedFrame
from voice_runtime.stt_recovery import STTFinalMissingFrame

TID = {"id": "n", "kind": "ask", "config": {"variable": "tid", "entity": {
    "dataType": "identifier", "preserveAlphanumeric": True,
    "regexPattern": r"(?<![A-Za-z0-9])((?:(?:[A-Za-z]|ए|एम)(?:\s+for\s+[A-Za-z]+)?[\s-]*)?\d(?:[\s-]?\d){6,7}"
                    r"(?:[\s-]*(?:[A-Za-z]|ए|एम))?)(?![A-Za-z0-9])",
}}}


# ── b. negation wins over the new affirmatives ──────────────────────────────

@pytest.mark.parametrize("text", [
    "theek nahi hai", "sahi nahi hai", "barobar nahi", "yah sahi nahi hai",
    "ठीक नहीं है", "सही नहीं है", "बरोबर नहीं", "बरोबर नाही", "यह सही नहीं है",
    "ye theek nahi", "bilkul nahi", "perfect nahi", "nahi barobar", "haan sahi nahi hai",
    "नहीं, आईडी सही नहीं है", "हाँ जी। नहीं, आईडी सही नहीं है।", "No, it's not correct.", "नहीं।",
])
def test_a_negated_affirmative_is_no(text):
    assert confirmation_answer(text) == "no"


@pytest.mark.parametrize("text", [
    "theek hai", "barobar", "बरोबर.", "ठीक है।", "हाँ, यह सही है।", "haan, yah sahi hai",
    "हाँ जी, बिल्कुल सही है।", "It's correct.",
])
def test_the_natural_confirmations_still_confirm(text):
    assert confirmation_answer(text) == "yes"


@pytest.mark.parametrize("text", [
    "हाँ, बिल्कुल सुन पा रहे हैं।",        # cv_cd292d6c02cd: answers "can you hear me?", not the ID
    "आपने लास्ट में ए नहीं बोला।", "नहीं नहीं आई है।", "Okay", "it is", "सही है क्या?",
])
def test_anything_else_stays_undecided(text):
    assert confirmation_answer(text) is None


# ── "the letter comes at the end" (cv_cd292d6c02cd, cv_a9af6b51d328) ────────

@pytest.mark.parametrize("text", [
    "आपने लास्ट में ए नहीं बोला।", "नहीं। लास्ट में ए है। लास्ट में ए है।", "लास्ट में ए है",
    "last mein A hai", "उसके बाद। A for Apple", "at the end A for Apple", "A for Apple", "और ए",
])
def test_a_trailing_letter_statement_amends_the_pending_identifier(text):
    assert identifier_suffix(text) == "A"
    assert amend_identifier_suffix(TID, "tid", "2544083", text) == "2544083A"


@pytest.mark.parametrize("text", [
    "लास्ट में ए है। लास्ट में बी है।", "लास्ट में नहीं है", "आखिर में क्या है?", "last mein 5 hai",
    "लास्ट में सही है", "Ki A for F pao", "नहीं नहीं आई है।", "हाँ जी।",
])
def test_other_prose_is_never_a_suffix(text):
    assert identifier_suffix(text) is None


# ── "Eight for Alpha" (cv_a3a2e9c1d463) ─────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("301134 Six, Eight for Alpha.", "3011346A"),
    ("3011346 8 for Apple", "3011346A"),
    ("1317317 एट फॉर एप्पल", "1317317A"),
    ("1 4 3 2 4 2 1 A for Alpha", "1432421A"),            # unchanged
    ("2 5 Four Four 0 Eight Three A, A for Apple", "2544083A"),   # a real eight stays
])
def test_eight_spelled_with_an_a_word_is_the_letter_a(text, expected):
    assert identifier_sequence(text) == expected


def test_eight_before_another_spelling_word_is_left_alone():
    assert identifier_sequence("Eight for Elephant 1234567") != "A1234567"


# ── b. missing-STT recovery cannot loop ─────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_final_recovery_stops_after_two_prompts():
    brain = make_brain()
    brain._active_workflow = "wf-1"
    brain._pending_workflow_question = "कौन-सा option?"
    for generation in range(1, 7):
        await frame_in(brain, STTFinalMissingFrame(generation=generation))
    spoken = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
    assert len(spoken) == 2 and brain._stt_repeat_prompts == 2
    await brain.cleanup()


# ── c. TTS safety net ───────────────────────────────────────────────────────

SAFETY_NET = [
    # cv_4cecac598f1c 07:00:12 (PIN code in the address)
    ("address Ward Number 12 Shop Number 14 Gola Bazar Sherghati Gaya, Bihar 824211 है?",
     "address Ward Number 12 Shop Number 14 Gola Bazar Sherghati Gaya, Bihar eight, two, four, two, one, one है?"),
    # cv_f3800137eb10 06:46:46
    ("address M S Sadhana Ganadhi Quare, Karnataka 570001 है?",
     "address M S Sadhana Ganadhi Quare, Karnataka five, seven, zero, zero, zero, one है?"),
    # 17 Nov texts as they reached TTS before workflow v55
    ("क्या आपका registered contact number 9880905195 है?",
     "क्या आपका registered contact number nine, eight, eight, zero, nine, zero, five, one, nine, five है?"),
    ("T-I-D number 2 5 4 4 0 8 3A, और address",
     "T-I-D number two, five, four, four, zero, eight, three, letter A, और address"),
    ("आपका docket number है WL458921।",
     "आपका docket number है letter W, letter L, four, five, eight, nine, two, one।"),
    ("Your Terminal ID is 2544083A.", "Your Terminal ID is two, five, four, four, zero, eight, three, letter A."),
]
UNTOUCHED = [
    "क्या आपके terminal का model Linura 7220 Handheld GPRS है?",
    "क्या आपके terminal का model Antera AL9220U Handheld GPRS है?",
    "model Antera A9210 Handheld GPRS", "Paytivo 7210 Handheld GPRS",
    "terminal को charger से कम से कम 4 घंटे तक connected रखिए। battery percentage 80% या उससे ज़्यादा",
    "address 209 A 9 Westend Rajendra Nagar, Hukmakhedi A B Road Indore",
    "Ward Number 12 Shop Number 14", "Quantity one से nine के बीच होनी चाहिए।",
    "अब तक 6 अंक नोट कर लिए हैं — कृपया आगे बताइए।",
    "Loan amount ₹150000 and EMI 12500 rupees due on 2026-11-18 at 10:30.",
    "Total 15000 रुपये", "12,50,000 and 3.14159",
    "Terminal ID two, five, four, four, zero, eight, three, letter A. क्या यह सही है?",
]


@pytest.mark.parametrize("raw,spoken", SAFETY_NET)
def test_safety_net_speaks_identifier_digits_in_english(raw, spoken):
    once = sanitize_for_tts(raw, english_identifier_digits_enabled=True)
    assert once == spoken
    assert sanitize_for_tts(once, english_identifier_digits_enabled=True) == once   # already converted


@pytest.mark.parametrize("text", UNTOUCHED)
def test_safety_net_leaves_models_amounts_quantities_and_house_numbers(text):
    assert english_identifier_digits(text) == text
    assert sanitize_for_tts(text, english_identifier_digits_enabled=True) == sanitize_for_tts(text)


@pytest.mark.parametrize("raw,_spoken", SAFETY_NET)
def test_safety_net_is_off_unless_the_tenant_opts_in(raw, _spoken):
    from shared.audio.text import verbalize_booking_ids
    assert sanitize_for_tts(raw) == verbalize_booking_ids(raw)      # today's behaviour, unchanged
    for transport in TURN_DETECTION_DEFAULTS.values():
        assert transport["tts_english_identifier_digits"] == 0.0
        assert transport["unheard_reply_guard_enabled"] == 0.0


@pytest.mark.asyncio
async def test_router_applies_the_safety_net_only_with_the_tenant_flag():
    recorder = FakeRecorder()
    router = make_router(recorder=recorder)
    raw = "contact number 8340294140 है"
    assert await router._sanitize_transform(raw, None) == "contact number 8 3 4 0 2 9 4 1 4 0 है"
    recorder.english_identifier_digits = True
    assert await router._sanitize_transform(raw, None) == (
        "contact number eight, three, four, zero, two, nine, four, one, four, zero है")


# ── e. one retry when a reply produced no audio ─────────────────────────────

def _capture(router):
    pushed = []

    async def push_frame(frame, direction=None):
        pushed.append(frame)

    async def push_error(error_msg=None, **kwargs):
        pushed.append(("error", error_msg))

    router.push_frame = push_frame
    router.push_error = push_error
    return pushed


async def _speak(router, provider, text, context_id, marker=7):
    async def fake_get_provider(engine, locale):
        return provider

    router._get_provider = fake_get_provider
    router._pending_marker = marker
    async for _ in router.run_tts(text, context_id):
        pass
    return router._generations[context_id]


def _texts(provider):
    return [call[2] for call in provider.calls if call[0] == "text"]


READBACK = "Terminal ID one, nine, six, four, nine, three, nine, letter A. क्या यह सही है?"


@pytest.mark.asyncio
async def test_first_audio_timeout_replays_the_reply_once_then_reports_it_undelivered():
    """cv_8358a2aef66b: the Terminal ID readback timed out without audio."""
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder, first_audio_timeout=0.05)
    pushed = _capture(router)
    provider = FakeProvider()
    state = await _speak(router, provider, READBACK, "ctx")
    state.flushed = state.turn_complete = True

    await router._first_audio_watchdog("ctx")                  # 50 ms without audio
    assert _texts(provider) == [READBACK, READBACK]            # replayed on a fresh stream
    assert ("cancel", "ctx~1") in provider.calls and state.retry_used
    assert not [f for f in pushed if isinstance(f, ReplyTTSCompleteFrame)]

    await asyncio.sleep(0.15)                                  # the retry's own watchdog fires
    assert [e for e in recorder.events if e["kind"] == "tts_retry"] == [{
        "kind": "tts_retry", "provider": "sarvam", "model": "bulbul:v3",
        "category": "timeout", "chars": len(READBACK)}]
    assert _texts(provider) == [READBACK, READBACK]            # never a second retry
    assert ("error", "tts_failure:timeout") in pushed
    done = [f for f in pushed if isinstance(f, ReplyTTSCompleteFrame)]
    assert len(done) == 1 and done[0].marker == 7 and done[0].failed
    for task in router.tasks:
        task.cancel()


@pytest.mark.asyncio
async def test_a_retry_that_delivers_audio_completes_normally():
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder, first_audio_timeout=0.05)
    pushed = _capture(router)
    provider = FakeProvider()
    state = await _speak(router, provider, READBACK, "ctx")
    state.flushed = state.turn_complete = True
    await router._first_audio_watchdog("ctx")
    await router._dispatch_event(KEY, TTSStreamEvent(kind="audio", generation_id=state.active, audio=b"\x01\x02" * 80))
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id=state.active))
    done = [f for f in pushed if isinstance(f, ReplyTTSCompleteFrame)]
    assert len(done) == 1 and not done[0].failed
    assert not [f for f in pushed if isinstance(f, tuple)]
    for task in router.tasks:
        task.cancel()


@pytest.mark.asyncio
async def test_a_reply_that_ends_without_audio_and_without_error_is_retried_too():
    recorder = FakeRecorder()
    router = make_router(pause_ms=200, recorder=recorder)
    pushed = _capture(router)
    provider = FakeProvider()
    state = await _speak(router, provider, READBACK, "ctx")
    state.flushed = state.turn_complete = True
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id=state.active))
    assert _texts(provider) == [READBACK, READBACK] and "ctx" in router._generations
    await router._dispatch_event(KEY, TTSStreamEvent(kind="final", generation_id=state.active))
    done = [f for f in pushed if isinstance(f, ReplyTTSCompleteFrame)]
    assert len(done) == 1 and done[0].failed and "ctx" not in router._generations
    for task in router.tasks:
        task.cancel()


@pytest.mark.asyncio
async def test_no_retry_once_audio_was_delivered_or_for_a_permanent_error():
    for category, audio in (("upstream", True), ("auth", False), ("invalid_input", False)):
        recorder = FakeRecorder()
        router = make_router(pause_ms=200, recorder=recorder)
        _capture(router)
        router._maybe_end_call_fatal = lambda category: _noop()
        provider = FakeProvider()
        state = await _speak(router, provider, READBACK, "ctx")
        if audio:
            await router._dispatch_event(KEY, TTSStreamEvent(kind="audio", generation_id=state.active, audio=b"\x01\x02" * 80))
        assert not await router._try_retry("ctx", ProviderError("sarvam", category, "x"))
        assert _texts(provider) == [READBACK] and not state.retry_used
        for task in router.tasks:
            task.cancel()


async def _noop():
    return None


@pytest.mark.asyncio
async def test_interrupted_reply_reports_the_sentences_it_was_speaking():
    router = make_router(pause_ms=200, recorder=FakeRecorder())
    pushed = _capture(router)
    provider = FakeProvider()
    await _speak(router, provider, "Step one.", "ctx")
    async for _ in router.run_tts("क्या मैं और मदद कर सकता हूँ?", "ctx"):
        pass
    await router.on_audio_context_interrupted("ctx")
    cut = [f for f in pushed if isinstance(f, ReplyTTSInterruptedFrame)]
    assert len(cut) == 1 and cut[0].texts == ["Step one.", "क्या मैं और मदद कर सकता हूँ?"]
    assert len(cut[0].sentences) == 2
    for task in router.tasks:
        task.cancel()


# ── d. unheard reply is repeated, not answered ──────────────────────────────

SMS_STEPS = ("sir paper rolls request करने के लिए आपको एक SMS भेजना होगा। "
             "सर क्या मैं आज आपकी किसी और चीज़ में मदद कर सकता हूँ?")
GOODBYE = "मुझे खुशी है कि मैं आज आपकी मदद कर सका।"
SMS_FLOW = {
    "id": "wf_sms", "version": 1,
    "nodes": [
        {"id": "start", "kind": "start"},
        {"id": "n_bank", "kind": "ask", "config": {
            "variable": "bank_ok", "question": "क्या आपके bank का नाम BOI है? कृपया हाँ या ना में बताइए।",
            "entity": {"dataType": "text", "synonyms": {"yes": ["हाँ", "haan", "yes"], "no": ["नहीं", "no"]}}}},
        {"id": "n_sms", "kind": "intent", "config": {"prompt": SMS_STEPS, "responseMode": "fixed"}},
        {"id": "n_more", "kind": "message", "config": {"text": "जी Sir, बताइए।"}},
        {"id": "n_end", "kind": "end", "config": {"text": GOODBYE}},
    ],
    "edges": [
        {"from": "start", "to": "n_bank"}, {"from": "n_bank", "to": "n_sms"},
        {"from": "n_sms", "to": "n_end", "label": "no/nahi/नहीं/नहीं चाहिए"},
        {"from": "n_sms", "to": "n_more", "label": "yes/haan/हाँ"},
        {"from": "n_more", "to": "n_end"},
    ],
}


async def _sms_call(monkeypatch, *, guard: bool):
    from langgraph.checkpoint.memory import MemorySaver
    import shared.orchestration.workflow_engine as module

    monkeypatch.setattr(module, "load_workflow_definition", lambda *args: SMS_FLOW)
    engine = module.WorkflowEngine()

    async def memory():
        if engine._checkpointer is None:
            engine._checkpointer = MemorySaver()
        return engine._checkpointer

    monkeypatch.setattr(engine, "_get_checkpointer", memory)
    brain = make_workflow_brain(workflow_engine=engine)
    brain._unheard_reply_guard = guard
    brain._active_workflow = "wf_sms"
    await brain._handle_turn("मुझे पेपर रोल चाहिए")       # → bank question
    await brain._handle_turn("हाँ")                         # → SMS steps + "anything else?"
    assert brain._reply_nodes == ["n_sms"]
    return brain


def _cut(brain, played_s):
    """A barge-in ``played_s`` seconds into the reply that is being delivered."""
    brain._bot_speaking = True
    brain._reply_audio_started_at = time.monotonic() - played_s
    brain._on_reply_interrupted()
    brain._bot_speaking = False


def _said(brain):
    return [t.text for t in brain._recorder.turns if getattr(t, "role", "") == "bot"]


def _events(brain, kind):
    return [data for name, data in brain._recorder.events if name == kind]


@pytest.mark.asyncio
async def test_cv_b14a8c89cb55_a_no_over_an_unheard_reply_never_closes_the_call(monkeypatch):
    """17 Nov: "नहीं नहीं आई है।" 1.37 s into the SMS steps ended the call."""
    brain = await _sms_call(monkeypatch, guard=True)
    _cut(brain, 1.37)
    await brain._handle_turn("नहीं नहीं आई है।")
    assert _said(brain)[-1] == SMS_STEPS                    # spoken again, not the goodbye
    assert brain._active_workflow == "wf_sms" and brain._reply_nodes == ["n_sms"]
    event = _events(brain, "unheard_reply_not_answered")[-1]
    # No classifier in this harness: the turn carries no label, as after a
    # classifier timeout. The words themselves ("नहीं …", four words) decide.
    assert (event["nodes"], event["reason"], event["signal"]) == (["n_sms"], "early_cut", None)
    assert event["played_s"] == pytest.approx(1.37, abs=0.05)

    # The caller heard the repeat to the end and now declines: that is an answer.
    brain._reply_heard = True
    await brain._handle_turn("नहीं चाहिए")
    assert _said(brain)[-1] == GOODBYE
    await brain.cleanup()


@pytest.mark.asyncio
async def test_the_repeat_happens_once_so_the_call_cannot_loop(monkeypatch):
    brain = await _sms_call(monkeypatch, guard=True)
    _cut(brain, 0.9)
    await brain._handle_turn("नहीं")
    assert _said(brain)[-1] == SMS_STEPS
    _cut(brain, 0.5)                                        # talks over the repeat as well
    await brain._handle_turn("नहीं")
    assert _said(brain)[-1] == GOODBYE
    assert len(_events(brain, "unheard_reply_not_answered")) == 1
    await brain.cleanup()


@pytest.mark.asyncio
async def test_without_the_tenant_flag_nothing_changes(monkeypatch):
    brain = await _sms_call(monkeypatch, guard=False)
    _cut(brain, 1.37)
    assert brain._unheard_reply is None
    await brain._handle_turn("नहीं नहीं आई है।")
    assert _said(brain)[-1] == GOODBYE and not _events(brain, "unheard_reply_not_answered")
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_caller_who_listened_is_answered_as_before(monkeypatch):
    brain = await _sms_call(monkeypatch, guard=True)
    _cut(brain, 8.25)                                       # cv_a3a2e9c1d463: cut after 8.25 s
    assert brain._unheard_reply is None
    await brain._handle_turn("नहीं चाहिए")
    assert _said(brain)[-1] == GOODBYE
    await brain.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,signal", [
    ("मुझे आईवीआर का तरीका बताओ।", "question"),             # a request is handled as before
    ("2 5 4 4 0 8 3 A for Apple", "refusal"),                # carries an identifier
    ("हाँ, मेरा terminal चालू नहीं हो रहा है उसका क्या करूँ", None),   # long, unlabelled: the caller's own content
])
async def test_only_a_bare_yes_or_no_is_held_back(monkeypatch, text, signal):
    brain = await _sms_call(monkeypatch, guard=True)
    _cut(brain, 1.0)
    brain._turn_counter += 1
    assert brain._take_unheard_reply(text, signal) is None and brain._unheard_reply is None
    await brain.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,signal", [
    ("नहीं नहीं आई है।", "refusal"), ("नहीं नहीं आई है।", None),      # cv_b14a8c89cb55, with and without a label
    ("ठीक है।", "affirm"),                                   # cv_cc27070dbea4 / cv_4cecac598f1c
    ("हाँ जी।", "affirm"), ("हाँ, बिल्कुल। हाँ।", "affirm"),   # cv_f3800137eb10 / cv_26dac9f59e0c
    ("हाँ, हो गया।", "affirm"),                              # cv_c50d25900364
])
async def test_every_reviewed_yes_or_no_over_an_early_cut_is_held_back(monkeypatch, text, signal):
    brain = await _sms_call(monkeypatch, guard=True)
    _cut(brain, 1.0)
    brain._turn_counter += 1
    assert brain._take_unheard_reply(text, signal) is not None
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_no_that_names_an_option_of_the_waiting_step_is_the_callers_answer(monkeypatch):
    brain = await _sms_call(monkeypatch, guard=True)
    brain._pending_hub_edges = [{"tokens": ["no", "नहीं"], "signals": ["refusal"]},
                                {"tokens": ["ivr", "आईवीआर"], "signals": []}]
    _cut(brain, 1.0)
    brain._turn_counter += 1
    assert brain._take_unheard_reply("नहीं, IVR बताओ", "refusal") is None
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_later_turn_never_repeats_a_stale_reply(monkeypatch):
    brain = await _sms_call(monkeypatch, guard=True)
    _cut(brain, 1.0)
    brain._turn_counter += 2                                 # some other turn came in between
    assert brain._take_unheard_reply("नहीं", "refusal") is None
    await brain.cleanup()


IVR = ["Step one. पहले call कीजिए।", "Step two. Language select कीजिए।", "Step three. Terminal I-D enter कीजिए।",
       "Step four. Option चुनिए।", "सर क्या मैं आज आपकी किसी और चीज़ में मदद कर सकता हूँ?"]


def _tail_cut(brain, played_s, texts, seconds):
    brain._reply_nodes, brain._reply_text = ["n_ivr"], " ".join(texts)
    brain._reply_marker = 9
    _cut(brain, played_s)
    brain._on_reply_tts_interrupted(ReplyTTSInterruptedFrame(
        marker=9, sentences=[(len(t), s) for t, s in zip(texts, seconds)], texts=list(texts)))


@pytest.mark.asyncio
async def test_cv_cc27070dbea4_backchannel_before_the_closing_question_is_not_its_answer():
    """"हांजी। हांजी।" 21.3 s into the IVR steps was taken as yes to a question not yet asked."""
    brain = make_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "wf"
    _tail_cut(brain, 21.32, IVR, [5.0, 6.0, 7.0, 6.5, 4.0])     # the cut falls inside step four
    record = brain._unheard_reply
    assert record["reason"] == "question_not_started" and record["text"] == " ".join(IVR[3:])
    brain._turn_counter += 1
    assert brain._take_unheard_reply("हांजी। हांजी।", "affirm") is record
    await brain._repeat_unheard_reply(record, "affirm")
    assert [f.text for f in brain._pushed if isinstance(f, TextFrame)] == [" ".join(IVR[3:])]
    await brain.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("played_s,texts", [
    (25.0, IVR),                                           # the closing question had started
    (21.32, IVR[:4] + ["धन्यवाद।"]),                        # the reply does not end in a question
])
async def test_a_reply_cut_inside_or_without_a_closing_question_is_not_repeated(played_s, texts):
    brain = make_brain()
    brain._unheard_reply_guard = True
    _tail_cut(brain, played_s, texts, [5.0, 6.0, 7.0, 6.5, 4.0])
    assert brain._unheard_reply is None
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_reply_without_audio_is_never_treated_as_heard_even_without_the_flag():
    """e: the retry failed too — the next yes/no repeats the reply (every bot)."""
    brain = make_brain()
    assert brain._unheard_reply_guard is False
    brain._active_workflow = "wf"
    brain._reply_nodes, brain._reply_text, brain._reply_marker = ["n_tid__confirm"], READBACK, 4
    brain._on_reply_tts_complete(ReplyTTSCompleteFrame(marker=4, failed=True))
    assert brain._reply_nodes == [] and "n_tid__confirm" not in brain._heard_nodes
    assert _events_stub(brain, "tts_reply_not_delivered") == [{"nodes": ["n_tid__confirm"]}]
    brain._turn_counter += 1
    record = brain._take_unheard_reply("हाँ", "affirm")
    assert record["reason"] == "tts_failed" and record["text"] == READBACK
    await brain.cleanup()


def _events_stub(brain, kind):
    events = brain._recorder.events_of(kind) if hasattr(brain._recorder, "events_of") else [
        data for name, data in brain._recorder.events if name == kind]
    return [{k: v for k, v in dict(e).items() if k not in ("kind", "at")} for e in events]


@pytest.mark.asyncio
async def test_the_synthesis_report_may_arrive_before_the_cut_itself_is_handled():
    """DEMO 2026-11-25 (problem 5): "haan ji" in the middle of the purpose line
    answered the question that line had not asked yet. The interruption stops
    the synthesis first, so its per-sentence report can reach the brain before
    the brain has handled the cut; the report was then discarded and the reply
    counted as heard."""
    brain = make_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "wf"
    brain._reply_nodes, brain._reply_text = ["n_ivr"], " ".join(IVR)
    brain._reply_marker = 9
    seconds = [5.0, 6.0, 7.0, 6.5, 4.0]
    # 1. the report arrives first: nothing to measure it against yet
    brain._on_reply_tts_interrupted(ReplyTTSInterruptedFrame(
        marker=9, sentences=[(len(t), s) for t, s in zip(IVR, seconds)], texts=list(IVR)))
    assert brain._unheard_reply is None and brain._reply_cut_report is not None
    # 2. the cut is handled: the kept report is applied
    _cut(brain, 21.32)
    record = brain._unheard_reply
    assert record is not None and record["reason"] == "question_not_started"
    assert record["text"] == " ".join(IVR[3:])
    assert brain._reply_cut_report is None
    measured = brain._recorder.events_of("reply_cut_measured")[-1]
    assert measured["sentences"] == 5 and measured["heard"] == 3 and measured["ends_with_question"] is True
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_report_of_an_earlier_reply_is_never_applied_to_the_next_one():
    brain = make_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "wf"
    brain._on_reply_tts_interrupted(ReplyTTSInterruptedFrame(
        marker=8, sentences=[(len(t), 5.0) for t in IVR], texts=list(IVR)))
    brain._reply_nodes, brain._reply_text = ["n_ivr"], " ".join(IVR)
    brain._reply_marker = 9                                  # a newer reply
    _cut(brain, 21.32)
    assert brain._unheard_reply is None and brain._reply_cut_report is None
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_reply_cut_while_playing_after_its_synthesis_finished_is_measured_too():
    """LOCAL voice run 2026-11-26: synthesis runs ahead of playback, so the
    router had already reported the purpose line complete when "haan ji" cut
    it 6 s in; no interruption report followed and the question that line
    had not asked yet was taken as answered."""
    brain = make_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "wf"
    brain._reply_nodes, brain._reply_text = ["n_ivr"], " ".join(IVR)
    brain._reply_marker = 9
    brain._bot_speaking = True
    brain._on_reply_tts_complete(ReplyTTSCompleteFrame(
        marker=9, sentences=[(len(t), s) for t, s in zip(IVR, [5.0, 6.0, 7.0, 6.5, 4.0])], texts=list(IVR)))
    assert brain._reply_tts_complete and brain._unheard_reply is None
    _cut(brain, 21.32)                                       # inside step four, the question not started
    record = brain._unheard_reply
    assert record is not None and record["reason"] == "question_not_started"
    assert record["text"] == " ".join(IVR[3:])
    await brain.cleanup()


@pytest.mark.asyncio
async def test_a_complete_report_without_sentences_changes_nothing():
    brain = make_brain()
    brain._unheard_reply_guard = True
    brain._active_workflow = "wf"
    brain._reply_nodes, brain._reply_text = ["n_ivr"], " ".join(IVR)
    brain._reply_marker = 9
    brain._bot_speaking = True
    brain._on_reply_tts_complete(ReplyTTSCompleteFrame(marker=9))
    _cut(brain, 21.32)
    assert brain._unheard_reply is None and brain._reply_cut_report is None
    await brain.cleanup()
