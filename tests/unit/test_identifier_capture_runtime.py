"""Real-time spoken-identifier capture: the cv_ed7879c6a848 regression suite.

Primary regression: realtime STT produced "Seven Zero Zero One" + "Zero" +
"Zero Two" for a spoken order id; prefix-only cumulative merging deleted the
genuinely repeated zero (700102 instead of 7001002), per-fragment turns caused
TTS churn/barge-in, the Goal Engine marked digit fragments out_of_scope, and a
Gujarati-labelled "સાત" was rejected as unsupported_script.

Covers (numbering follows the task's testing requirements):
 1  the exact segment sequence normalizes to 7001002
 2  proven cumulative finals still replace (Flux turn identity / same open
    audio segment) rather than duplicate
 3  independent repeated-prefix finals append
 4  Hindi / English / Hinglish / Devanagari-transliterated digit forms
 5  context-safe rescue of Gujarati "સાત" → 7
 6  unsupported non-numeric Gujarati speech stays rejected
 7  active-workflow digits cannot be redirected out_of_scope
 8  no Goal Engine request for deterministic identifier fragments
 9  one-digit-at-a-time dictation with natural pauses
10  no TTS/turn dispatch per partial fragment
11  an exactly-valid candidate dispatches immediately
12  "What did you note?" answers from the pending buffer (masked for phones)
13  explicit restart clears the old partial
14  overflowing/impossible buffers recover instead of wedging
15  the lookup API executes exactly once after a valid id
16  browser 16 kHz and telephony 8 kHz audio paths
17  barge-in deduplication (one physical start → one barge_in)
18  replayed finals are still deduplicated (reconnect re-delivery intact)
19  buffers are session-isolated and cleared at cleanup
20  non-identifier conversations keep their behavior (full suite covers this;
    spot checks here)
21  recording duration stays wall-clock aligned under faster-than-realtime
    bot audio
22  no remote lookups per audio frame; batch recovery runs at most once
"""

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

import shared.orchestration.workflow_engine as wfe
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.entity_extractor import identifier_length_bounds
from shared.orchestration.spoken_numbers import (
    pure_digit_payload,
    spoken_digit_sequence,
)
from voice_runtime.audio_gate import CallerAudioGate
from voice_runtime.brain import ConversationBrain
from voice_runtime.identifier_capture import (
    IdentifierCapture,
    resolve_pause_window,
)
from voice_runtime.recording import AlignedStereoRecorder
from voice_runtime.transcript_gate import SegmentQuality, assess_transcript

# Real (small) timers: fragment pauses sit between GRACE and WINDOW so the
# tolerant identifier window is observably different from normal endpoints.
GRACE = 0.06
WINDOW = 0.4

ORDER_ENTITY = {
    "name": "order_ref", "dataType": "text",
    "regexPattern": r"(?<![0-9])([0-9]{10}|[0-9]{7})(?![0-9])",
}


# ── shared stubs ─────────────────────────────────────────────────────────────


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0,
                      "llm_input_tokens": 0}
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

    def events_of(self, kind):
        return [data for name, data in self.events if name == kind]


class _GateStub:
    """Audio-gate stand-in with a controllable segment counter + retention."""

    def __init__(self, snr_db=25.0):
        self.segments_started = 0
        self.retained: tuple[bytes, int] | None = None
        self.retention_enabled = False
        self._snapshot = {"snr_db": snr_db, "during_bot_audio": False}

    def speech_snapshot(self):
        return self._snapshot

    def stats(self):
        return {"opens": self.segments_started, "suppressed_ms": 0.0}

    def begin_backchannel_window(self):
        pass

    def end_backchannel_window(self):
        pass

    def enable_utterance_retention(self, max_seconds: float = 30.0):
        self.retention_enabled = True

    def disable_utterance_retention(self):
        self.retention_enabled = False
        self.retained = None

    def clear_retained_audio(self):
        self.retained = None

    def take_retained_audio(self):
        retained, self.retained = self.retained, None
        return retained


def make_brain(*, gate=None, workflow_engine=None, batch_transcriber=None,
               stt_settings=None):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam", "settings": dict(stt_settings or {})},
        system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        workflow_engine=workflow_engine,
        finalize_grace=GRACE, finalize_settle=0.02,
        complete_endpoint=GRACE, short_reply_endpoint=GRACE,
        audio_gate=gate, batch_transcriber=batch_transcriber,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel_task
    return brain


def stub_turn_handler(brain):
    handled = []

    async def _handle(text):
        handled.append(text)

    brain._handle_turn = _handle
    return handled


