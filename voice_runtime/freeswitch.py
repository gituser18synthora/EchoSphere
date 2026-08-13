"""FreeSWITCH integration.

Media: FreeSWITCH dialplans attach `mod_audio_stream` to the voice worker's
`/ws/telephony/freeswitch/{session_id}` endpoint. Caller audio is raw L16 at
8 kHz; bot audio uses the module's JSON/base64 ``streamAudio`` envelope.

Control: a minimal asyncio Event Socket Layer (ESL) client for call control —
transfer, hangup, originate — used by human-handoff and call-control routes.
Requires a reachable FreeSWITCH event socket (FREESWITCH_HOST/PORT + password
reference); every operation fails loudly when unconfigured, never fakes success.

Transfer lifecycle: :class:`FreeSwitchTransferMonitor` subscribes (per
transferred call) to the events the blind-transfer flow actually produces —
``CHANNEL_BRIDGE`` / ``CHANNEL_UNBRIDGE`` / ``CHANNEL_HANGUP_COMPLETE`` plus
the two ``CUSTOM`` subclasses (``mod_audio_fork::transfer`` from the media
module, ``echosphere::transfer`` fired by ``voicebot.lua``) — and maps them
onto the call's existing event stream (SessionRecorder → ``voice_events`` /
transcript). FreeSWITCH has no ``CHANNEL_TRANSFER``/``CHANNEL_REDIRECT``
event types; a ``uuid_transfer`` keeps the caller's channel uuid, so every
event stays keyed to the uuid the webhook minted the session with.

Hangup lifecycle: :class:`FreeSwitchHangupMonitor` (one per FreeSWITCH call,
started at call start) follows the caller channel's teardown —
``CHANNEL_HANGUP`` → ``CHANNEL_HANGUP_COMPLETE`` → ``CHANNEL_DESTROY`` — so
the platform knows when AND why a call ended straight from FreeSWITCH,
independent of the media WebSocket closing. The raw ``Hangup-Cause`` (plus
the SIP hangup disposition, which says which side sent the BYE) is
normalized into the platform's disconnect vocabulary by
:func:`normalize_disconnect`. ``CHANNEL_UNBRIDGE`` is deliberately not part
of this subscription: the bot leg is a media fork, not a bridged channel, so
unbridge only occurs inside the transfer flow — where the transfer monitor
already interprets it with the context it needs.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from shared.config import get_settings
from shared.providers.base import ProviderError

logger = logging.getLogger(__name__)

# The channel uuid arrives from the signed webhook (voicebot_webhook.py sends
# FreeSWITCH's own uuid) and is embedded into ESL command lines, so only a
# strict uuid shape is ever accepted — anything else disables call control
# for that call rather than risking command injection.
_CALL_UUID = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")


def valid_call_uuid(value) -> bool:
    return bool(value) and isinstance(value, str) and bool(_CALL_UUID.match(value))


def esl_configured() -> bool:
    """Whether an ESL password is resolvable — the deployment's opt-in."""
    settings = get_settings()
    try:
        return bool(settings.resolve_secret(settings.freeswitch_password_reference))
    except Exception:  # noqa: BLE001 — a broken reference means "not configured"
        return False


def parse_event(body: str) -> dict[str, str]:
    """Parse a ``text/event-plain`` payload into a flat header dict.

    Header values are URL-encoded by FreeSWITCH; an optional event body
    (custom events may carry one) is preserved under ``_event_body``.
    """
    headers: dict[str, str] = {}
    lines = body.split("\n")
    body_start = len(lines)
    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = unquote(value.strip())
    rest = "\n".join(lines[body_start:]).strip()
    if rest:
        headers["_event_body"] = rest
    return headers


