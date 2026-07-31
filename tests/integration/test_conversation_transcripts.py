"""Conversation detail: transcript resolution across document generations,
canonical turn mapping, and authorized call-recording playback.

The voice runtime keys transcript documents by its own session id and stamps
``control_plane_id`` (the MySQL row id). Legacy runtime documents carry no
such link and are matched by tenant/bot/start-time. Turns are served in the
UI shape regardless of which writer produced them.
"""

import struct
import uuid
import wave
from datetime import datetime, timedelta

import pymongo
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
from sqlalchemy import text as sa_text

from backend.main import app
from backend.core.security import create_access_token
from shared.config import get_settings
from shared.db.mysql import get_engine, get_sessionmaker

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
TENANT_A = "tn-001"
BOT_A = "bot-101"

_STARTED = datetime(2026, 7, 29, 10, 15, 0)


def _bearer(email: str) -> dict:
    from sqlalchemy import select

    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tenant_a_admin():
    return _bearer("priya.sharma@meridianhealth.com")


@pytest.fixture(scope="module")
def tenant_b_admin():
    return _bearer("admin@pokket.com")


def _mongo_collection():
    settings = get_settings()
    client = pymongo.MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=4000)
    return client, client[settings.mongodb_database]["conversation_transcripts"]


def _insert_row(row_id: str, *, started_at: datetime, duration: int) -> None:
    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "INSERT INTO conversation_sessions "
            "(id, tenant_id, bot_id, channel, started_at, duration_sec, sentiment, "
            " contained, cost_usd, language, flagged, status, is_deleted, created_at, updated_at) "
            "VALUES (:id, :t, :b, 'voice', :st, :d, 'neutral', 1, 0, 'hi-IN', 0, 'completed', 0, NOW(), NOW())"
        ), {"id": row_id, "t": TENANT_A, "b": BOT_A, "st": started_at, "d": duration})


_RUNTIME_TURNS = [
    {"role": "bot", "text": "Namaskar! Main Aditya bol raha hoon.",
     "ts": _STARTED.timestamp() + 1.0, "route": "workflow", "kbUsed": False,
     "kbSources": [], "latencyMs": {"total": 812.4, "llm_first_token": 300.2}},
    {"role": "user", "text": "haan boliye, sun raha hoon",
     "ts": _STARTED.timestamp() + 4.5, "route": "workflow", "kbUsed": False,
     "kbSources": [], "latencyMs": {}},
]


@pytest.fixture(scope="module", autouse=True)
def seed_and_cleanup():
    linked_id = f"cv_{_SUFFIX}lnk1"
    legacy_id = f"cv_{_SUFFIX}leg1"
    recorded_id = f"cv_{_SUFFIX}rec1"
    missing_id = f"cv_{_SUFFIX}gone"

    _insert_row(linked_id, started_at=_STARTED, duration=17)
    _insert_row(legacy_id, started_at=_STARTED + timedelta(minutes=5), duration=9)
    _insert_row(recorded_id, started_at=_STARTED + timedelta(minutes=10), duration=2)
    _insert_row(missing_id, started_at=_STARTED + timedelta(minutes=15), duration=4)

    # A tiny stereo WAV pretending to be the recorded call.
    recordings_root = Path(get_settings().voice_recordings_dir)
    rel = f"{TENANT_A}/vs_{_SUFFIX}rec.wav"
    wav_path = recordings_root / rel
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        frames = b"".join(struct.pack("<hh", i % 2000, -(i % 2000)) for i in range(8000 * 2))
        wav.writeframes(frames)

    mongo_client, col = _mongo_collection()
    docs = [
        {   # current runtime generation: linked via control_plane_id
            "session_id": f"vs_{_SUFFIX}lnk", "control_plane_id": linked_id,
            "tenant_id": TENANT_A, "bot_id": BOT_A, "channel": "vaani",
            "started_at": _STARTED, "duration_sec": 17,
            "turns": _RUNTIME_TURNS,
        },
        {   # legacy generation: no control_plane_id, matched by time window
            "session_id": f"vs_{_SUFFIX}leg",
            "tenant_id": TENANT_A, "bot_id": BOT_A, "channel": "vaani",
            "started_at": _STARTED + timedelta(minutes=5, seconds=2), "duration_sec": 9,
            "turns": _RUNTIME_TURNS[:1],
        },
        {   # call with a recording on disk
            "session_id": f"vs_{_SUFFIX}rec", "control_plane_id": recorded_id,
            "tenant_id": TENANT_A, "bot_id": BOT_A, "channel": "vaani",
            "started_at": _STARTED + timedelta(minutes=10), "duration_sec": 2,
            "turns": _RUNTIME_TURNS,
            "recording": {"path": rel, "mimeType": "audio/wav", "durationSec": 2.0,
                          "sampleRate": 8000, "channels": 2},
        },
        {   # recording reference whose file vanished
            "session_id": f"vs_{_SUFFIX}gone", "control_plane_id": missing_id,
            "tenant_id": TENANT_A, "bot_id": BOT_A, "channel": "vaani",
            "started_at": _STARTED + timedelta(minutes=15), "duration_sec": 4,
            "turns": [],
            "recording": {"path": f"{TENANT_A}/does-not-exist-{_SUFFIX}.wav",
                          "mimeType": "audio/wav", "durationSec": 4.0},
        },
    ]
    col.insert_many(docs)

    yield {"linked": linked_id, "legacy": legacy_id, "recorded": recorded_id,
           "missing": missing_id, "wav_path": wav_path}

    col.delete_many({"session_id": {"$regex": f"^vs_{_SUFFIX}"}})
    mongo_client.close()
    wav_path.unlink(missing_ok=True)
    with get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM conversation_sessions WHERE id LIKE :p"),
                     {"p": f"cv_{_SUFFIX}%"})