_seq = iter(range(1, 100_000))


def transcript(text, *, request_id="conn-1", turn_index=None, language="hi-IN",
               **quality):
    data = {"is_final": True, "request_id": request_id, **quality}
    if turn_index is not None:
        data["turn_index"] = turn_index
    data.setdefault("metrics", {
        # Plausible speaking rate so the gate's noise rules stay quiet.
        "audio_duration": max(0.5, 0.35 * len(text.split())) + next(_seq) / 1000,
        "processing_latency": 0.05 + next(_seq) / 10_000,
    })
    frame = TranscriptionFrame(
        text=text, user_id="caller", timestamp=f"t-{next(_seq)}",
        language=language,
        result={"type": "data", "data": data, "provider": "sarvam"},
    )
    frame.finalized = True
    return frame


def capture_for(brain, entity=ORDER_ENTITY, *, window=WINDOW, workflow="wf-1",
                held=""):
    brain._identifier_capture = IdentifierCapture(
        workflow=workflow, node="n_ask", variable="order_ref",
        entity=dict(entity), held_digits=held, pause_window=window,
    )
    brain._active_workflow = workflow
    return brain._identifier_capture


async def frame_in(brain, frame):
    await brain.process_frame(frame, FrameDirection.DOWNSTREAM)


# ── 1/3: the regression — repeated digits must append, not merge ────────────


@pytest.mark.asyncio
async def test_seven_zero_zero_one_plus_zero_plus_zero_two_is_7001002():
    gate = _GateStub()
    brain = make_brain(gate=gate)
    handled = stub_turn_handler(brain)
    capture_for(brain, window=0.15)

    for fragment in ("Seven Zero Zero One", "Zero", "Zero Two"):
        gate.segments_started += 1  # each fragment is a new speech burst
        await frame_in(brain, transcript(fragment))
    # "Zero Two" completes 7001002 → immediate dispatch.
    await asyncio.sleep(0.05)
    assert handled == ["Seven Zero Zero One Zero Zero Two"]
    assert spoken_digit_sequence(handled[0]) == "7001002"
    assert "stt_cumulative_final_merged" not in brain._recorder.event_kinds()


@pytest.mark.asyncio
async def test_repeated_prefix_across_bursts_appends_even_without_vad_turns():
    # The live failure shape: fragments arrived while the bot was speaking the
    # ack (below the barge-in word gate → NO UserStartedSpeaking frames). The
    # audio gate's segment counter is the provenance that keeps them apart.
    gate = _GateStub()
    brain = make_brain(gate=gate)
    stub_turn_handler(brain)
    capture_for(brain)

    gate.segments_started = 1
    await frame_in(brain, transcript("Zero"))
    gate.segments_started = 2
    await frame_in(brain, transcript("Zero Two"))
    assert brain._pending_segments == ["Zero", "Zero Two"]
    await brain._cancel_finalize()


# ── 2: proven cumulative delivery still replaces ─────────────────────────────


@pytest.mark.asyncio
async def test_flux_same_turn_identity_replaces_buffer():
    brain = make_brain(gate=_GateStub())
    stub_turn_handler(brain)
    await frame_in(brain, transcript("मैं कल", turn_index=4))
    await frame_in(brain, transcript("मैं कल कर दूंगा", turn_index=4))
    assert brain._pending_segments == ["मैं कल कर दूंगा"]
    assert "stt_cumulative_final_merged" in brain._recorder.event_kinds()
    await brain._cancel_finalize()


@pytest.mark.asyncio
async def test_same_open_segment_reemission_replaces_buffer():
    # No new speech burst between the finals (same VAD serial, same gate
    # segment) and within the re-emission window → proven cumulative.
    gate = _GateStub()
    gate.segments_started = 1
    brain = make_brain(gate=gate)
    stub_turn_handler(brain)
    await frame_in(brain, transcript("मैं कल"))
    await frame_in(brain, transcript("मैं कल कर दूंगा"))
    assert brain._pending_segments == ["मैं कल कर दूंगा"]
    await brain._cancel_finalize()


@pytest.mark.asyncio
async def test_new_burst_between_prefix_finals_appends():
    gate = _GateStub()
    gate.segments_started = 1
    brain = make_brain(gate=gate)
    stub_turn_handler(brain)
    await frame_in(brain, transcript("हाँ हाँ"))
    gate.segments_started = 2  # the caller spoke again
    await frame_in(brain, transcript("हाँ हाँ ठीक है"))
    assert brain._pending_segments == ["हाँ हाँ", "हाँ हाँ ठीक है"]
    await brain._cancel_finalize()


