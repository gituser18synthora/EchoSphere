"""Offline tests for the Vaani dialer sandbox CLI."""

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from backend.scripts import vaani_dialer_sim as sim


def test_signature_matches_webhook_contract():
    body = b'{"To":"+918045221010","callId":"SIM-test"}'
    headers = sim.sign(body, ts=1_785_417_437, key="unit-test-secret")
    expected = hmac.new(
        b"unit-test-secret",
        b"1785417437." + body,
        hashlib.sha256,
    ).hexdigest()

    assert headers == {
        "X-Webhook-Signature": expected,
        "X-Webhook-Timestamp": "1785417437",
        "Content-Type": "application/json",
    }


def test_call_body_contains_routing_and_optional_bot():
    args = SimpleNamespace(to="+91 80 4522 1010", bot="bot_campaign_1")
    payload = json.loads(sim.call_body(args))

    assert payload["To"] == args.to
    assert payload["botId"] == args.bot
    assert payload["callId"].startswith("SIM-")
    assert payload["variables"]["customer_name"]


def test_raw_pcm_loader_accepts_even_bytes_and_rejects_invalid(tmp_path):
    valid = tmp_path / "caller.pcm"
    valid.write_bytes(b"\x01\x02" * 160)
    assert sim.load_raw_8k(str(valid)) == valid.read_bytes()

    odd = tmp_path / "odd.pcm"
    odd.write_bytes(b"\x01")
    with pytest.raises(SystemExit, match="even number of bytes"):
        sim.load_raw_8k(str(odd))

    empty = tmp_path / "empty.pcm"
    empty.write_bytes(b"")
    with pytest.raises(SystemExit, match="empty"):
        sim.load_raw_8k(str(empty))


def test_local_websocket_rewrite_and_protocol_command():
    args = SimpleNamespace(base="http://127.0.0.1:9011", no_rewrite=False)
    public = "ws://192.168.60.123:9011/ws/telephony/vaani/vs_example"

    assert sim.ws_local(args, public) == (
        "ws://127.0.0.1:9011/ws/telephony/vaani/vs_example"
    )
    assert "protocol-events" in sim.COMMANDS


def test_synthetic_audio_is_pcm16_at_requested_duration():
    pcm = sim.synthetic_speech_8k(seconds=0.2)
    assert len(pcm) == 3_200
    assert len(pcm) % 2 == 0
