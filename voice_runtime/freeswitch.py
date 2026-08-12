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
        recorder = self._recorder
        try:
            if recorder is not None and recorder.end_reason is None:
                await recorder.flush_event(kind, **data)
                return
            # Session already finalized: write to the same stores directly.
            from shared.db.mongo import Mongo

            now = datetime.now(timezone.utc)
            await Mongo.voice_events().insert_one({
                "session_id": self.session_id,
                "tenant_id": recorder.config.tenant_id if recorder else None,
                "bot_id": recorder.config.bot_id if recorder else None,
                "kind": kind,
                "at": now,
                "data": data,
            })
            await Mongo.transcripts().update_one(
                {"session_id": self.session_id},
                {"$push": {"events": {"kind": kind, "at": now.isoformat(), **data}}},
            )
        except Exception:  # noqa: BLE001 — recording must never kill the monitor
            logger.warning("transfer event write failed (%s)", kind)

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