# ── 18: replayed finals stay deduplicated ────────────────────────────────────


@pytest.mark.asyncio
async def test_replayed_final_is_still_dropped():
    brain = make_brain(gate=_GateStub())
    stub_turn_handler(brain)
    frame = transcript("Seven Zero Zero One")
    await frame_in(brain, frame)
    replay = TranscriptionFrame(
        text=frame.text, user_id="caller", timestamp="t-replay",
        language=frame.language, result=frame.result,
    )
    await frame_in(brain, replay)
    assert brain._pending_segments == ["Seven Zero Zero One"]
    assert "stt_duplicate_final_dropped" in brain._recorder.event_kinds()
    await brain._cancel_finalize()


# ── 4: multilingual digit forms ─────────────────────────────────────────────


@pytest.mark.parametrize("utterance", [
    "seven zero zero one zero zero two",                  # English
    "सात शून्य शून्य एक शून्य शून्य दो",                    # Hindi
    "saat shunya shunya ek shunya shunya do",              # Hinglish romanized
    "सेवेन ज़ीरो ज़ीरो वन ज़ीरो ज़ीरो टू",                  # Devanagari-transliterated English
    "मेरा ऑर्डर आई डी है सेवेन ज़ीरो ज़ीरो वन ज़ीरो ज़ीरो टू",  # live batch transcript
])
def test_multilingual_digit_forms_normalize(utterance):
    assert spoken_digit_sequence(utterance) == "7001002"


def test_identifier_capture_keeps_digits_after_devanagari_danda():
    capture = IdentifierCapture(
        workflow="wf-1", node="n-ask", variable="order_ref",
        entity=dict(ORDER_ENTITY),
    )
    raw_final = "हाँ, मेरा ऑर्डर आई डी है 7001। 0 0 1"
    assert capture.candidate(raw_final) == "7001001"
    assert capture.matches("7001001")


# ── 5/6: unsupported-script digit rescue, strictly scoped ────────────────────


def test_gujarati_saat_rescued_only_in_numeric_context():
    quality = SegmentQuality(language="gu-IN", language_probability=0.95,
                             confidence=0.9, snr_db=43.3, audio_seconds=0.5)
    rejected = assess_transcript("સાત", quality)
    assert not rejected.accepted and rejected.reason == "unsupported_script"

    rescued = assess_transcript("સાત", quality, numeric_context=True)
    assert rescued.accepted and rescued.reason == "digit_payload"
    assert rescued.normalized_text == "7"


def test_gujarati_sentence_stays_rejected_even_in_numeric_context():
    quality = SegmentQuality(language="gu-IN", language_probability=0.95,
                             confidence=0.9, snr_db=40.0, audio_seconds=1.4)
    verdict = assess_transcript(
        "મને ખબર નથી શું કહેવું", quality, numeric_context=True
    )
    assert not verdict.accepted
    assert verdict.reason == "unsupported_script"


def test_low_snr_digit_is_not_rescued():
    quality = SegmentQuality(language="gu-IN", language_probability=0.95,
                             confidence=0.9, snr_db=4.0, audio_seconds=0.5)
    verdict = assess_transcript("સાત", quality, numeric_context=True)
    assert not verdict.accepted


def test_pure_digit_payload_is_strict():
    assert pure_digit_payload("સાત") == "7"
    assert pure_digit_payload("double nine") == "99"
    assert pure_digit_payload("સાત please") == ""
    assert pure_digit_payload("okay") == ""


@pytest.mark.asyncio
async def test_rescued_digit_does_not_steer_conversation_language():
    gate = _GateStub()
    brain = make_brain(gate=gate)
    stub_turn_handler(brain)
    capture_for(brain)
    gate.segments_started = 1
    await frame_in(brain, transcript(
        "સાત", language="gu-IN",
        language_probability=0.95, confidence=0.9,
    ))
    assert brain._pending_segments == ["7"]
    assert brain._pending_language is None
    assert "unsupported_script_digit_rescued" in brain._recorder.event_kinds()
    await brain._cancel_finalize()


# ── 9/10/11: pacing — hold fragments, dispatch exact candidates now ─────────