class ESLClient:
    """Minimal FreeSWITCH event-socket client (inbound mode)."""

    def __init__(self, host: str | None = None, port: int | None = None,
                 password: str | None = None, timeout: float = 5.0) -> None:
        settings = get_settings()
        self._host = host or settings.freeswitch_host
        self._port = port or settings.freeswitch_port
        self._password = password or settings.resolve_secret(
            settings.freeswitch_password_reference
        )
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _read_message(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = (await self._reader.readline()).decode().rstrip("\n")
            if not line:
                break
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip()] = value.strip()
        length = int(headers.get("Content-Length", 0))
        if length:
            headers["_body"] = (await self._reader.readexactly(length)).decode()
        return headers

    async def connect(self) -> None:
        if not self._password:
            raise ProviderError("freeswitch", "auth", "FreeSWITCH password reference not set")
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=self._timeout
            )
            greeting = await asyncio.wait_for(self._read_message(), timeout=self._timeout)
            if greeting.get("Content-Type") != "auth/request":
                raise ProviderError("freeswitch", "upstream", "Unexpected ESL greeting")
            self._writer.write(f"auth {self._password}\n\n".encode())
            await self._writer.drain()
            reply = await asyncio.wait_for(self._read_message(), timeout=self._timeout)
            if "+OK" not in reply.get("Reply-Text", ""):
                raise ProviderError("freeswitch", "auth", "ESL authentication rejected")
        except (OSError, TimeoutError) as exc:
            raise ProviderError("freeswitch", "timeout", f"ESL connect failed: {exc}") from exc

    async def api(self, command: str) -> str:
        async with self._lock:
            if self._writer is None:
                await self.connect()
            self._writer.write(f"api {command}\n\n".encode())
            await self._writer.drain()
            reply = await asyncio.wait_for(self._read_message(), timeout=self._timeout)
            return reply.get("_body", "")

    async def command(self, command: str) -> dict[str, str]:
        """Non-api command (``event``/``filter``) — raises unless FS replies +OK.

        Only safe BEFORE events start flowing on this connection: once an
        ``event`` subscription is active, interleaved events would be read as
        the reply. Subscribe with filters first, ``event`` last.
        """
        async with self._lock:
            if self._writer is None:
                await self.connect()
            self._writer.write(f"{command}\n\n".encode())
            await self._writer.drain()
            reply = await asyncio.wait_for(self._read_message(), timeout=self._timeout)
            if "+OK" not in reply.get("Reply-Text", ""):
                raise ProviderError(
                    "freeswitch", "upstream",
                    f"command rejected: {command.split(' ', 1)[0]}",
                )
            return reply

    async def next_event(self, timeout: float) -> dict[str, str] | None:
        """Next subscribed event as a flat header dict; None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = await asyncio.wait_for(
                    self._read_message(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return None
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise ProviderError(
                    "freeswitch", "upstream", f"event socket closed: {exc}"
                ) from exc
            content_type = message.get("Content-Type", "")
            if content_type == "text/event-plain":
                return parse_event(message.get("_body", ""))
            if content_type == "text/disconnect-notice":
                raise ProviderError(
                    "freeswitch", "upstream", "event socket disconnected"
                )
            # Command replies/heartbeats interleaved with events: skip.

    async def transfer(self, call_uuid: str, destination: str) -> None:
        result = await self.api(f"uuid_transfer {call_uuid} {destination}")
        if "+OK" not in result:
            raise ProviderError("freeswitch", "upstream", f"transfer failed: {result[:120]}")

    async def hangup(self, call_uuid: str, cause: str = "NORMAL_CLEARING") -> None:
        result = await self.api(f"uuid_kill {call_uuid} {cause}")
        if "+OK" not in result:
            raise ProviderError("freeswitch", "upstream", f"hangup failed: {result[:120]}")

    async def health_check(self) -> dict:
        try:
            status = await self.api("status")
            return {"ok": "UP" in status, "detail": status.splitlines()[0] if status else ""}
        except ProviderError as exc:
            return {"ok": False, "error": exc.category}

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._reader = None


async def _write_session_event(session_id: str, recorder, kind: str, data: dict) -> None:
    """One lifecycle event onto the call's existing event stream.

    Written through the SessionRecorder while the session is live, and
    directly to the same Mongo stores after finalize — transfer and hangup
    confirmations regularly land after the bot session is gone.
    """
    try:
        if recorder is not None and recorder.end_reason is None:
            await recorder.flush_event(kind, **data)
            return
        from shared.db.mongo import Mongo

        now = datetime.now(timezone.utc)
        await Mongo.voice_events().insert_one({
            "session_id": session_id,
            "tenant_id": recorder.config.tenant_id if recorder else None,
            "bot_id": recorder.config.bot_id if recorder else None,
            "kind": kind,
            "at": now,
            "data": data,
        })
        await Mongo.transcripts().update_one(
            {"session_id": session_id},
            {"$push": {"events": {"kind": kind, "at": now.isoformat(), **data}}},
        )
    except Exception:  # noqa: BLE001 — recording must never kill a monitor
        logger.warning("freeswitch lifecycle event write failed (%s)", kind)


# ── transfer lifecycle ────────────────────────────────────────────────────

# Hangup causes that mean the caller (or the platform) simply ended the call
# while the agent leg was still being set up — a normal hangup, never a
# transfer failure.
_BENIGN_HANGUP_CAUSES = {
    "NORMAL_CLEARING", "ORIGINATOR_CANCEL", "BLIND_TRANSFER",
    "SYSTEM_SHUTDOWN", "MANAGER_REQUEST",
}


class TransferStateMachine:
    """Pure transfer-lifecycle tracker: FreeSWITCH events in, records out.

    States only move forward (requested → initiated → connected → terminal),
    which is what makes duplicate or replayed FreeSWITCH events idempotent:
    a transition to the current-or-earlier state is a no-op, and exact
    duplicate deliveries are additionally dropped by ``Event-Sequence``.

    The caller keeps their channel uuid across ``uuid_transfer``; the agent
    leg appears as ``Other-Leg-Unique-ID`` on the bridge event. Events for
    any other uuid are ignored — they belong to other calls.
    """

    _ORDER = {
        "requested": 0, "initiated": 1, "connected": 2,
        "completed": 3, "abandoned": 3, "failed": 3,
    }

    def __init__(self, call_uuid: str) -> None:
        self.call_uuid = (call_uuid or "").lower()
        self.state = "requested"
        self.agent_uuid: str | None = None
        self.hangup_cause: str | None = None
        self._seen_sequences: set[str] = set()

    @property
    def terminal(self) -> bool:
        return self._ORDER.get(self.state, 0) >= 3

    def _advance(self, state: str) -> bool:
        if self.terminal or self._ORDER[state] <= self._ORDER.get(self.state, 0):
            return False
        self.state = state
        return True

    def handle(self, event: dict) -> list[dict]:
        """Fold one FreeSWITCH event in; return the records it produced.

        Each record carries ``kind`` (the voice-event name) plus its data.
        An ignored/duplicate/foreign event returns ``[]``.
        """
        sequence = event.get("Event-Sequence") or ""
        if sequence:
            if sequence in self._seen_sequences:
                return []  # exact duplicate delivery of the same event
            self._seen_sequences.add(sequence)
        name = event.get("Event-Name", "")
        uuid = (event.get("Unique-ID") or "").lower()
        other = (event.get("Other-Leg-Unique-ID") or "").lower()

        if name == "CUSTOM":
            return self._handle_custom(event, uuid)
        if name == "CHANNEL_BRIDGE":
            if self.call_uuid not in (uuid, other):
                return []
            agent = other if uuid == self.call_uuid else uuid
            if not self._advance("connected"):
                return []
            self.agent_uuid = agent or None
            return [{"kind": "transfer_connected", "agent_uuid": self.agent_uuid}]
        if name == "CHANNEL_UNBRIDGE":
            if self.call_uuid not in (uuid, other) or self.state != "connected":
                return []
            # Informational: the legs separated but the caller is still up
            # (agent hangup with the caller parked, or a re-route).
            return [{"kind": "transfer_unbridged"}]
        if name == "CHANNEL_HANGUP_COMPLETE":
            return self._handle_hangup(event, uuid)
        return []

    def _handle_custom(self, event: dict, uuid: str) -> list[dict]:
        subclass = event.get("Event-Subclass", "")
        if uuid != self.call_uuid:
            return []
        if subclass == "mod_audio_fork::transfer":
            # The media module accepted EchoSphere's transfer message — the
            # dialplan script is about to act on it.
            if self._advance("initiated"):
                return [{"kind": "transfer_initiated", "source": subclass}]
            return []
        if subclass == "echosphere::transfer":
            status = (event.get("Transfer-Status") or "").lower()
            destination = event.get("Transfer-Destination") or ""
            if status == "initiated":
                if self._advance("initiated"):
                    return [{
                        "kind": "transfer_initiated", "source": subclass,
                        "destination": destination,
                    }]
                return []
            if status == "failed":
                if self._advance("failed"):
                    return [{
                        "kind": "transfer_failed",
                        "destination": destination,
                        "detail": event.get("Transfer-Detail")
                        or "dialplan transfer failed",
                    }]
                return []
        return []

    def _handle_hangup(self, event: dict, uuid: str) -> list[dict]:
        cause = event.get("Hangup-Cause") or ""
        if uuid == self.call_uuid:
            self.hangup_cause = cause
            if self.state == "connected":
                # The caller spoke to the agent; however the call ended, the
                # transfer itself succeeded — never a failure.
                if self._advance("completed"):
                    return [{"kind": "transfer_completed", "hangup_cause": cause}]
                return []
            if cause in _BENIGN_HANGUP_CAUSES:
                # Caller (or platform) hung up while the agent leg was still
                # being set up: a normal hangup during transfer, not a failure.
                if self._advance("abandoned"):
                    return [{"kind": "transfer_abandoned", "hangup_cause": cause}]
                return []
            if self._advance("failed"):
                return [{"kind": "transfer_failed", "hangup_cause": cause}]
            return []
        if uuid and uuid == self.agent_uuid and not self.terminal:
            # The agent leg ended while the caller is still up (agent hangup,
            # queue re-route). Informational — the caller leg decides the
            # terminal state.
            return [{"kind": "transfer_agent_leg_ended", "hangup_cause": cause}]
        return []


class FreeSwitchTransferMonitor:
    """Tracks one transferred call over a dedicated ESL connection.

    Started when EchoSphere puts the transfer message on the media socket.
    Writes lifecycle records through the call's SessionRecorder while the
    session is live, and directly to the same Mongo stores after finalize —
    transfer connect/complete usually lands after the bot session is gone.
    """

    # Everything the blind-transfer flow can produce, and nothing more.
    EVENTS = ("CHANNEL_BRIDGE CHANNEL_UNBRIDGE CHANNEL_HANGUP_COMPLETE "
              "CUSTOM mod_audio_fork::transfer echosphere::transfer")

    def __init__(self, *, session_id: str, call_uuid: str, recorder) -> None:
        settings = get_settings()
        self.session_id = session_id
        self.machine = TransferStateMachine(call_uuid)
        self._recorder = recorder
        self._ring_seconds = float(settings.freeswitch_transfer_ring_seconds)
        self._max_seconds = float(settings.freeswitch_transfer_monitor_max_seconds)
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        client = ESLClient()
        try:
            try:
                await client.connect()
                # Filters first (nothing can match yet), subscription last —
                # replies stay unambiguous. Both filters are needed: the
                # caller keeps their uuid; bridge/hangup events for the agent
                # leg reference it as the other leg.
                await client.command(f"filter Unique-ID {self.machine.call_uuid}")
                await client.command(
                    f"filter Other-Leg-Unique-ID {self.machine.call_uuid}"
                )
                await client.command(f"event plain {self.EVENTS}")
            except ProviderError as exc:
                logger.warning(
                    "transfer monitor unavailable for %s: %s", self.session_id, exc
                )
                await self._record("transfer_monitor_unavailable", reason=str(exc))
                return
            await self._record(
                "transfer_monitor_started", call_uuid=self.machine.call_uuid
            )
            await self._watch(client)
        except Exception:  # noqa: BLE001 — the monitor must never crash the worker
            logger.exception("transfer monitor crashed for %s", self.session_id)
        finally:
            await client.close()

    async def _watch(self, client: ESLClient) -> None:
        started = time.monotonic()
        ring_expired_noted = False
        while not self.machine.terminal:
            elapsed = time.monotonic() - started
            if elapsed >= self._max_seconds:
                await self._record(
                    "transfer_monitor_timeout", state=self.machine.state
                )
                return
            try:
                event = await client.next_event(
                    timeout=min(30.0, self._max_seconds - elapsed)
                )
            except ProviderError as exc:
                await self._record(
                    "transfer_monitor_disconnected",
                    state=self.machine.state, detail=str(exc),
                )
                return
            if (
                not ring_expired_noted
                and self.machine.state in ("requested", "initiated")
                and time.monotonic() - started >= self._ring_seconds
            ):
                # No agent bridge and no hangup yet: flag it (queue hold can
                # legitimately take longer, so keep watching).
                ring_expired_noted = True
                await self._record(
                    "transfer_unconfirmed",
                    state=self.machine.state,
                    waited_s=round(time.monotonic() - started),
                )
            if event is None:
                continue
            records = self.machine.handle(event)
            for record in records:
                data = dict(record)
                kind = data.pop("kind")
                await self._record(kind, state=self.machine.state, **data)
            if records:
                await self._store_summary()

    async def _record(self, kind: str, **data) -> None:
        """One transfer event onto the call's existing event stream."""
        await _write_session_event(self.session_id, self._recorder, kind, data)

    async def _store_summary(self) -> None:
        """Current transfer state as a queryable field on the transcript."""
        try:
            from shared.db.mongo import Mongo

            await Mongo.transcripts().update_one(
                {"session_id": self.session_id},
                {"$set": {"transfer": {
                    "state": self.machine.state,
                    "call_uuid": self.machine.call_uuid,
                    "agent_uuid": self.machine.agent_uuid,
                    "hangup_cause": self.machine.hangup_cause,
                    "updated_at": datetime.now(timezone.utc),
                }}},
            )
        except Exception:  # noqa: BLE001
            logger.warning("transfer summary write failed for %s", self.session_id)


