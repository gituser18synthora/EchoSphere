"""Shareable conversation document (PDF / text download from Conversation
Review): viewer-timezone clock times, drawer-equivalent AI summary, no costs,
and Indic transcripts that survive the trip into both file formats."""

from datetime import datetime, timedelta

import fitz
import pytest

from backend.reports import conversation_document as cd
from backend.reports.conversation_document import (
    build_conversation_document,
    render_conversation_pdf,
    render_conversation_text,
    resolve_viewer_zone,
)
from shared.models import ConversationSession

IST = resolve_viewer_zone("Asia/Kolkata")
STARTED = datetime(2026, 7, 29, 10, 15, 0)  # naive UTC, as MySQL stores it
NAMASTE = "नमस्ते"  # "namaste" in Devanagari
KSHAMA = "क्षमा"  # "kshama": a conjunct that needs shaping


def _conversation(**overrides) -> ConversationSession:
    values = dict(
        id="cv_doc000001", tenant_id="tn-001", bot_id="bot-101", channel="voice",
        caller_masked="+91 ••••3210", started_at=STARTED, duration_sec=134,
        sentiment="neutral", contained=True, escalation_reason=None,
        language="hi-IN", disposition=None, cost_usd=0.42,
    )
    values.update(overrides)
    return ConversationSession(**values)


def _at(seconds: float) -> str:
    return (STARTED + timedelta(seconds=seconds)).isoformat(timespec="milliseconds") + "Z"


TURNS = [
    {"turn": 1, "speaker": "bot", "text": f"{NAMASTE}! Main Aditya bol raha hoon.",
     "at": _at(1), "route": "workflow", "latencyMs": 812, "costUsd": 0.01},
    {"turn": 2, "speaker": "user", "text": f"haan boliye {KSHAMA}", "at": _at(4.5)},
    {"turn": 3, "speaker": "user", "text": "   "},  # nothing said: not a turn
    {"turn": 4, "speaker": "bot", "text": "Line one\r\nLine two"},
]

SUMMARY = {  # serialize_conversation_memory() shape
    "status": "completed",
    "callOutcome": "promise_to_pay",
    "summary": "Customer agreed to pay on Friday.",
    "structuredFields": {"reach_customer_location": "Yes", "call_cx": None},
    "structuredFieldLabels": {"reach_customer_location": "Reached customer location"},
    "nextBestAction": {"action": "schedule_callback", "priority": "high",
                       "reason": "Confirm the payment"},
    "customerCommitments": [{"type": "payment", "description": "Pay EMI", "amount": 100000,
                             "currency": "INR", "dueDate": "2026-08-01", "status": "open"}],
    "unresolvedItems": ["refund_status"],
    "missingSlots": ["order_id_last4"],
    "importantFacts": ["Customer is travelling"],
}


def _document(**overrides):
    kwargs = dict(bot_name="Zepto MDND Support", turns=TURNS, summary=SUMMARY, zone=IST,
                  now=datetime(2026, 9, 28, 12, 10, 0))
    conversation = overrides.pop("conversation", None) or _conversation()
    kwargs.update(overrides)
    return build_conversation_document(conversation, **kwargs)


def _pdf_text(pdf: fitz.Document) -> str:
    # Short labels are laid out with non-breaking spaces.
    return "".join(page.get_text() for page in pdf).replace("\xa0", " ")


class TestBuild:
    def test_times_render_in_the_viewers_timezone(self):
        document = _document()
        assert ("Started", "29 Jul 2026, 03:45:00 PM") in document.details
        assert document.time_zone == "IST (UTC+05:30)"
        assert [turn.at for turn in document.turns[:2]] == ["03:45:01 PM", "03:45:04 PM"]
        assert document.generated_at == "28 Sep 2026, 05:40:00 PM"

    def test_missing_or_unknown_zone_is_labelled_utc(self):
        document = _document(zone=resolve_viewer_zone("Not/AZone"))
        assert document.time_zone == "UTC"
        assert ("Started", "29 Jul 2026, 10:15:00 AM") in document.details

    @pytest.mark.parametrize("zone, label", [
        ("Asia/Calcutta", "IST (UTC+05:30)"),  # what Chrome reports for India
        ("America/New_York", "EDT (UTC-04:00)"),  # DST as of the call, not today
        ("Asia/Dubai", "UTC+04:00"),  # tzdata abbreviation is just "+04"
    ])
    def test_zone_label_is_abbreviation_and_offset(self, zone, label):
        assert _document(zone=resolve_viewer_zone(zone)).time_zone == label

    @pytest.mark.parametrize("name", [None, "", "Asia", "../../etc/passwd", "UTC\x00", "x" * 64])
    def test_malformed_zone_names_fall_back_to_utc(self, name):
        assert resolve_viewer_zone(name).key == "UTC"

    def test_speakers_follow_the_channel_and_blank_turns_are_dropped(self):
        voice = _document()
        assert [(t.role, t.speaker) for t in voice.turns] == [
            ("bot", "Bot"), ("user", "Caller"), ("bot", "Bot")]
        assert voice.turns[-1].text == "Line one\nLine two"
        assert voice.turns[-1].at is None
        chat = _document(conversation=_conversation(channel="whatsapp"))
        assert chat.turns[1].speaker == "Customer"
        assert ("Customer", "+91 ••••3210") in chat.details
        assert ("Channel", "WhatsApp") in chat.details

    def test_escalation_reason_only_for_escalated_calls(self):
        escalated = _document(conversation=_conversation(
            contained=False, escalation_reason="Asked for a human",
            disposition="callback_requested"))
        assert ("Outcome", "Escalated") in escalated.details
        assert ("Escalation reason", "Asked for a human") in escalated.details
        assert ("Disposition", "Callback requested") in escalated.details
        contained = _document(conversation=_conversation(escalation_reason="stale"))
        assert ("Outcome", "Contained") in contained.details
        assert all(label != "Escalation reason" for label, _ in contained.details)

    def test_duration_matches_the_review_list_wording(self):
        assert cd._duration(45) == "45 sec"
        assert cd._duration(134) == "2m 14s"
        assert cd._duration(3725) == "1h 02m 05s"
        assert cd._duration(None) == "0 sec"

    def test_summary_mirrors_the_drawer(self):
        document = _document()
        assert document.summary_text == "Customer agreed to pay on Friday."
        assert document.summary_facts == [
            ("Call outcome", "Promise to pay"),
            ("Reached customer location", "Yes"),
            ("Call cx", "Not determined"),
            ("Next best action", "Schedule callback (high priority) — Confirm the payment"),
        ]
        assert document.summary_lists == [
            ("Customer commitments", ["Pay EMI — INR 1,00,000 (due 2026-08-01) · open"]),
            ("Pending", ["Refund status", "Order id last4"]),
            ("Important facts", ["Customer is travelling"]),
        ]

    def test_no_summary_section_without_post_call_analysis(self):
        assert not _document(summary=None).has_summary
        assert not _document(summary={"status": "processing", "summary": None}).has_summary

    @pytest.mark.parametrize("value, expected", [
        (100000, "1,00,000"), (2500.5, "2,500.5"), (999, "999"),
        (-1234567, "-12,34,567"), ("n/a", "n/a"),
    ])
    def test_amounts_use_indian_grouping(self, value, expected):
        assert cd._amount(value) == expected