@pytest.mark.asyncio
async def test_one_digit_at_a_time_with_pauses_is_one_turn():
    gate = _GateStub()
    brain = make_brain(gate=gate)
    handled = stub_turn_handler(brain)
    capture_for(brain, window=WINDOW)

    for fragment in ("seven", "zero", "zero", "one", "zero", "zero"):
        gate.segments_started += 1
        await frame_in(brain, UserStartedSpeakingFrame())
        await frame_in(brain, UserStoppedSpeakingFrame())
        await frame_in(brain, transcript(fragment))
        await asyncio.sleep(0.12)  # natural inter-digit pause < WINDOW
        assert handled == [], "a partial fragment must not dispatch a turn"
    gate.segments_started += 1
    await frame_in(brain, UserStartedSpeakingFrame())
    await frame_in(brain, UserStoppedSpeakingFrame())
    await frame_in(brain, transcript("two"))
    await asyncio.sleep(0.1)  # exact match → immediate, well inside WINDOW
    assert handled == ["seven zero zero one zero zero two"]
    partials = brain._recorder.events_of("identifier_digits_partial")
    assert partials and all(p["prompt_suppressed"] for p in partials)
    # Digit VALUES are never logged — only counts.
    assert all("digits" not in p or isinstance(p["digits"], int) for p in partials)


@pytest.mark.asyncio
async def test_incomplete_fragment_dispatches_after_identifier_window():
    gate = _GateStub()
    brain = make_brain(gate=gate)
    handled = stub_turn_handler(brain)
    capture_for(brain, window=0.2)
    gate.segments_started = 1
    await frame_in(brain, transcript("seven zero zero one"))
    await asyncio.sleep(GRACE + 0.05)  # normal endpoints would have fired
    assert handled == []
    await asyncio.sleep(0.25)  # identifier window elapses → dispatch
    assert handled == ["seven zero zero one"]


@pytest.mark.asyncio
async def test_ordinary_conversation_keeps_normal_endpoints():
    # Identifier mode active, but the caller says something non-numeric: the
    # tolerant window must NOT apply (no global slow-down).
    brain = make_brain(gate=_GateStub())
    handled = stub_turn_handler(brain)
    capture_for(brain, window=1.0)
    await frame_in(brain, transcript("मुझे एजेंट से बात करनी है।"))
    await asyncio.sleep(GRACE + 0.08)
    assert handled == ["मुझे एजेंट से बात करनी है।"]


# ── 7/8: deterministic workflow outranks Goal Engine ─────────────────────────


def consumed_result(reply="ok", *, done=False, awaiting_identifier=None,
                    slots=None):
    return {
        "reply": reply, "done": done,
        "status": "done" if done else "collecting",
        "source": "definition", "workflowId": "wf_1", "trace": ["n_ask"],
        "slots": slots or {}, "handoffQueue": None, "offScript": False,
        "nodePrompt": None, "signal": None,
        "awaitingIdentifier": awaiting_identifier,
    }


class _WorkflowStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def handle_turn_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_active_workflow_digits_skip_goal_engine_and_scope():
    workflows = _WorkflowStub(consumed_result(
        awaiting_identifier={"node": "n_ask", "variable": "order_ref",
                             "entity": ORDER_ENTITY, "held_digits": "70010"},
    ))
    brain = make_brain(gate=_GateStub(), workflow_engine=workflows)
    capture_for(brain)
    decisions = []

    async def _take_decision(text):
        decisions.append(text)
        return None

    brain._take_decision = _take_decision
    redirects = []

    async def _redirect(*args, **kwargs):
        redirects.append(args)

    brain._redirect_off_goal = _redirect

    async def _say(text, **kwargs):
        return None

    brain._say = _say
    await brain._handle_turn("zero two")

    assert decisions == [], "Goal Engine must not run for identifier turns"
    assert redirects == [], "digits must never become an out_of_scope redirect"
    assert len(workflows.calls) == 1
    assert workflows.calls[0]["user_text"] == "zero two"
    fast = brain._recorder.events_of("deterministic_fast_path")
    assert fast and fast[0]["rule"] == "identifier_capture"


@pytest.mark.asyncio
async def test_decision_prefetch_skipped_for_identifier_fragments():
    brain = make_brain(gate=_GateStub())
    stub_turn_handler(brain)
    capture_for(brain)
    brain._pending_segments = ["seven zero zero one"]

    class _EngineStub:
        enabled = True
        last_fallback_reason = None

    brain._goal_engine = _EngineStub()
    brain._start_decision_prefetch()
    assert brain._decision_prefetch is None


# ── capture lifecycle: sync from workflow results, cleanup, isolation ───────