_active_monitors: dict[str, FreeSwitchTransferMonitor] = {}


def start_transfer_monitor(
    *, session_id: str, call_uuid: str | None, recorder
) -> FreeSwitchTransferMonitor | None:
    """Start (once per session) the ESL transfer monitor. Fail-open:
    a missing uuid or unconfigured event socket records why and returns None —
    the transfer itself still proceeds through the media module."""
    existing = _active_monitors.get(session_id)
    if existing is not None:
        return existing  # duplicate transfer controls never fork a second monitor
    if not valid_call_uuid(call_uuid):
        logger.warning(
            "transfer monitor skipped for %s: no usable FreeSWITCH call uuid",
            session_id,
        )
        recorder.flush_event_soon(
            "transfer_monitor_unavailable", reason="missing or invalid call uuid"
        )
        return None
    if not esl_configured():
        recorder.flush_event_soon(
            "transfer_monitor_unavailable", reason="event socket not configured"
        )
        return None
    monitor = FreeSwitchTransferMonitor(
        session_id=session_id, call_uuid=call_uuid, recorder=recorder
    )
    _active_monitors[session_id] = monitor
    monitor.task = asyncio.create_task(monitor.run())
    monitor.task.add_done_callback(
        lambda _task: _active_monitors.pop(session_id, None)
    )
    return monitor


