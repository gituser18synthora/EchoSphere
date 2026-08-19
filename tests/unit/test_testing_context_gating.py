"""Testing Studio must not turn tenant-authored fixtures into verification."""

from backend.routers.testing import (
    _asks_about_verified_context,
    _testing_customer_context,
)


def test_manual_verification_flag_hides_all_customer_facts():
    values, required, verified = _testing_customer_context(
        {
            "identity_verified": True,
            "booking_id": "601001",
            "guest_name": "Rahul Sharma",
            "amount_pending_inr": 2400,
        },
        None,
    )

    assert values == {}
    assert required is True
    assert verified is False


def test_verified_workflow_output_replaces_manual_payload():
    values, required, verified = _testing_customer_context(
        {
            "identity_verified": False,
            "booking_id": "stale-booking",
            "guest_name": "Wrong Name",
        },
        {
            "customer_verified": True,
            "booking_id": "601001",
            "guest_name": "Rahul Sharma",
            "hotel_name": "OYO Townhouse",
        },
    )

    assert values == {
        "booking_id": "601001",
        "guest_name": "Rahul Sharma",
        "hotel_name": "OYO Townhouse",
    }
    assert required is True
    assert verified is True


def test_unrelated_manual_context_keeps_existing_behavior():
    values, required, verified = _testing_customer_context(
        {"ticket_number": "T-42", "issue": "Wi-Fi"},
        None,
    )

    assert values == {"ticket_number": "T-42", "issue": "Wi-Fi"}
    assert required is False
    assert verified is False


def test_verified_booking_field_question_uses_verified_context():
    context = {
        "customer_verified": True,
        "booking_id": "601001",
        "hotel_name": "OYO Townhouse",
        "checkin_date": "2026-08-20",
    }

    assert _asks_about_verified_context(
        "What are my hotel and check-in date?", context,
    )
    assert not _asks_about_verified_context(
        "What is the standard cancellation policy?", context,
    )


def test_filler_prefix_and_stt_variants_still_reach_verified_context():
    # Real callers repeat yes/no and STT mangles "check-in" — the question
    # after the filler must still be answered from the verified booking.
    context = {
        "customer_verified": True,
        "booking_id": "601001",
        "hotel_name": "OYO Townhouse",
        "checkin_date": "2026-08-20",
        "checkout_date": "2026-08-22",
        "payment_status": "partially_paid",
        "amount_pending": 2400,
    }

    for text in (
        "No no. What is my checking date?",
        "Yes yes, what is my checkout date?",
        "No, how much payment is pending?",
        "Okay okay, what is the hotel name?",
        "No no, I mean the checkout date, when is it?",
    ):
        assert _asks_about_verified_context(text, context), text