@pytest.mark.asyncio
async def test_capture_starts_from_awaiting_identifier_and_ends_on_fill():
    workflows = _WorkflowStub(consumed_result(
        awaiting_identifier={"node": "n_ask", "variable": "order_ref",
                             "entity": ORDER_ENTITY, "held_digits": ""},
    ))
    gate = _GateStub()
    brain = make_brain(gate=gate, workflow_engine=workflows)

    async def _say(text, **kwargs):
        return None

    brain._say = _say
    from shared.orchestration.router import RouteDecision, RouteKind
    decision = RouteDecision(kind=RouteKind.WORKFLOW, action="wf-1")
    await brain._handle_workflow(decision, "order status", 0.0)
    assert brain._identifier_capture is not None
    assert brain._identifier_capture.variable == "order_ref"
    assert gate.retention_enabled, "audio retention arms with the capture"
    assert "identifier_capture_started" in brain._recorder.event_kinds()

    # The identifier fills → the engine stops reporting awaitingIdentifier.
    workflows.result = consumed_result(slots={"order_ref": "7001002"})
    await brain._handle_workflow(decision, "seven zero zero one zero zero two", 0.0)
    assert brain._identifier_capture is None
    assert not gate.retention_enabled, "retention is disarmed with the capture"
    assert "identifier_validated" in brain._recorder.event_kinds()


@pytest.mark.asyncio
async def test_cleanup_clears_capture_buffers_and_retention():
    gate = _GateStub()
    brain = make_brain(gate=gate)
    stub_turn_handler(brain)
    capture_for(brain)
    gate.retention_enabled = True
    gate.retained = (b"\x01\x02", 16000)
    await frame_in(brain, transcript("seven zero"))
    await brain.cleanup()
    assert brain._identifier_capture is None
    assert brain._pending_segments == []
    assert brain._last_buffered_final is None
    assert not gate.retention_enabled and gate.retained is None


def test_capture_state_is_per_session_instance():
    brain_a = make_brain(gate=_GateStub())
    brain_b = make_brain(gate=_GateStub())
    capture_for(brain_a)
    assert brain_b._identifier_capture is None
    assert brain_a._identifier_capture is not None


# ── 22: batch recovery — bounded, once, never on the happy path ─────────────


class _BatchStub:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def __call__(self, pcm, rate, language):
        self.calls.append((len(pcm), rate, language))
        return self.text


@pytest.mark.asyncio
async def test_batch_recovery_rescues_invalid_streaming_identifier():
    gate = _GateStub()
    gate.retained = (b"\x00" * 32000, 16000)
    batch = _BatchStub("सेवेन ज़ीरो ज़ीरो वन ज़ीरो ज़ीरो टू")
    brain = make_brain(gate=gate, batch_transcriber=batch)
    handled = stub_turn_handler(brain)
    capture_for(brain, window=0.1)
    gate.segments_started = 1
    await frame_in(brain, transcript("seven zero zero one"))  # STT lost the tail
    await asyncio.sleep(0.3)
    assert handled == ["7001002"], "the recovered digits replace the turn text"
    assert len(batch.calls) == 1
    kinds = brain._recorder.event_kinds()
    assert "identifier_batch_recovery_attempted" in kinds
    assert "identifier_batch_recovery_succeeded" in kinds


@pytest.mark.asyncio
async def test_no_batch_call_when_streaming_identifier_is_already_valid():
    gate = _GateStub()
    gate.retained = (b"\x00" * 32000, 16000)
    batch = _BatchStub("7001002")
    brain = make_brain(gate=gate, batch_transcriber=batch)
    handled = stub_turn_handler(brain)
    capture_for(brain, window=0.1)
    gate.segments_started = 1
    await frame_in(brain, transcript("seven zero zero one zero zero two"))
    await asyncio.sleep(0.15)
    assert handled == ["seven zero zero one zero zero two"]
    assert batch.calls == [], "a valid streaming id must not pay an extra API call"


@pytest.mark.asyncio
async def test_batch_recovery_runs_at_most_once_and_degrades_safely():
    gate = _GateStub()
    gate.retained = (b"\x00" * 8000, 8000)  # telephony-rate audio

    calls = []

    async def _failing(pcm, rate, language):
        calls.append(rate)
        raise RuntimeError("provider down")

    brain = make_brain(gate=gate, batch_transcriber=_failing)
    handled = stub_turn_handler(brain)
    capture_for(brain, window=0.1)
    gate.segments_started = 1
    await frame_in(brain, transcript("seven zero zero one"))
    await asyncio.sleep(0.3)
    assert handled == ["seven zero zero one"], "failure degrades to streaming text"
    assert calls == [8000]
    assert "identifier_batch_recovery_failed" in brain._recorder.event_kinds()

    # A later invalid fragment must NOT retry the batch call.
    gate.retained = (b"\x00" * 8000, 8000)
    gate.segments_started = 2
    await frame_in(brain, transcript("nine nine"))
    await asyncio.sleep(0.3)
    assert len(calls) == 1