# ── hangup lifecycle ──────────────────────────────────────────────────────

# uuid_transfer/pickup teardown causes: the leg ended because the call moved,
# not because anyone failed or hung up.
_TRANSFER_HANGUP_CAUSES = {"BLIND_TRANSFER", "ATTENDED_TRANSFER", "PICKED_OFF"}
# The media path died or never matched, with signaling still up.
_MEDIA_FAILURE_CAUSES = {
    "MEDIA_TIMEOUT", "INCOMPATIBLE_DESTINATION",
    "BEARERCAPABILITY_NOTIMPL", "BEARERCAPABILITY_NOTAVAIL",
}
# Orderly clearing — which SIDE cleared is decided by the SIP disposition.
_NORMAL_HANGUP_CAUSES = {"NORMAL_CLEARING", "ORIGINATOR_CANCEL"}
# Platform/operator call control (uuid_kill with an admin cause, shutdown).
_APP_HANGUP_CAUSES = {"MANAGER_REQUEST", "SYSTEM_SHUTDOWN"}


def normalize_disconnect(
    cause: str | None,
    *,
    sip_disposition: str | None = "",
    transferred: bool = False,
    bot_ended: bool = False,
) -> str:
    """Map a FreeSWITCH hangup onto the platform's disconnect vocabulary.

    Returns ``caller_hangup`` / ``app_hangup`` / ``provider_failure`` /
    ``media_failure`` / ``transferred`` / ``unknown``. ``sip_disposition``
    is FreeSWITCH's ``sip_hangup_disposition`` variable (``recv_bye`` = the
    far end hung up, ``send_bye`` = this side did); ``bot_ended`` is the
    fallback signal when SIP did not say (the session had already finalized,
    so the platform's own ``uuid_kill`` is what cleared the channel).
    """
    cause = (cause or "").strip().upper()
    disposition = (sip_disposition or "").strip().lower()
    if transferred or cause in _TRANSFER_HANGUP_CAUSES:
        return "transferred"
    if cause in _MEDIA_FAILURE_CAUSES:
        return "media_failure"
    if cause in _APP_HANGUP_CAUSES:
        return "app_hangup"
    if cause in _NORMAL_HANGUP_CAUSES:
        if disposition.startswith("recv"):
            return "caller_hangup"
        if disposition.startswith("send"):
            return "app_hangup"
        return "app_hangup" if bot_ended else "caller_hangup"
    if not cause:
        return "unknown"
    # Everything else in the Q.850 space is a teardown nobody chose: SIP/
    # provider/FreeSWITCH failures (congestion, timer expiry, rejects, …).
    # NORMAL_TEMPORARY_FAILURE lands here on purpose — it is exactly what
    # voicebot.lua sends when the webhook/media/transfer path broke.
    return "provider_failure"


