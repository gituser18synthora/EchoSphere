"""Vaani dialer gateway: ONE signed webhook + ONE media WebSocket host:port.

The gateway (`python -m voice_runtime.gateway`, port 9011) is the voice-worker
app, which mounts POST /telephony/webhook/{provider} at the ROOT path (no
/api/v1) next to /ws/telephony/{provider}/{session_id}. These tests run that
same ASGI app in-process:

- webhook: signature/replay, payload validation, number→bot routing, and the
  per-campaign `botId` selection (tenant anchored by the dialed number —
  unknown/inactive/cross-tenant bots are sanitized 404s),
- WebSocket: session validation and the connected/start/media/stop lifecycle
  with fully mocked STT/LLM/TTS providers,
- the returned WS URL honoring TELEPHONY_PUBLIC_WS_BASE,
- (live-data, skipped when absent) all four mPokket collections bots routing
  through the single webhook.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from shared.db.mysql import get_sessionmaker
from shared.models import (
    ChannelConfig,
    PhoneNumber,
    Prompt,
    PromptVersion,
    Tenant,
    VoiceBot,
    VoiceBotSetting,
)
from voice_runtime.app import app as worker_app

pytestmark = pytest.mark.integration

SECRET = "gateway-test-secret"


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setenv("TELEPHONY_WEBHOOK_SECRET", SECRET)


@pytest.fixture(scope="module")
def client():
    with TestClient(worker_app) as test_client:
        yield test_client


def signed(payload: dict) -> tuple[bytes, dict]:
    """HMAC-SHA256(`<ts>.<body>`) headers for the generic webhook scheme."""
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    signature = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": ts,
        "Content-Type": "application/json",
    }


def call_payload(number: str, **extra) -> dict:
    """Unique per call — identical bodies in the same second share a signature
    and would trip replay protection."""
    return {"To": number, "From": "+15550001111",
            "callId": f"CALL-{uuid.uuid4().hex[:12]}", **extra}


def session_in_redis(ws_url: str) -> dict | None:
    from shared.voice_sessions import load_voice_session

    session_id = ws_url.rsplit("/", 1)[-1]
    return asyncio.run(load_voice_session(session_id))


class DialerRig:
    """Two tenants, four bots and one DID — everything uniquely named and
    removed on teardown. Bot A1 is the number's assigned bot; A2 proves botId
    selection; A3 is unpublished; B1 belongs to the other tenant."""

    def __init__(self):
        suffix = uuid.uuid4().hex[:10]
        self.tenant_a = f"tn_gwtest_a_{suffix}"
        self.tenant_b = f"tn_gwtest_b_{suffix}"
        self.bot_a1 = f"bot_gwtest_a1_{suffix}"
        self.bot_a2 = f"bot_gwtest_a2_{suffix}"
        self.bot_a3 = f"bot_gwtest_a3_{suffix}"
        self.bot_b1 = f"bot_gwtest_b1_{suffix}"
        self.number = f"+1999{int(suffix[:7], 16) % 10_000_000:07d}"
        self.channel_id = f"ch_gwtest_{suffix}"

    def create(self):
        session = get_sessionmaker()()
        try:
            session.add(Tenant(id=self.tenant_a, name=f"GW Test A {self.tenant_a[-4:]}",
                               domain=f"{self.tenant_a}.example.test", status="active"))
            session.add(Tenant(id=self.tenant_b, name=f"GW Test B {self.tenant_b[-4:]}",
                               domain=f"{self.tenant_b}.example.test", status="active"))
            session.flush()
            for bot_id, tenant, status in (
                (self.bot_a1, self.tenant_a, "published"),
                (self.bot_a2, self.tenant_a, "published"),
                (self.bot_a3, self.tenant_a, "draft"),
                (self.bot_b1, self.tenant_b, "published"),
            ):
                session.add(VoiceBot(id=bot_id, tenant_id=tenant,
                                     name=f"GW Test {bot_id[-6:]}", status=status,
                                     live_version="v0.1.0" if status == "published" else None))
            session.flush()
            for bot_id in (self.bot_a1, self.bot_a2):
                session.add(VoiceBotSetting(
                    id=f"vbs_{bot_id[4:]}", bot_id=bot_id, tenant_id=self.tenant_a,
                    stt_provider="mock", tts_provider="mock", llm_provider="mock",
                    language_voice_map={"default": "en-US"},
                ))
            prompt_id = f"pr_{self.bot_a1[4:]}"
            session.add(Prompt(id=prompt_id, tenant_id=self.tenant_a, bot_id=self.bot_a1,
                               type="greeting", name="Greeting", state="published"))
            session.flush()
            session.add(PromptVersion(
                id=f"pv_{self.bot_a1[4:]}", prompt_id=prompt_id, version=1,
                variants=[{"language": "en-US",
                           "content": "Hello from the gateway test bot."}],
            ))
            session.add(PhoneNumber(id=f"pn_{self.bot_a1[4:]}", number=self.number,
                                    tenant_id=self.tenant_a, bot_id=self.bot_a1,
                                    provider="vaani", status="assigned"))
            session.commit()
        finally:
            session.close()
        return self

    def disable_voice_channel(self, bot_id: str):
        session = get_sessionmaker()()
        try:
            session.add(ChannelConfig(id=self.channel_id, tenant_id=self.tenant_a,
                                      bot_id=bot_id, type="voice", enabled=False))
            session.commit()
        finally:
            session.close()

    def cleanup(self):
        from shared.models import ConversationSession, UsageEvent, UsageRecord

        bots = [self.bot_a1, self.bot_a2, self.bot_a3, self.bot_b1]
        session = get_sessionmaker()()
        try:
            for model, column, values in (
                (PromptVersion, PromptVersion.prompt_id, [f"pr_{self.bot_a1[4:]}"]),
                (Prompt, Prompt.bot_id, [self.bot_a1]),
                (ChannelConfig, ChannelConfig.id, [self.channel_id]),
                (PhoneNumber, PhoneNumber.number, [self.number]),
                (VoiceBotSetting, VoiceBotSetting.bot_id, [self.bot_a1, self.bot_a2]),
                # Calls run by the WS lifecycle tests persist transcripts and
                # (potentially) usage rows that reference the bots.
                (ConversationSession, ConversationSession.tenant_id,
                 [self.tenant_a, self.tenant_b]),
                (UsageEvent, UsageEvent.tenant_id, [self.tenant_a, self.tenant_b]),
                # Metering writes per-bot AND tenant-level (bot_id NULL) rollups.
                (UsageRecord, UsageRecord.tenant_id, [self.tenant_a, self.tenant_b]),
                (VoiceBot, VoiceBot.id, bots),
                (Tenant, Tenant.id, [self.tenant_a, self.tenant_b]),
            ):
                for row in session.query(model).filter(column.in_(values)).all():
                    session.delete(row)
                # Models declare no relationships, so the unit of work cannot
                # order deletes by FK dependency — flush each group in order.
                session.flush()
            session.commit()
        finally:
            session.close()


@pytest.fixture(scope="module")
def rig():
    rig = DialerRig().create()
    yield rig
    rig.cleanup()


class TestWebhookSecurity:
    def test_unsigned_request_403(self, client, rig):
        response = client.post("/telephony/webhook/vaani",
                               json=call_payload(rig.number))
        assert response.status_code == 403

    def test_tampered_signature_403(self, client, rig):
        body, headers = signed(call_payload(rig.number))
        headers["X-Webhook-Signature"] = "0" * 64
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 403

    def test_replayed_signature_403(self, client, rig):
        body, headers = signed(call_payload(rig.number))
        first = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert first.status_code == 200
        replay = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert replay.status_code == 403

    def test_unknown_provider_404(self, client):
        body, headers = signed(call_payload("+15550009999"))
        response = client.post("/telephony/webhook/carrierx", content=body, headers=headers)
        assert response.status_code == 404

    def test_missing_dialed_number_422(self, client):
        body, headers = signed({"callId": f"CALL-{uuid.uuid4().hex[:12]}"})
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 422


class TestWebhookRouting:
    def test_number_only_routes_to_assigned_bot(self, client, rig):
        body, headers = signed(call_payload(rig.number))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 200
        session = session_in_redis(response.json()["url"])
        assert session is not None
        assert session["bot_id"] == rig.bot_a1
        assert session["tenant_id"] == rig.tenant_a
        assert session["channel"] == "phone"

    def test_bot_id_selects_sibling_bot_on_same_number(self, client, rig):
        body, headers = signed(call_payload(rig.number, botId=rig.bot_a2))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 200
        session = session_in_redis(response.json()["url"])
        assert session["bot_id"] == rig.bot_a2
        assert session["tenant_id"] == rig.tenant_a

    def test_bot_id_snake_case_alias(self, client, rig):
        body, headers = signed(call_payload(rig.number, bot_id=rig.bot_a2))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 200
        assert session_in_redis(response.json()["url"])["bot_id"] == rig.bot_a2

    def test_cross_tenant_bot_rejected(self, client, rig):
        body, headers = signed(call_payload(rig.number, botId=rig.bot_b1))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 404  # sanitized: existence not revealed

    def test_unknown_bot_404(self, client, rig):
        body, headers = signed(call_payload(rig.number, botId="bot_does_not_exist"))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 404

    def test_unpublished_bot_404(self, client, rig):
        body, headers = signed(call_payload(rig.number, botId=rig.bot_a3))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 404

    def test_malformed_bot_id_422(self, client, rig):
        body, headers = signed(call_payload(rig.number, botId="../../etc/passwd"))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 422

    def test_unknown_number_404(self, client):
        body, headers = signed(call_payload("+19990000000"))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 404

    def test_disabled_voice_channel_403(self, client, rig):
        rig.disable_voice_channel(rig.bot_a2)
        try:
            body, headers = signed(call_payload(rig.number, botId=rig.bot_a2))
            response = client.post("/telephony/webhook/vaani", content=body,
                                   headers=headers)
            assert response.status_code == 403
        finally:
            session = get_sessionmaker()()
            try:
                row = session.get(ChannelConfig, rig.channel_id)
                if row is not None:
                    session.delete(row)
                    session.commit()
            finally:
                session.close()

    def test_ws_url_uses_configured_public_base(self, client, rig, monkeypatch):
        from shared.config import get_settings

        monkeypatch.setattr(get_settings(), "telephony_public_ws_base",
                            "ws://192.168.60.123:9011")
        body, headers = signed(call_payload(rig.number))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 200
        url = response.json()["url"]
        assert url.startswith("ws://192.168.60.123:9011/ws/telephony/vaani/vs_")

    def test_api_v1_route_serves_the_same_webhook(self, rig):
        """The historical API path keeps working (same shared handler)."""
        from backend.main import app as api_app

        with TestClient(api_app) as api_client:
            body, headers = signed(call_payload(rig.number, botId=rig.bot_a2))
            response = api_client.post("/api/v1/telephony/webhook/vaani",
                                       content=body, headers=headers)
            assert response.status_code == 200
            assert session_in_redis(response.json()["url"])["bot_id"] == rig.bot_a2


class TestMediaWebSocket:
    def test_unknown_session_rejected(self, client):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/telephony/vaani/vs_does_not_exist"):
                pass
        assert excinfo.value.code == 4401

    def test_unknown_provider_rejected(self, client):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/telephony/carrierx/vs_x"):
                pass
        assert excinfo.value.code == 4404

    def test_missing_start_handshake_rejected(self, client, rig):
        body, headers = signed(call_payload(rig.number))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        session_id = response.json()["url"].rsplit("/", 1)[-1]
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/ws/telephony/vaani/{session_id}") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(json.dumps({"event": "not-a-start"}))
                ws.send_text(json.dumps({"event": "still-not-a-start"}))
                ws.send_text(json.dumps({"event": "nope"}))
                ws.receive_text()
        assert excinfo.value.code == 4400

    def test_full_call_lifecycle_with_mocked_providers(self, client, rig):
        """webhook → session → WS connected/start → greeting media out →
        caller media in → stop → clean teardown (session removed)."""
        body, headers = signed(call_payload(
            rig.number, variables={"customer_name": "Gateway Test"}))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        assert response.status_code == 200
        ws_url = response.json()["url"]
        assert "/ws/telephony/vaani/vs_" in ws_url
        session_id = ws_url.rsplit("/", 1)[-1]

        stream_sid = f"MZ{uuid.uuid4().hex[:16]}"
        got_media = False
        outbound_stops = 0
        with client.websocket_connect(f"/ws/telephony/vaani/{session_id}") as ws:
            ws.send_text(json.dumps({"event": "connected", "protocol": "websocket"}))
            ws.send_text(json.dumps({
                "event": "start",
                "streamSid": stream_sid,
                "start": {
                    "streamSid": stream_sid,
                    "mediaFormat": {"encoding": "audio/lin",
                                    "sampleRate": 8000, "channels": 1},
                },
            }))
            # 200 ms of caller-side silence (idempotent, sequential chunks).
            silence = base64.b64encode(b"\x00" * 3200).decode()
            ws.send_text(json.dumps({
                "event": "media", "streamSid": stream_sid,
                "media": {"chunk": 1, "timestamp": str(int(time.time() * 1000)),
                          "payload": silence},
            }))
            for _ in range(100):  # greeting media must arrive (mock TTS)
                message = json.loads(ws.receive_text())
                if message.get("event") == "media":
                    payload = base64.b64decode(message["media"]["payload"])
                    assert len(payload) % 320 == 0 and payload
                    got_media = True
                    break
            assert got_media, "no greeting media arrived from the bot"
            ws.send_text(json.dumps({
                "event": "stop", "streamSid": stream_sid,
                "stop": {"reason": "callended"},
            }))
            try:
                for _ in range(500):  # drain until the worker closes the socket
                    message = json.loads(ws.receive_text())
                    if message.get("event") == "stop":
                        outbound_stops += 1
            except WebSocketDisconnect:
                pass
        assert outbound_stops <= 1  # never more than one outbound stop
        assert session_in_redis(ws_url) is None  # single-use session removed

    def test_duplicate_connection_rejected_4409(self, client, rig):
        body, headers = signed(call_payload(rig.number))
        response = client.post("/telephony/webhook/vaani", content=body, headers=headers)
        session_id = response.json()["url"].rsplit("/", 1)[-1]
        stream_sid = f"MZ{uuid.uuid4().hex[:16]}"
        with client.websocket_connect(f"/ws/telephony/vaani/{session_id}") as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(json.dumps({
                "event": "start", "streamSid": stream_sid,
                "start": {"streamSid": stream_sid,
                          "mediaFormat": {"encoding": "audio/lin",
                                          "sampleRate": 8000, "channels": 1}},
            }))
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect(f"/ws/telephony/vaani/{session_id}") as dup:
                    dup.receive_text()
            assert excinfo.value.code == 4409


MPOKKET_TENANT = "tn_22a809aecf66"
MPOKKET_NUMBER = "+91 80 4522 1010"
MPOKKET_BOTS = (
    "bot_c2453561ef8c",  # DPD 0-7 (Early Overdue) — the number's assigned bot
    "bot_b97b33667066",  # DPD 8-30 (Follow-Up)
    "bot_7ed9c825644f",  # DPD 30-60 (Escalation)
    "bot_39db9985b7d5",  # DPD 60-210+ (Recovery)
)


class TestMpokketLiveRouting:
    """Live-data check: all four collections bots route through the ONE
    webhook via botId over the tenant's single DID. Skips on databases
    without the mPokket seed."""

    def _present(self) -> bool:
        session = get_sessionmaker()()
        try:
            bots = session.query(VoiceBot).filter(
                VoiceBot.id.in_(MPOKKET_BOTS),
                VoiceBot.tenant_id == MPOKKET_TENANT,
                VoiceBot.is_deleted.is_(False),
                VoiceBot.status == "published",
            ).count()
            number = session.query(PhoneNumber).filter(
                PhoneNumber.number == MPOKKET_NUMBER,
                PhoneNumber.status == "assigned",
            ).count()
            return bots == len(MPOKKET_BOTS) and number == 1
        finally:
            session.close()

    def test_all_four_bots_route_through_one_webhook(self, client):
        if not self._present():
            pytest.skip("mPokket tenant/bots not present in this database")
        for bot_id in MPOKKET_BOTS:
            body, headers = signed(call_payload(MPOKKET_NUMBER, botId=bot_id))
            response = client.post("/telephony/webhook/vaani", content=body,
                                   headers=headers)
            assert response.status_code == 200, f"{bot_id}: {response.text}"
            session = session_in_redis(response.json()["url"])
            assert session["bot_id"] == bot_id
            assert session["tenant_id"] == MPOKKET_TENANT