# ── 17: barge-in deduplication ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_physical_speech_start_records_one_barge_in():
    brain = make_brain(gate=_GateStub())
    stub_turn_handler(brain)
    await frame_in(brain, BotStartedSpeakingFrame())
    brain._reply_audio_started = True
    # pipecat delivers both frames for the same physical start.
    await frame_in(brain, UserStartedSpeakingFrame())
    await frame_in(brain, InterruptionFrame())
    assert brain._recorder.event_kinds().count("barge_in") == 1
    assert "barge_in_duplicate_suppressed" in brain._recorder.event_kinds()

    # A genuine LATER interruption still registers.
    await frame_in(brain, UserStoppedSpeakingFrame())
    await brain._cancel_finalize()
    await frame_in(brain, BotStartedSpeakingFrame())
    await frame_in(brain, UserStartedSpeakingFrame())
    assert brain._recorder.event_kinds().count("barge_in") == 2
    await brain._cancel_finalize()


# ── engine: readback, restart, overflow, api-once, schema surfacing ─────────


ORDER_FLOW = {
    "id": "wf_order", "version": 1, "name": "Order support journey",
    "nodes": [
        {"id": "n1", "kind": "start", "label": "Call starts"},
        {"id": "n2", "kind": "ask", "label": "Ask order id",
         "config": {"question": "Please share your order ID.",
                    "variable": "order_ref", "entityType": "text",
                    "pattern": r"(?<![0-9])([0-9]{10}|[0-9]{7})(?![0-9])"}},
        {"id": "n3", "kind": "api", "label": "Order Lookup",
         "config": {"connection": "order_lookup"}},
        {"id": "n4", "kind": "end", "label": "End",
         "config": {"text": "Verified, thank you!"}},
    ],
    "edges": [
        {"id": "e1", "from": "n1", "to": "n2"},
        {"id": "e2", "from": "n2", "to": "n3"},
        {"id": "e3", "from": "n3", "to": "n4", "label": "success"},
        {"id": "e4", "from": "n2", "to": "n4", "label": "fallback"},
    ],
}

PHONE_FLOW = {
    "id": "wf_phone", "version": 1, "name": "Phone capture journey",
    "nodes": [
        {"id": "p1", "kind": "start", "label": "Call starts"},
        {"id": "p2", "kind": "ask", "label": "Ask phone",
         "config": {"question": "Your registered mobile number?",
                    "variable": "phone", "entityType": "phone"}},
        {"id": "p3", "kind": "end", "label": "End", "config": {"text": "Done"}},
    ],
    "edges": [
        {"id": "pe1", "from": "p1", "to": "p2"},
        {"id": "pe2", "from": "p2", "to": "p3"},
    ],
}

_FLOWS = {"order_support_journey": ORDER_FLOW, "phone_capture_journey": PHONE_FLOW}


class _ExecutorStub:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)

        class _R:
            ok = True
            status = 200
            mocked = False
            mapped = {"verified": True}
            spoken_error = ""

        return _R()


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()

    async def _mem_checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem_checkpointer)
    monkeypatch.setattr(
        wfe, "load_workflow_definition",
        lambda tenant_id, bot_id, name: _FLOWS.get(name),
    )
    return eng


@pytest.fixture()
def executor(monkeypatch):
    stub = _ExecutorStub()
    import shared.orchestration.tool_executor as tool_executor

    monkeypatch.setattr(tool_executor, "get_tool_executor", lambda: stub)
    return stub


async def _turn(engine, text, session, workflow="order_support_journey"):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="t", bot_id="b",
        workflow_name=workflow, user_text=text,
    )


@pytest.mark.asyncio
async def test_engine_reports_awaiting_identifier_schema(engine, executor):
    result = await _turn(engine, "hello", "s-schema")
    awaiting = result["awaitingIdentifier"]
    assert awaiting is not None
    assert awaiting["variable"] == "order_ref"
    assert awaiting["held_digits"] == ""
    assert identifier_length_bounds(awaiting["entity"]) == (7, 10)

    partial = await _turn(engine, "seven zero zero one", "s-schema")
    assert partial["awaitingIdentifier"]["held_digits"] == "7001"