class HangupStateMachine:
    """Pure hangup-lifecycle tracker: FreeSWITCH events in, records out.

    FreeSWITCH tears a channel down as ``CHANNEL_HANGUP`` (hangup started,
    cause decided) → ``CHANNEL_HANGUP_COMPLETE`` (final variables) →
    ``CHANNEL_DESTROY`` (channel object gone). States only move forward, so
    a replayed or duplicated delivery of an already-seen stage is a no-op —
    that is what makes downstream finalization single-shot; exact duplicate
    deliveries are additionally dropped by ``Event-Sequence``. Any of the
    three events is accepted as the first sign of the hangup, so a monitor
    that missed an earlier stage still terminates correctly. Events for any
    other uuid belong to other calls and are ignored.
    """

    _ORDER = {"up": 0, "hung_up": 1, "hangup_complete": 2, "destroyed": 3}
    _EVENT_STATES = {
        "CHANNEL_HANGUP": ("hung_up", "channel_hangup"),
        "CHANNEL_HANGUP_COMPLETE": ("hangup_complete", "channel_hangup_complete"),
        "CHANNEL_DESTROY": ("destroyed", "channel_destroyed"),
    }

    def __init__(self, call_uuid: str) -> None:
        self.call_uuid = (call_uuid or "").lower()
        self.state = "up"
        # First-seen values win: CHANNEL_HANGUP carries the authoritative
        # cause; later stages only fill gaps, never rewrite the verdict.
        self.cause: str | None = None
        self.sip_disposition: str | None = None
        self.other_leg_uuid: str | None = None
        self._seen_sequences: set[str] = set()

    @property
    def ended(self) -> bool:
        """A real hangup has been seen for the caller's channel."""
        return self.state != "up"

    @property
    def destroyed(self) -> bool:
        return self.state == "destroyed"

    def handle(self, event: dict) -> list[dict]:
        """Fold one FreeSWITCH event in; return the records it produced."""
        sequence = event.get("Event-Sequence") or ""
        if sequence:
            if sequence in self._seen_sequences:
                return []  # exact duplicate delivery of the same event
            self._seen_sequences.add(sequence)
        mapping = self._EVENT_STATES.get(event.get("Event-Name", ""))
        if mapping is None:
            return []
        uuid = (event.get("Unique-ID") or "").lower()
        if uuid != self.call_uuid:
            return []
        target, kind = mapping
        if self._ORDER[target] <= self._ORDER[self.state]:
            return []  # replayed/out-of-order delivery of a seen stage
        self.state = target
        cause = (
            event.get("Hangup-Cause")
            or event.get("variable_hangup_cause")
            or ""
        ).upper()
        disposition = (event.get("variable_sip_hangup_disposition") or "").lower()
        other_leg = (
            event.get("Other-Leg-Unique-ID")
            or event.get("variable_bridge_uuid")
            or event.get("variable_last_bridge_to")
            or ""
        ).lower()
        if cause and not self.cause:
            self.cause = cause
        if disposition and not self.sip_disposition:
            self.sip_disposition = disposition
        if other_leg and not self.other_leg_uuid:
            self.other_leg_uuid = other_leg
        record = {"kind": kind, "cause": cause or self.cause or ""}
        if disposition:
            record["sip_disposition"] = disposition
        if other_leg:
            record["other_leg_uuid"] = other_leg
        if event.get("Event-Date-Timestamp"):
            record["fs_timestamp_us"] = event["Event-Date-Timestamp"]
        return [record]