class TestTranscriptResolution:
    def test_runtime_doc_resolved_via_control_plane_id(self, client, tenant_a_admin,
                                                       seed_and_cleanup):
        detail = _data(client.get(f"{API}/conversations/{seed_and_cleanup['linked']}",
                                  headers=tenant_a_admin))
        turns = detail["transcript"]
        assert [t["speaker"] for t in turns] == ["bot", "user"]
        assert turns[0]["turn"] == 1 and turns[1]["turn"] == 2
        assert turns[0]["text"].startswith("Namaskar")
        # Runtime latency dict mapped to a total; timestamps ISO-formatted.
        assert turns[0]["latencyMs"] == 812
        assert turns[0]["at"].endswith("Z") and "T" in turns[0]["at"]
        assert turns[0]["route"] == "workflow"

    def test_legacy_doc_matched_by_time_window(self, client, tenant_a_admin,
                                               seed_and_cleanup):
        detail = _data(client.get(f"{API}/conversations/{seed_and_cleanup['legacy']}",
                                  headers=tenant_a_admin))
        assert len(detail["transcript"]) == 1
        assert detail["transcript"][0]["speaker"] == "bot"

    def test_cross_tenant_detail_is_hidden(self, client, tenant_b_admin, seed_and_cleanup):
        response = client.get(f"{API}/conversations/{seed_and_cleanup['linked']}",
                              headers=tenant_b_admin)
        assert response.status_code == 404


class TestCallRecording:
    def test_detail_exposes_recording_descriptor(self, client, tenant_a_admin,
                                                 seed_and_cleanup):
        cid = seed_and_cleanup["recorded"]
        detail = _data(client.get(f"{API}/conversations/{cid}", headers=tenant_a_admin))
        rec = detail["recording"]
        assert rec is not None
        assert rec["url"] == f"/api/v1/conversations/{cid}/recording"
        assert rec["mimeType"] == "audio/wav"
        assert rec["durationSec"] == 2.0
        assert rec["sizeBytes"] > 44

    def test_recording_streams_audio(self, client, tenant_a_admin, seed_and_cleanup):
        cid = seed_and_cleanup["recorded"]
        response = client.get(f"{API}/conversations/{cid}/recording",
                              headers=tenant_a_admin)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.content[:4] == b"RIFF"
        assert len(response.content) == seed_and_cleanup["wav_path"].stat().st_size

    def test_recording_download_sets_disposition(self, client, tenant_a_admin,
                                                 seed_and_cleanup):
        cid = seed_and_cleanup["recorded"]
        response = client.get(f"{API}/conversations/{cid}/recording?download=true",
                              headers=tenant_a_admin)
        assert response.status_code == 200
        assert "attachment" in response.headers.get("content-disposition", "")

    def test_recording_is_tenant_scoped(self, client, tenant_b_admin, seed_and_cleanup):
        response = client.get(
            f"{API}/conversations/{seed_and_cleanup['recorded']}/recording",
            headers=tenant_b_admin)
        assert response.status_code == 404

    def test_missing_file_is_null_descriptor_and_404(self, client, tenant_a_admin,
                                                     seed_and_cleanup):
        cid = seed_and_cleanup["missing"]
        detail = _data(client.get(f"{API}/conversations/{cid}", headers=tenant_a_admin))
        assert detail["recording"] is None
        assert client.get(f"{API}/conversations/{cid}/recording",
                          headers=tenant_a_admin).status_code == 404

    def test_traversal_reference_is_rejected(self, client, tenant_a_admin,
                                             seed_and_cleanup):
        mongo_client, col = _mongo_collection()
        try:
            col.update_one({"control_plane_id": seed_and_cleanup["missing"]},
                           {"$set": {"recording.path": "../../.env"}})
            response = client.get(
                f"{API}/conversations/{seed_and_cleanup['missing']}/recording",
                headers=tenant_a_admin)
            assert response.status_code == 404
        finally:
            mongo_client.close()

    def test_transcript_export_uses_mapped_turns(self, client, tenant_a_admin,
                                                 seed_and_cleanup):
        cid = seed_and_cleanup["linked"]
        response = client.get(
            f"{API}/conversations/{cid}/transcript/export?format=csv",
            headers=tenant_a_admin)
        assert response.status_code == 200
        body = response.content.decode("utf-8", errors="replace")
        assert "Namaskar" in body
        assert "bot" in body and "user" in body