@pytest.mark.asyncio
async def test_readback_answers_from_pending_buffer(engine, executor):
    await _turn(engine, "hello", "s-read")
    await _turn(engine, "seven zero zero", "s-read")
    result = await _turn(engine, "what did you note?", "s-read")
    assert "3 digits" in result["reply"]
    assert "7 0 0" in result["reply"]
    # The buffer survives the question and the id still completes.
    finished = await _turn(engine, "one zero zero two", "s-read")
    assert finished["slots"]["order_ref"] == "7001002"


@pytest.mark.asyncio
async def test_readback_with_empty_buffer_never_claims_nothing_heard(
    engine, executor,
):
    await _turn(engine, "hello", "s-read-empty")
    result = await _turn(engine, "क्या नोट किया आपने?", "s-read-empty")
    assert "अंक नोट नहीं" in result["reply"] or "haven't noted" in result["reply"]
    assert not result["done"]


@pytest.mark.asyncio
async def test_phone_readback_is_masked(engine, executor):
    await _turn(engine, "hello", "s-mask", workflow="phone_capture_journey")
    await _turn(engine, "nine eight seven six five", "s-mask",
                workflow="phone_capture_journey")
    result = await _turn(engine, "what did you note?", "s-mask",
                         workflow="phone_capture_journey")
    assert "98765" not in result["reply"].replace(" ", "")
    assert "5 digits" in result["reply"] or "5 अंक" in result["reply"]
    assert "6 5" in result["reply"]  # ending only


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [
    "start again", "dobara bolta hoon", "phir se", "that was wrong",
    "clear the number", "फिर से बताता हूँ", "क्या नोट किया? गलत है, दोबारा",
])
async def test_explicit_restart_clears_the_partial(engine, executor, restart):
    session = f"s-restart-{abs(hash(restart)) % 10_000}"
    await _turn(engine, "hello", session)
    await _turn(engine, "nine nine nine", session)
    result = await _turn(engine, restart, session)
    assert not result["done"]
    # The old partial is gone: a fresh full id resolves cleanly.
    finished = await _turn(engine, "seven zero zero one zero zero two", session)
    assert finished["slots"]["order_ref"] == "7001002"


@pytest.mark.asyncio
async def test_overflowing_buffer_recovers_instead_of_wedging(engine, executor):
    await _turn(engine, "hello", "s-over")
    await _turn(engine, "one two three four five six", "s-over")       # 6 held
    await _turn(engine, "seven eight nine one two three", "s-over")    # 12 > 10
    over = await _turn(engine, "one one one one one", "s-over")        # 17 > 10 → overflow
    assert "digits" in over["reply"] or "अंक" in over["reply"]
    assert not over["done"]
    # The impossible buffer was dropped; a clean id still resolves.
    finished = await _turn(engine, "start again", "s-over")
    assert not finished["done"]
    finished = await _turn(engine, "9876501003", "s-over")
    assert finished["slots"]["order_ref"] == "9876501003"


@pytest.mark.asyncio
async def test_lookup_api_executes_exactly_once_for_a_valid_id(
    engine, executor,
):
    await _turn(engine, "hello", "s-api")
    result = await _turn(engine, "seven zero zero one zero zero two", "s-api")
    assert result["slots"]["order_ref"] == "7001002"
    assert result["done"]
    assert len(executor.calls) == 1
    assert executor.calls[0]["args"]["order_ref"] == "7001002"


@pytest.mark.asyncio
async def test_danda_chunked_id_reaches_workflow_and_api_without_truncation(
    engine, executor,
):
    """Full flow regression for cv_d5632106d170, using the raw STT final
    exactly as stored in the conversation document."""
    result = await _turn(
        engine,
        "हाँ, मेरा ऑर्डर आई डी है 7001। 0 0 1",
        "s-danda-7001001",
    )
    assert result["slots"]["order_ref"] == "7001001"
    assert result["done"]
    assert len(executor.calls) == 1
    assert executor.calls[0]["args"]["order_ref"] == "7001001"


@pytest.mark.asyncio
async def test_partial_fragment_gets_at_most_one_concise_ack(engine, executor):
    await _turn(engine, "hello", "s-ack")
    partial = await _turn(engine, "seven zero", "s-ack")
    assert partial["reply"].count("digits") <= 1
    assert "2" in partial["reply"]  # reports how many digits were captured


