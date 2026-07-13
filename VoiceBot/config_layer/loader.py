"""Config loader: cache-first load from MongoDB, resolve by voicebot_id or phone number."""

from datetime import datetime, timezone

import pytz
#used for timezone handling in Python
from pydantic import ValidationError

from .cache import ConfigCache
from .db import MongoDB
from .exceptions import (
    ConfigValidationError as ConfigValidationErrorExc,
    NoVoicebotForNumberError,
    OutsideWorkingHoursError,
    ProviderNotFoundError,
    VoicebotNotFoundError,
    VoicebotNotActiveError,
)
from .mongo_normalize import normalize_voicebot_config_document
from .models import (
    AvailabilityConfig,
    ModelProvider,
    VoicebotConfig,
)

PROVIDER_CACHE_TTL = 3600
PROVIDER_KEY_PREFIX = "provider:"


class ConfigLoader:
    """
    Loads complete voicebot config.
    Cache-first: Redis -> MongoDB fallback.
    Returns validated VoicebotConfig Pydantic model.
    """

    def __init__(self):
        self._cache = ConfigCache()

    async def load(self, voicebot_id: str) -> VoicebotConfig:
        """
        Step 1: Check Redis cache -> Hit: deserialize -> return
        Step 2: Query voicebot_configs find_one by voicebot_id
        Step 3: Parse + validate with Pydantic; on error raise ConfigValidationError
        Step 4: Write to Redis cache
        Step 5: Return VoicebotConfig
        """
        data = await self._cache.get(voicebot_id)
        if data is not None:
            return VoicebotConfig.from_cache_dict(data)

        doc = await MongoDB.voicebot_configs().find_one(
            {"voicebot_id": voicebot_id}
        )
        if doc is None:
            raise VoicebotNotFoundError(voicebot_id)

        doc_clean = {k: v for k, v in doc.items() if k != "_id"}
        doc_clean = normalize_voicebot_config_document(doc_clean)
        try:
            config = VoicebotConfig.model_validate(doc_clean)
        except ValidationError as e:
            errors = [
                {"field": err.get("loc", ()), "msg": err.get("msg", "")}
                for err in e.errors()
            ]
            raise ConfigValidationErrorExc(errors) from e

        await self._cache.set(voicebot_id, config.to_cache_dict())
        return config

    async def load_for_incoming_call(self, phone_number: str) -> VoicebotConfig:
        """
        Entry point when FreeSWITCH receives inbound call.
        Resolves phone number -> voicebot_id -> config.
        """
        phone_doc = await MongoDB.phone_numbers().find_one(
            {"phone_number": phone_number, "status": "active"}
        )
        if phone_doc is None:
            raise NoVoicebotForNumberError(phone_number)

        voicebot_id = phone_doc["voicebot_id"]

        voicebot_doc = await MongoDB.voicebots().find_one(
            {"voicebot_id": voicebot_id}
        )
        if voicebot_doc is None:
            raise VoicebotNotActiveError(voicebot_id, "missing")
        status = voicebot_doc.get("status", "")
        if status != "active":
            raise VoicebotNotActiveError(voicebot_id, str(status))

        config = await self.load(voicebot_id)

        if not await self._check_availability(config.availability):
            raise OutsideWorkingHoursError(
                config.name, config.availability.timezone
            )

        return config

    async def refresh(self, voicebot_id: str) -> VoicebotConfig:
        """Force bypass cache. Reload fresh from MongoDB."""
        await self._cache.invalidate(voicebot_id)
        return await self.load(voicebot_id)

    async def load_model_provider(
        self,
        provider_id: str,
        provider_type: str,
    ) -> ModelProvider:
        """
        Load single provider from model_providers collection.
        Raise ProviderNotFoundError if not found or is_active=False.
        """
        doc = await MongoDB.model_providers().find_one(
            {"provider_id": provider_id, "type": provider_type}
        )
        if doc is None:
            raise ProviderNotFoundError(provider_id, provider_type)
        if not doc.get("is_active", True):
            raise ProviderNotFoundError(provider_id, provider_type)

        doc_clean = {k: v for k, v in doc.items() if k != "_id"}
        return ModelProvider.model_validate(doc_clean)

    async def _check_availability(
        self,
        availability: AvailabilityConfig,
    ) -> bool:
        """
        Check if current UTC time falls within working hours.
        If enable_24x7 = True: always return True.
        """
        if availability.enable_24x7:
            return True

        tz = pytz.timezone(availability.timezone)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz).time()

        def parse_hhmm(s: str):
            parts = s.strip().split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            from datetime import time
            return time(h, m)

        start = parse_hhmm(availability.working_hours_start)
        end = parse_hhmm(availability.working_hours_end)

        if start <= end:
            return start <= now_local <= end
        return now_local >= start or now_local <= end
