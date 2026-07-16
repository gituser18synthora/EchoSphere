"""MongoDB (Motor) connection for document data.

MongoDB is used only where documents genuinely fit better than rows:
- conversation_transcripts: per-session nested turn lists whose shape varies
  per turn (api calls, retrieval chunks, confidences, latencies).
- voice_events: raw voice interaction / call-event payloads from providers.

MySQL remains the source of truth for all structured entities; documents
reference them via tenant_id / bot_id / session_id.
"""

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from backend.config import get_settings

logger = logging.getLogger(__name__)

COLLECTION_TRANSCRIPTS = "conversation_transcripts"
COLLECTION_VOICE_EVENTS = "voice_events"


class Mongo:
    _client: AsyncIOMotorClient | None = None
    _db: AsyncIOMotorDatabase | None = None

    @classmethod
    async def connect(cls) -> None:
        settings = get_settings()
        cls._client = AsyncIOMotorClient(settings.mongodb_uri, serverSelectionTimeoutMS=4000)
        cls._db = cls._client[settings.mongodb_database]
        await cls._client.admin.command("ping")
        logger.info("MongoDB connected: %s", settings.mongodb_database)

    @classmethod
    async def disconnect(cls) -> None:
        if cls._client is not None:
            cls._client.close()
            cls._client = None
            cls._db = None

    @classmethod
    def db(cls) -> AsyncIOMotorDatabase:
        if cls._db is None:
            raise RuntimeError("MongoDB not connected — call Mongo.connect() at startup")
        return cls._db

    @classmethod
    def transcripts(cls):
        return cls.db()[COLLECTION_TRANSCRIPTS]

    @classmethod
    def voice_events(cls):
        return cls.db()[COLLECTION_VOICE_EVENTS]


async def create_indexes() -> None:
    """Idempotent index bootstrap — safe to run on every startup."""
    db = Mongo.db()
    t = db[COLLECTION_TRANSCRIPTS]
    await t.create_index([("session_id", 1)], unique=True)
    await t.create_index([("tenant_id", 1), ("created_at", -1)])
    await t.create_index([("tenant_id", 1), ("bot_id", 1), ("created_at", -1)])
    await t.create_index([("user_id", 1)])

    e = db[COLLECTION_VOICE_EVENTS]
    await e.create_index([("session_id", 1), ("created_at", 1)])
    await e.create_index([("tenant_id", 1), ("bot_id", 1), ("created_at", -1)])
    await e.create_index([("event_type", 1), ("created_at", -1)])