class FreeSwitchHangupMonitor:
    """Tracks one call's channel teardown over a dedicated ESL connection.

    Started at call start for every FreeSWITCH call. On the first real
    hangup event it records the raw cause plus the normalized disconnect
    reason (once — duplicates cannot re-trigger it), and invokes the
    ``on_hangup`` hook so the session host can stop STT/LLM/TTS and queued
    audio immediately instead of waiting for the media socket to die. Stands
    down as soon as the call transfers: the transfer monitor owns that
    lifecycle, and an expected original-leg hangup during a transfer must
    never read as a call failure.
    """

    EVENTS = "CHANNEL_HANGUP CHANNEL_HANGUP_COMPLETE CHANNEL_DESTROY"
    # After the final hangup is recorded, how long to keep the connection
    # open for the complete/destroy confirmations before closing quietly.
    FINAL_EVENTS_GRACE_S = 15.0
    # After the SESSION finalized with no hangup seen (bot-ended call), how
    # long to keep waiting for the uuid_kill teardown events.
    POST_SESSION_LINGER_S = 60.0

    def __init__(
        self, *, session_id: str, call_uuid: str, recorder, on_hangup=None
    ) -> None:
        settings = get_settings()
        self.session_id = session_id
        self.machine = HangupStateMachine(call_uuid)
        self._recorder = recorder
        self._on_hangup = on_hangup
        self._max_seconds = float(settings.freeswitch_hangup_monitor_max_seconds)
        self._final_signaled = False
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        client = ESLClient()
        try:
            try:
                await client.connect()
                # Only the caller's leg: it keeps its uuid for the whole
                # call (uuid_transfer included). Agent-leg hangups are the
                # transfer monitor's business.
                await client.command(f"filter Unique-ID {self.machine.call_uuid}")
                await client.command(f"event plain {self.EVENTS}")
            except ProviderError as exc:
                logger.warning(
                    "hangup monitor unavailable for %s: %s", self.session_id, exc
                )
                await self._record("hangup_monitor_unavailable", reason=str(exc))
                return
            await self._record(
                "hangup_monitor_started", call_uuid=self.machine.call_uuid
            )
            await self._watch(client)
        except Exception:  # noqa: BLE001 — the monitor must never crash the worker
            logger.exception("hangup monitor crashed for %s", self.session_id)
        finally:
            await client.close()

    async def _watch(self, client: ESLClient) -> None:
        started = time.monotonic()
        ended_at: float | None = None
        finalized_at: float | None = None
        while True:
            now = time.monotonic()
            if self.machine.destroyed:
                return
            if ended_at is not None and now - ended_at >= self.FINAL_EVENTS_GRACE_S:
                return  # hangup recorded; complete/destroy never showed up
            if now - started >= self._max_seconds:
                await self._record("hangup_monitor_timeout", state=self.machine.state)
                return
            recorder = self._recorder
            if not self.machine.ended and recorder is not None:
                if recorder.transferred:
                    # The caller now belongs to the transfer lifecycle; its
                    # monitor interprets every later hangup with the context
                    # this one lacks (bridge state, agent leg).
                    await self._record(
                        "hangup_monitor_stood_down", reason="transferred"
                    )
                    return
                if recorder.end_reason is not None:
                    if finalized_at is None:
                        finalized_at = now
                    elif now - finalized_at >= self.POST_SESSION_LINGER_S:
                        # Bot-ended session but FreeSWITCH never reported the
                        # channel down (already gone before we subscribed, or
                        # uuid_kill unavailable). Nothing left to learn.
                        await self._record(
                            "hangup_monitor_timeout", state=self.machine.state
                        )
                        return
            timeout = min(5.0, self._max_seconds - (now - started))
            if ended_at is not None:
                timeout = min(timeout, self.FINAL_EVENTS_GRACE_S - (now - ended_at))
            if finalized_at is not None and not self.machine.ended:
                timeout = min(
                    timeout, self.POST_SESSION_LINGER_S - (now - finalized_at)
                )
            try:
                event = await client.next_event(timeout=max(0.05, timeout))
            except ProviderError as exc:
                await self._record(
                    "hangup_monitor_disconnected",
                    state=self.machine.state, detail=str(exc),
                )
                return
            if event is None:
                continue
            was_ended = self.machine.ended
            records = self.machine.handle(event)
            for record in records:
                data = dict(record)
                kind = data.pop("kind")
                await self._record(kind, **data)
            if records and not was_ended and self.machine.ended:
                ended_at = time.monotonic()
                await self._finalize_hangup()

    async def _finalize_hangup(self) -> None:
        """The one-shot verdict for this call's disconnect.

        Guarded so replayed FreeSWITCH deliveries can never re-run it: the
        end-call hook, the recorder's hangup record and the transcript
        summary all happen at most once per call.
        """
        if self._final_signaled:
            return
        self._final_signaled = True
        machine = self.machine
        recorder = self._recorder
        transferred = bool(getattr(recorder, "transferred", False))
        bot_ended = recorder is not None and recorder.end_reason is not None
        reason = normalize_disconnect(
            machine.cause,
            sip_disposition=machine.sip_disposition,
            transferred=transferred,
            bot_ended=bot_ended,
        )
        info = {
            "cause": machine.cause or "",
            "reason": reason,
            "sip_disposition": machine.sip_disposition or "",
            "call_uuid": machine.call_uuid,
            "other_leg_uuid": machine.other_leg_uuid,
            "transfer_related": transferred,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "freeswitch hangup: session=%s uuid=%s cause=%s sip_disposition=%s "
            "normalized=%s transfer_related=%s other_leg=%s",
            self.session_id, machine.call_uuid, machine.cause or "",
            machine.sip_disposition or "", reason, transferred,
            machine.other_leg_uuid or "",
        )
        if recorder is not None:
            recorder.set_hangup(info)
        # End the pipeline BEFORE the bookkeeping writes: no STT/LLM/TTS or
        # queued audio may outlive a dead channel. Not on transfers — there
        # the fork stop already tears the media path down, and this hangup
        # is the transfer ending, not the bot call failing.
        if self._on_hangup is not None and not transferred:
            try:
                await self._on_hangup(info)
            except Exception:  # noqa: BLE001 — teardown hook must not kill the monitor
                logger.exception("hangup end-call hook failed for %s", self.session_id)
        await self._record("freeswitch_hangup", **info)
        await self._store_hangup_summary(info)

    async def _record(self, kind: str, **data) -> None:
        """One hangup event onto the call's existing event stream."""
        await _write_session_event(self.session_id, self._recorder, kind, data)

    async def _store_hangup_summary(self, info: dict) -> None:
        """The disconnect verdict as a queryable field on the transcript.

        No upsert: pre-finalize the document may not exist yet, and then the
        recorder's own finalize persists the same info from ``recorder.hangup``.
        """
        try:
            from shared.db.mongo import Mongo

            await Mongo.transcripts().update_one(
                {"session_id": self.session_id},
                {"$set": {"hangup": info}},
            )
        except Exception:  # noqa: BLE001
            logger.warning("hangup summary write failed for %s", self.session_id)


