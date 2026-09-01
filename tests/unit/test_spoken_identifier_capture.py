"""Spoken numeric-identifier capture across the shared voice pipeline.

A caller dictates a booking ID / OTP / reference in any supported Indian
language — digit words, native-script digits, "double"/"triple" constructs,
spaced digit groups, chunked across turns. Covers:

- spoken_numbers: multilingual normalization + the dictation guard
- entity_extractor: digit-expecting entities match normalized transcripts
- workflow ask nodes: multi-turn accumulation of partial identifiers
- transcript_gate: dictated numbers survive the noise rules
- Deepgram Flux adapter: numerals pass-through on the connection query
"""

import re

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.entity_extractor import _expects_digits, extract_entity
from shared.orchestration.spoken_numbers import (
    digits_dominant,
    normalize_script_digits,
    spoken_digit_sequence,
    spoken_digit_text,
    verbalized_digits,
)

# ── normalization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("utterance", [
    "six zero one zero one one",
    "Six zero one zero double one.",
    "601011",
    "6 0 1 0 1 1",
    "60 10 11",
    "छह शून्य एक शून्य एक एक",                      # Hindi words
    "छह zero one zero double one",                  # Hindi/English code-switch
    "सहा शून्य एक शून्य एक एक",                     # Marathi (सहा = 6)
    "ஆறு பூஜ்யம் ஒன்று பூஜ்யம் ஒன்று ஒன்று",        # Tamil words
    "aaru poojyam onnu poojyam onnu onnu",          # Tamil/Malayalam romanized
    "ఆరు సున్నా ఒకటి సున్నా ఒకటి ఒకటి",             # Telugu words
    "ਛੇ ਸਿਫ਼ਰ ਇੱਕ ਸਿਫ਼ਰ ਇੱਕ ਇੱਕ",                    # Punjabi words
    "چھ صفر ایک صفر ایک ایک",                        # Urdu words
    "सिक्स जीरो वन जीरो वन वन",                     # English words transliterated by STT
    "٦٠١٠١١",                                        # Arabic-Indic digit chars
    "੬੦੧੦੧੧",                                        # Gurmukhi digit chars
])
def test_spoken_forms_normalize_to_601011(utterance):
    assert spoken_digit_sequence(utterance) == "601011"


def test_repeat_constructs():
    assert spoken_digit_sequence("nine triple two") == "9222"
    assert spoken_digit_sequence("double nine double one") == "9911"
    assert spoken_digit_sequence("डबल नौ डबल एक") == "9911"


@pytest.mark.parametrize("utterance", [
    "हाँ, मेरा ऑर्डर आई डी है 7001। 0 0 1",
    "7001। 0 0 1",
    "7001॥ 0 0 1",
    "7001. 0 0 1",
    "7 001 001",
])
def test_stt_chunk_punctuation_preserves_every_identifier_digit(utterance):
    """Regression for cv_d5632106d170: the raw final was complete, but the
    danda split it into runs and longest-run selection discarded trailing
    001."""
    assert spoken_digit_sequence(utterance) == "7001001"


@pytest.mark.parametrize("utterance,forbidden", [
    ("7:00", "700"),
    ("07:00:01", "070001"),
    ("31/08/2026", "31082026"),
    ("7.001", "7001"),
])
def test_time_date_and_decimal_punctuation_is_not_fused(utterance, forbidden):
    assert spoken_digit_sequence(utterance) != forbidden


def test_script_digits_translate_generically():
    # Any Unicode decimal digit maps via the character database — scripts the
    # lexicon never mentions still work.
    assert normalize_script_digits("৬০১০১১") == "601011"   # Bengali
    assert normalize_script_digits("೬೦೧೦೧೧") == "601011"   # Kannada
    assert normalize_script_digits("abc ०९") == "abc 09"


def test_non_number_text_untouched():
    for text in ("I want to cancel my booking",
                 "confirm with the property please",
                 "मुझे बुकिंग कैंसिल करनी है"):
        assert spoken_digit_text(text) == text
    # verbalized_digits keeps its long-standing behavior for amounts.
    assert verbalized_digits("पचास हज़ार") == "50000"


def test_digits_dominant_guard():
    assert digits_dominant("six zero")
    assert digits_dominant("one zero double one")
    assert digits_dominant("yes. six zero one zero double one.")
    assert digits_dominant("haan ji 601011")
    assert not digits_dominant("my room is on floor 2")
    assert not digits_dominant("I paid two thousand rupees yesterday")
    assert not digits_dominant("book a room for 2 guests")
    assert not digits_dominant("okay thanks")
    assert not digits_dominant("")
    assert digits_dominant("हाँ, मेरा ऑर्डर आईडी है सेवन जी")
    assert spoken_digit_sequence("हाँ, मेरा ऑर्डर आईडी है सेवन जी") == "7"