class TestText:
    def test_utf8_with_bom_keeps_indic_text_and_speakers(self):
        raw = render_conversation_text(_document())
        assert raw.startswith("\ufeff".encode("utf-8"))
        text = raw.decode("utf-8-sig")
        assert f"[03:45:01 PM] Bot: {NAMASTE}! Main Aditya bol raha hoon." in text
        assert f"[03:45:04 PM] Caller: haan boliye {KSHAMA}" in text
        assert "Bot: Line one\n    Line two" in text
        assert "Conversation ID : cv_doc000001" in text
        assert "Times are shown in IST (UTC+05:30)." in text
        assert "AI SUMMARY" in text and "Reached customer location: Yes" in text

    def test_never_carries_costs_or_internal_trace(self):
        text = render_conversation_text(_document()).decode("utf-8-sig")
        for internal in ("0.42", "0.01", "812", "workflow", "cost", "Cost"):
            assert internal not in text

    def test_empty_transcript_says_so(self):
        text = render_conversation_text(_document(turns=[], summary=None)).decode("utf-8-sig")
        assert "No turns were captured for this call." in text
        assert "AI SUMMARY" not in text


class TestPdf:
    def test_lays_out_details_summary_and_every_turn(self):
        pdf = fitz.open("pdf", render_conversation_pdf(_document()))
        text = _pdf_text(pdf)
        for expected in ("Conversation transcript", "cv_doc000001", "Zepto MDND Support",
                         "29 Jul 2026, 03:45:00 PM", "AI summary", "Reached customer location",
                         "Transcript", "Main Aditya bol raha hoon.", "03:45:04 PM",
                         "Line one", "Line two", f"Page 1 of {pdf.page_count}"):
            assert expected in text, expected
        assert pdf.metadata["title"] == "Conversation cv_doc000001"
        assert "0.42" not in text and "812" not in text

    def test_indic_text_gets_a_shaping_font_not_tofu(self):
        pdf = fitz.open("pdf", render_conversation_pdf(_document()))
        fonts = {font[3] for page in pdf for font in page.get_fonts()}
        assert any("Devanagari" in name for name in fonts), fonts

    def test_turn_text_is_escaped_not_interpreted(self):
        turns = [{"turn": 1, "speaker": "user", "text": "<b>order</b> & <script>x</script>"}]
        text = _pdf_text(fitz.open("pdf", render_conversation_pdf(_document(turns=turns))))
        assert "<b>order</b> & <script>x</script>" in text

    def test_long_calls_paginate_with_a_footer_on_every_page(self):
        turns = [{"turn": i + 1, "speaker": "bot" if i % 2 else "user",
                  "text": f"Turn {i + 1}: " + "kuch lamba jawab " * 12, "at": _at(i * 5)}
                 for i in range(120)]
        pdf = fitz.open("pdf", render_conversation_pdf(_document(turns=turns)))
        assert pdf.page_count > 3
        for index, page in enumerate(pdf):
            assert f"Page {index + 1} of {pdf.page_count}" in page.get_text()
        assert "Turn 120:" in _pdf_text(pdf)

    def test_stranded_transcript_heading_detected(self):
        spacer = '<div style="height: 700pt"></div>'
        _pdf, stranded = cd._lay_out(f'{spacer}<h2 id="transcript">Transcript</h2><p>x</p>')
        assert stranded
        _pdf, stranded = cd._lay_out('<h2 id="transcript">Transcript</h2><p>x</p>')
        assert not stranded

    def test_stranded_heading_moves_the_transcript_to_a_new_page(self, monkeypatch):
        monkeypatch.setattr(cd, "_HEADING_KEEP_PT", 10_000)  # always "too close"
        pdf = fitz.open("pdf", render_conversation_pdf(_document(summary=None)))
        assert "Transcript" not in pdf[0].get_text()
        assert pdf[1].get_text().startswith("Transcript")