_active_hangup_monitors: dict[str, FreeSwitchHangupMonitor] = {}


def start_hangup_monitor(
    *, session_id: str, call_uuid: str | None, recorder, on_hangup=None
) -> FreeSwitchHangupMonitor | None:
    """Start (once per session) the ESL hangup monitor. Fail-open: a missing
    uuid or unconfigured event socket records why and returns None — the call
    still ends through the media-socket disconnect path, just without the
    FreeSWITCH-side cause."""
    existing = _active_hangup_monitors.get(session_id)
    if existing is not None:
        return existing  # one channel, one hangup lifecycle
    if not valid_call_uuid(call_uuid):
        logger.warning(
            "hangup monitor skipped for %s: no usable FreeSWITCH call uuid",
            session_id,
        )
        recorder.flush_event_soon(
            "hangup_monitor_unavailable", reason="missing or invalid call uuid"
        )
        return None
    if not esl_configured():
        recorder.flush_event_soon(
            "hangup_monitor_unavailable", reason="event socket not configured"
        )
        return None
    monitor = FreeSwitchHangupMonitor(
        session_id=session_id, call_uuid=call_uuid,
        recorder=recorder, on_hangup=on_hangup,
    )
    _active_hangup_monitors[session_id] = monitor
    monitor.task = asyncio.create_task(monitor.run())
    monitor.task.add_done_callback(
        lambda _task: _active_hangup_monitors.pop(session_id, None)
    )
    return monitor


