"""OYO customer verification accepts equivalent names across scripts safely."""

from fastapi.testclient import TestClient

from oyo.api.main import _contains, app


def test_devanagari_guest_name_matches_english_booking_name():
    assert _contains("राहुल शर्मा", "Rahul Sharma")
    assert _contains("guest name is राहुल शर्मा.", "Rahul Sharma")


def test_partial_or_different_name_does_not_verify():
    assert not _contains("र", "Rahul Sharma")
    assert not _contains("राहुल कुमार", "Rahul Sharma")
    assert not _contains("Rahul", "Rahul Sharma")


def test_verification_endpoint_accepts_spaced_id_and_devanagari_name():
    with TestClient(app) as client:
        response = client.post("/api/v1/customers/verify", json={
            "booking_id": "6 0 1 0 0 1",
            "guest_name": "guest name is राहुल शर्मा.",
        })

    assert response.status_code == 200
    assert response.json() == {
        "verified": True,
        "booking_id": "601001",
        "matched_on": "guest_name",
    }


def test_verification_endpoint_rejects_wrong_devanagari_name():
    with TestClient(app) as client:
        response = client.post("/api/v1/customers/verify", json={
            "booking_id": "601001",
            "guest_name": "राहुल कुमार",
        })

    assert response.status_code == 401
    assert response.json()["verified"] is False
