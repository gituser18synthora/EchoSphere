"""FreeSWITCH integration.

Media: FreeSWITCH dialplans attach `mod_audio_fork` to the voice worker's
`/ws/telephony/freeswitch/{session_id}` endpoint (raw L16 @ 8 kHz both ways).

Control: a minimal asyncio Event Socket Layer (ESL) client for call control —
transfer, hangup, originate — used by human-handoff and call-control routes.
Requires a reachable FreeSWITCH event socket (FREESWITCH_HOST/PORT + password
reference); every operation fails loudly when unconfigured, never fakes success.
"""

import asyncio
import logging

from shared.config import get_settings
from shared.providers.base import ProviderError

logger = logging.getLogger(__name__)


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
