"""Natural Conversation filler audio: catalog + preview endpoints, and the
selection round trip through voice settings into the runtime config.

- GET /bots/{id}/natural-conversation/audio lists every pre-rendered sound
  per kind and voice gender with stable clip ids, the bot's active voice per
  language and the voiced cue options;
- GET …/audio/clip streams the exact WAV the runtime plays for a clip id;
- GET …/audio/cue renders one voiced cue in the bot's voice (stubbed here);
- the selection keys persist through PUT /bots/{id}/voice-settings, are
  validated strictly, and reach ResolvedBotConfig.human_speech.
"""

import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import backend.routers.natural_conversation as nc_router
from backend.core.security import create_access_token
from backend.main import app
from shared.bot_config import _load_config_sync
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import BotLanguage, User, VoiceBot, VoiceBotSetting
from shared.orchestration.naturalness import FILLER_SOUND_KINDS, SpeechNaturalnessPlanner
from voice_runtime.voiced_cues import VoicedCueLibrary

pytestmark = pytest.mark.integration

API = "/api/v1"
TENANT = "tn-001"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tenant_admin():
    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(User.email == "priya.sharma@meridianhealth.com"))
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture()
def bot():
    session = get_sessionmaker()()
    row = VoiceBot(
        id=new_id("bot"), tenant_id=TENANT, name=f"FillerAudio {uuid.uuid4().hex[:6]}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(row)
    session.flush()
    session.add(BotLanguage(bot_id=row.id, language_code="hi-IN"))
    session.commit()
    bot_id = row.id
    yield bot_id
    session.execute(VoiceBotSetting.__table__.delete().where(VoiceBotSetting.bot_id == bot_id))
    session.execute(BotLanguage.__table__.delete().where(BotLanguage.bot_id == bot_id))
    session.execute(VoiceBot.__table__.delete().where(VoiceBot.id == bot_id))
    session.commit()
    session.close()


class TestCatalog:
    def test_lists_every_kind_and_gender_with_stable_ids(self, client, tenant_admin, bot):
        r = client.get(f"{API}/bots/{bot}/natural-conversation/audio", headers=tenant_admin)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert [k["id"] for k in data["kinds"]] == list(FILLER_SOUND_KINDS)
        assert data["genders"] == ["male", "female", "neutral"]
        for kind in FILLER_SOUND_KINDS:
            for gender in ("male", "female", "neutral"):
                clips = data["clips"][kind][gender]
                assert clips, (kind, gender)
                for clip in clips:
                    assert clip["kind"] == kind and clip["gender"] == gender
                    assert clip["source"] in ("recording", "synthesized")
                    assert clip["durationMs"] > 100
                    assert clip["id"].startswith(("file:", "synth:"))
        # The bot's language list carries the runtime voice + its gender.
        assert data["voices"] and data["voices"][0]["language"] == "hi-IN"
        assert data["voices"][0]["gender"] in ("male", "female", "neutral")
        # Voiced cue options per base language, default first, with defaults.
        hi = data["cues"]["hi"]
        assert hi["options"]["hmm"][0]["text"] == "हम्म…"
        assert {"id", "text", "ready"} <= set(hi["options"]["hmm"][0])
        assert hi["defaultSelection"]["primary"] == "hmm" and "oh" in hi["defaultSelection"]["alternates"]
        assert data["effective"]["latencyFillerKind"] == "breath"

    def test_requires_access_to_the_bot(self, client, bot):
        r = client.get(f"{API}/bots/{bot}/natural-conversation/audio")
        assert r.status_code == 401


class TestClipPreview:
    def test_streams_the_runtime_clip_as_wav(self, client, tenant_admin, bot):
        catalog = client.get(
            f"{API}/bots/{bot}/natural-conversation/audio", headers=tenant_admin
        ).json()["data"]
        clip = catalog["clips"]["exhale"]["female"][0]
        r = client.get(
            f"{API}/bots/{bot}/natural-conversation/audio/clip",
            params={"id": clip["id"]}, headers=tenant_admin,
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("audio/wav")
        assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WAVE"
        # 24 kHz mono 16-bit: the duration in the catalog matches the bytes.
        pcm_bytes = len(r.content) - 44
        assert abs(pcm_bytes / (24000 * 2) * 1000 - clip["durationMs"]) < 5

    def test_unknown_clip_is_404(self, client, tenant_admin, bot):
        r = client.get(
            f"{API}/bots/{bot}/natural-conversation/audio/clip",
            params={"id": "synth:breath:male:99"}, headers=tenant_admin,
        )
        assert r.status_code == 404


class TestCuePreview:
    def test_renders_the_cue_in_the_bot_voice_through_the_shared_library(
        self, client, tenant_admin, bot, tmp_path, monkeypatch
    ):
        calls = []

        async def renderer(engine, language, text):
            calls.append((engine.get("provider"), language, text))
            return np.full(24000 // 4, 6000, dtype="<i2").tobytes(), 24000

        stub = VoicedCueLibrary(tmp_path, renderer=renderer)
        monkeypatch.setattr(nc_router, "get_voiced_cue_library", lambda: stub)
        r = client.get(
            f"{API}/bots/{bot}/natural-conversation/audio/cue",
            params={"language": "hi-IN", "kind": "hmm", "id": "achha"}, headers=tenant_admin,
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("audio/wav")
        assert calls and calls[0][1] == "hi-IN" and calls[0][2] == "अच्छा…"
        # Cached on disk for the runtime: the same cue renders once.
        client.get(
            f"{API}/bots/{bot}/natural-conversation/audio/cue",
            params={"language": "hi-IN", "kind": "hmm", "id": "achha"}, headers=tenant_admin,
        )
        assert len(calls) == 1
        # Unknown cue id for the language → nothing to render.
        r = client.get(
            f"{API}/bots/{bot}/natural-conversation/audio/cue",
            params={"language": "hi-IN", "kind": "hmm", "id": "nope"}, headers=tenant_admin,
        )
        assert r.status_code == 502


class TestSelectionPersistence:
    def test_selection_round_trips_and_reaches_the_runtime_config(self, client, tenant_admin, bot):
        selection = {
            "exhale": {"male": {"primary": "synth:exhale:male:2", "alternates": ["synth:exhale:male:1"]}},
        }
        body = {"humanSpeech": {
            "latency_filler_kind": "exhale",
            "filler_audio_selection": selection,
            "latency_filler_cue_selection": {"hi": {"primary": "achha", "alternates": ["ji"]}},
        }}
        r = client.put(f"{API}/bots/{bot}/voice-settings", json=body, headers=tenant_admin)
        assert r.status_code == 200, r.text
        got = r.json()["data"]
        assert got["humanSpeech"]["latency_filler_kind"] == "exhale"
        assert got["humanSpeech"]["filler_audio_selection"] == selection
        assert got["humanSpeechSources"]["filler_audio_selection"] == "bot"
        assert got["humanSpeechEffective"]["latency_filler_kind"] == "exhale"

        # Survives a fresh read and reaches the merged runtime config.
        again = client.get(f"{API}/bots/{bot}/voice-settings", headers=tenant_admin).json()["data"]
        assert again["humanSpeech"]["filler_audio_selection"] == selection
        config = _load_config_sync(bot, False)
        assert config.human_speech["latency_filler_kind"] == "exhale"
        assert config.human_speech["filler_audio_selection"] == selection
        planner = SpeechNaturalnessPlanner(config.human_speech)
        assert planner.latency_filler_kind == "exhale"
        assert planner.filler_selection_for("exhale", "male") == selection["exhale"]["male"]
        assert planner.filler_selection_for("exhale", "female") is None
        assert planner.cue_selection_for("hi-IN") == {"primary": "achha", "alternates": ["ji"]}
        assert planner.cue_selection_for("en-IN")["primary"] == "hmm"   # whole pool allowed by default

        # The catalog reports the effective choice.
        catalog = client.get(
            f"{API}/bots/{bot}/natural-conversation/audio", headers=tenant_admin
        ).json()["data"]
        assert catalog["effective"]["latencyFillerKind"] == "exhale"
        assert catalog["effective"]["fillerAudioSelection"] == selection

        # {} clears back to inheritance.
        r = client.put(f"{API}/bots/{bot}/voice-settings", json={"humanSpeech": {}}, headers=tenant_admin)
        assert r.json()["data"]["humanSpeechEffective"]["latency_filler_kind"] == "breath"

    def test_invalid_selection_is_rejected(self, client, tenant_admin, bot):
        r = client.put(
            f"{API}/bots/{bot}/voice-settings",
            json={"humanSpeech": {"latency_filler_kind": "sigh"}}, headers=tenant_admin,
        )
        assert r.status_code == 422
        r = client.put(
            f"{API}/bots/{bot}/voice-settings",
            json={"humanSpeech": {"filler_audio_selection": {"breath": {"robot": {"primary": "x"}}}}},
            headers=tenant_admin,
        )
        assert r.status_code == 422
        assert "unknown gender" in r.text
        r = client.put(
            f"{API}/bots/{bot}/voice-settings",
            json={"humanSpeech": {"latency_filler_cue_selection": ["hmm"]}}, headers=tenant_admin,
        )
        assert r.status_code == 422