# ── entity extraction ────────────────────────────────────────────────────────

BOOKING_ENTITY = {"name": "booking_id", "dataType": "text",
                  "regexPattern": r"(?:BK[-\s]?)?([0-9]{4,10})"}


def test_expects_digits_detection():
    assert _expects_digits(BOOKING_ENTITY)
    assert _expects_digits({"dataType": "phone"})
    assert _expects_digits({"dataType": "account_number"})
    assert not _expects_digits({"dataType": "text"})
    assert not _expects_digits({"dataType": "email"})


@pytest.mark.parametrize("utterance,normalized", [
    ("601011", False),                              # raw digits: old path
    ("BK 601011", False),
    ("six zero one zero one one", True),
    ("Six zero one zero double one.", True),
    ("6 0 1 0 1 1", True),
    ("छह शून्य एक शून्य एक एक", True),
    ("my booking id is six zero one zero one one", True),
])
def test_extract_booking_id_spoken_forms(utterance, normalized):
    result = extract_entity(utterance, BOOKING_ENTITY)
    assert result["matched"], utterance
    assert result["value"] == "601011"
    assert result["normalized"] is normalized


def test_extraction_not_over_applied():
    # A digit-expecting entity still refuses text without a valid sequence.
    assert not extract_entity("I want to book for two guests",
                              BOOKING_ENTITY)["matched"]
    # Non-digit entities never get the spoken-number rewrite.
    free_text = extract_entity("six zero one zero one one",
                               {"name": "note", "dataType": "text"})
    assert not free_text["matched"]
    # Email extraction is unchanged.
    email = extract_entity("it's rahul.sharma@example.com",
                           {"name": "email", "dataType": "email"})
    assert email["value"] == "rahul.sharma@example.com"


def test_phone_type_pattern_accepts_spoken_digits():
    result = extract_entity("nine eight one zero double zero one zero zero one",
                            {"name": "phone", "dataType": "phone"})
    assert result["matched"]
    assert result["maskedValue"] is not None


HONASA_ORDER_REF = {
    "name": "order_ref", "dataType": "text",
    "regexPattern": r"(?<![0-9])([0-9]{10}|[0-9]{7})(?![0-9])",
}


@pytest.mark.parametrize("utterance,expected", [
    ("7001003", "7001003"),
    ("my order is seven zero zero one zero zero three", "7001003"),
    ("registered mobile is 9876501003", "9876501003"),
    ("हाँ, मेरा ऑर्डर आई डी है 7001। 0 0 1", "7001001"),
])
def test_honasa_order_reference_accepts_only_supported_exact_lengths(
    utterance, expected,
):
    result = extract_entity(utterance, HONASA_ORDER_REF)
    assert result["matched"]
    assert result["value"] == expected


@pytest.mark.parametrize("utterance", [
    "7001", "70010003", "987650100", "98765010031",
])
def test_honasa_order_reference_rejects_partial_or_extra_digits(utterance):
    assert not extract_entity(utterance, HONASA_ORDER_REF)["matched"]


# ── workflow ask-node accumulation ───────────────────────────────────────────

BOOKING_FLOW = {
    "id": "wf_digits", "version": 1, "name": "Digit capture journey",
    "nodes": [
        {"id": "n1", "kind": "start", "label": "Call starts"},
        {"id": "n2", "kind": "ask", "label": "Ask booking id",
         "config": {"question": "Could you share your booking ID?",
                    "variable": "booking_id", "entityType": "text",
                    "pattern": r"(?:BK[-\s]?)?([0-9]{6})"}},
        {"id": "n3", "kind": "end", "label": "End",
         "config": {"text": "Noted, thank you!"}},
    ],
    "edges": [
        {"id": "e1", "from": "n1", "to": "n2"},
        {"id": "e2", "from": "n2", "to": "n3"},
        {"id": "e3", "from": "n2", "to": "n3", "label": "fallback"},
    ],
}


@pytest.fixture()
def engine(monkeypatch):
    eng = wfe.WorkflowEngine()

    async def _mem_checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem_checkpointer)
    monkeypatch.setattr(
        wfe, "load_workflow_definition", lambda tenant_id, bot_id, name: BOOKING_FLOW
    )
    return eng