# ── bot-initiated call end ────────────────────────────────────────────────

_background_hangups: set[asyncio.Task] = set()


def hangup_channel_soon(call_uuid: str | None, *, session_id: str = "") -> None:
    """Best-effort ``uuid_kill`` when the BOT ended the call.

    The dialplan script cannot see the media WebSocket close, so without this
    the PSTN leg keeps playing silence until the caller gives up. Never blocks
    teardown; a channel that is already gone (caller hung up first) is the
    normal case and stays silent. No-op when ESL is not configured — the
    Lua-side ``mod_audio_fork::disconnect`` handling is the fallback there.
    """
    if not valid_call_uuid(call_uuid) or not esl_configured():
        return

    async def _kill() -> None:
        client = ESLClient()
        try:
            result = await client.api(f"uuid_kill {call_uuid} NORMAL_CLEARING")
            if "+OK" not in result and "No such channel" not in result:
                logger.info(
                    "freeswitch hangup for %s returned: %s",
                    session_id, result[:120],
                )
        except ProviderError as exc:
            logger.debug("freeswitch hangup skipped for %s: %s", session_id, exc)
        finally:
            await client.close()

    task = asyncio.create_task(_kill())
    _background_hangups.add(task)
    task.add_done_callback(_background_hangups.discard)