# ── identifier capture unit behavior ─────────────────────────────────────────


def test_capture_matcher_and_bounds_come_from_entity_config():
    capture = IdentifierCapture(
        workflow="wf", node="n", variable="order_ref", entity=ORDER_ENTITY,
    )
    assert (capture.min_digits, capture.max_digits) == (7, 10)
    assert capture.matches("7001002")
    assert capture.matches("9876501003")
    assert not capture.matches("70010")
    assert capture.overflowed("98765010031")
    assert capture.hold_delay("seven zero") == capture.pause_window
    assert capture.hold_delay("seven zero zero one zero zero two") == 0.0
    assert capture.hold_delay("mujhe agent chahiye") is None


def test_pause_window_is_configurable_and_bounded():
    assert resolve_pause_window(None) == 3.0
    assert resolve_pause_window({"identifier_pause_window": 2.5}) == 2.5
    assert resolve_pause_window({"identifier_pause_window": 99}) == 4.0
    assert resolve_pause_window({"identifier_pause_window": 0.01}) == 0.6
    assert resolve_pause_window({"identifier_pause_window": "junk"}) == 3.0


# ── 16: audio gate retention at browser/telephony rates ─────────────────────


@pytest.mark.parametrize("rate", [16000, 8000])
def test_gate_retention_is_bounded_and_cleared(rate):
    gate = CallerAudioGate()
    gate.enable_utterance_retention(max_seconds=1.0)
    chunk = b"\x01\x02" * (rate // 10)  # 100 ms
    for _ in range(25):  # 2.5 s total against a 1 s cap
        gate._retain(chunk, rate)
    audio, got_rate = gate.take_retained_audio()
    assert got_rate == rate
    assert len(audio) <= int(1.0 * rate * 2) + len(chunk)
    assert gate.take_retained_audio() is None  # consumed
    gate._retain(chunk, rate)
    gate.disable_utterance_retention()
    assert gate.take_retained_audio() is None  # cleared with the mode
    gate._retain(chunk, rate)
    assert gate.take_retained_audio() is None  # no retention while disabled


# ── 21: recording stays wall-clock aligned ──────────────────────────────────


@pytest.mark.parametrize("rate", [16000, 8000])
def test_recording_duration_tracks_caller_clock_under_bot_bursts(rate):
    aligner = AlignedStereoRecorder(sample_rate=rate, chunk_seconds=0.5)
    frame_bytes = int(rate * 2 * 0.02)  # 20 ms caller frames
    total = bytearray()
    now = 1000.0
    for second in range(4):  # 4 s call
        if second == 1:
            # The bot pushes a 2 s reply in ONE burst (faster than realtime).
            total.extend(aligner.add_bot_audio(b"\x11\x22" * (rate * 2)))
        for _ in range(50):  # 1 s of mic audio at 20 ms per frame
            now += 0.02
            total.extend(aligner.add_user_audio(b"\x01\x02" * (frame_bytes // 2),
                                                now=now))
    total.extend(aligner.stop())
    duration = len(total) / (rate * 2 * 2)  # stereo
    assert abs(duration - 4.0) < 0.1, f"recording {duration:.2f}s for a 4s call"
    # stop() is idempotent: no double tail emission.
    assert aligner.stop() == b""


def test_recording_fills_real_input_gaps_once():
    rate = 16000
    aligner = AlignedStereoRecorder(sample_rate=rate, chunk_seconds=10.0)
    frame = b"\x01\x02" * int(rate * 0.02)
    aligner.add_user_audio(frame, now=10.0)
    aligner.add_user_audio(frame, now=11.0)  # 1 s capture gap (muted mic)
    chunk = aligner.stop()
    duration = len(chunk) / (rate * 2 * 2)
    assert abs(duration - 1.02) < 0.05


def test_recording_bot_tail_is_bounded_and_aligned():
    rate = 8000
    aligner = AlignedStereoRecorder(sample_rate=rate, chunk_seconds=10.0)
    frame = b"\x01\x02" * int(rate * 0.02)
    now = 5.0
    for _ in range(10):
        now += 0.02
        aligner.add_user_audio(frame, now=now)
    # Bot burst arrives; the call ends while it is still "playing".
    aligner.add_bot_audio(b"\x11\x22" * (rate * 2))  # 2 s of bot audio
    chunk = aligner.stop()
    duration = len(chunk) / (rate * 2 * 2)
    # 0.2 s of mic + the bot tail (2 s − what fit under the caller clock).
    assert 2.0 <= duration <= 2.3