async def _turn(engine, text, session):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="t", bot_id="b",
        workflow_name="digit_capture_journey", user_text=text,
    )


@pytest.mark.asyncio
async def test_single_turn_spoken_id(engine):
    await _turn(engine, "hello", "s-one")
    result = await _turn(engine, "six zero one zero double one", "s-one")
    assert result["slots"]["booking_id"] == "601011"
    assert result["done"]


@pytest.mark.asyncio
async def test_identifier_in_workflow_entry_is_consumed_without_reasking(engine):
    result = await _turn(
        engine, "my booking ID is six zero one zero double one", "s-entry",
    )
    assert result["slots"]["booking_id"] == "601011"
    assert result["done"]
    assert "share your booking ID" not in result["reply"]


@pytest.mark.asyncio
async def test_partial_identifier_in_workflow_entry_accumulates(engine):
    partial = await _turn(
        engine, "mera booking ID hai six zero one", "s-entry-partial",
    )
    assert not partial["done"]
    assert "booking_id" not in partial["slots"]
    assert re.search(r"noted (the digits|\d+ digits?)", partial["reply"])

    finished = await _turn(engine, "zero double one", "s-entry-partial")
    assert finished["slots"]["booking_id"] == "601011"
    assert finished["done"]


@pytest.mark.asyncio
async def test_partial_id_accumulates_across_turns(engine):
    await _turn(engine, "hello", "s-two")
    partial = await _turn(engine, "six zero", "s-two")
    assert not partial["done"]
    assert "booking_id" not in partial["slots"]
    # Progress reply, not the canned "didn't catch that" retry.
    assert re.search(r"noted (the digits|\d+ digits?)", partial["reply"])
    finished = await _turn(engine, "one zero double one", "s-two")
    assert finished["slots"]["booking_id"] == "601011"
    assert finished["done"]


@pytest.mark.asyncio
async def test_partial_three_chunks(engine):
    await _turn(engine, "hello", "s-three")
    await _turn(engine, "six zero", "s-three")
    await _turn(engine, "one zero", "s-three")
    finished = await _turn(engine, "double one", "s-three")
    assert finished["slots"]["booking_id"] == "601011"


@pytest.mark.asyncio
async def test_partial_does_not_burn_retries(engine):
    # Two partial chunks then completion: with retries burned per chunk the
    # second chunk would already exhaust _MAX_ASK_RETRIES and take the
    # fallback edge; accumulation must keep the ask open instead.
    await _turn(engine, "hello", "s-four")
    for chunk in ("six", "zero", "one zero"):
        result = await _turn(engine, chunk, "s-four")
        assert not result["done"], chunk
    finished = await _turn(engine, "one one", "s-four")
    assert finished["slots"]["booking_id"] == "601011"


@pytest.mark.asyncio
async def test_unrelated_words_do_not_accumulate(engine):
    await _turn(engine, "hello", "s-five")
    await _turn(engine, "six zero", "s-five")
    # An off-topic sentence is NOT folded into the identifier.
    result = await _turn(engine, "actually where is the hotel located", "s-five")
    assert "booking_id" not in result["slots"]
    # The held digits remain usable afterwards.
    finished = await _turn(engine, "one zero one one", "s-five")
    assert finished["slots"]["booking_id"] == "601011"


# ── transcript gate ──────────────────────────────────────────────────────────


def test_gate_accepts_dictated_digits():
    from voice_runtime.transcript_gate import SegmentQuality, assess_transcript

    # Very short audio would trip noise_duration for arbitrary words…
    noise = assess_transcript("xz", SegmentQuality(audio_seconds=0.15))
    assert not noise.accepted
    # …but a dictated digit chunk is a real answer.
    for text in ("six zero", "one zero double one", "छह शून्य", "6 0 1 0 1 1"):
        verdict = assess_transcript(text, SegmentQuality(audio_seconds=0.15))
        assert verdict.accepted, text


# ── Deepgram Flux numerals pass-through ─────────────────────────────────────


def test_flux_extra_query_params(monkeypatch):
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTSettings

    from voice_runtime.deepgram_stt import EchoDeepgramFluxSTTService

    service = EchoDeepgramFluxSTTService(
        api_key="key", sample_rate=8000,
        settings=DeepgramFluxSTTSettings(model="flux-general-multi"),
        extra_query_params={"numerals": "true"},
    )
    query = service._build_query_string()
    assert "numerals=true" in query
    assert "model=flux-general-multi" in query
