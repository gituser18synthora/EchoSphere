"""MongoDB connection and collection accessors."""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from voicebot.config.settings import Settings

COLLECTION_VOICEBOTS = "voicebots"
COLLECTION_VOICEBOT_CONFIGS = "voicebot_configs"
COLLECTION_MODEL_PROVIDERS = "model_providers"
COLLECTION_PHONE_NUMBERS = "phone_numbers"
COLLECTION_CALLER_GRAPHS = "caller_graphs"


class MongoDB:
    """
    Singleton async MongoDB connection.
    Call MongoDB.connect() once at application startup.
    Use MongoDB.db() to get database handle anywhere.
    """

    _client: AsyncIOMotorClient | None = None
    _database: AsyncIOMotorDatabase | None = None

    @classmethod
    async def connect(cls) -> None:
        settings = Settings()
        if not (settings.mongo_uri or "").strip():
            raise RuntimeError(
                "MONGO_URI is not set. Add MONGO_URI to your .env (e.g. mongodb://localhost:27017)."
            )
        cls._client = AsyncIOMotorClient(settings.mongo_uri)
        cls._database = cls._client[settings.mongo_db_name]
        try:
            await cls._client.admin.command("ping")
        except Exception as e:
            print(
                f"[MongoDB] Ping failed for database "
                f"'{settings.mongo_db_name}': {e}"
            )
            raise
        print(f"[MongoDB] Connected to {settings.mongo_db_name} ✅")

    @classmethod
    async def disconnect(cls) -> None:
        if cls._client:
            cls._client.close()
            cls._client = None
            cls._database = None

    @classmethod
    def db(cls) -> AsyncIOMotorDatabase:
        if cls._database is None:
            raise RuntimeError(
                "MongoDB not connected. Call MongoDB.connect() first."
            )
        return cls._database

    @classmethod
    def voicebots(cls):
        return cls.db()[COLLECTION_VOICEBOTS]

    @classmethod
    def voicebot_configs(cls):
        return cls.db()[COLLECTION_VOICEBOT_CONFIGS]

    @classmethod
    def model_providers(cls):
        return cls.db()[COLLECTION_MODEL_PROVIDERS]

    @classmethod
    def phone_numbers(cls):
        return cls.db()[COLLECTION_PHONE_NUMBERS]

    @classmethod
    def caller_graphs(cls):
        return cls.db()[COLLECTION_CALLER_GRAPHS]


async def create_indexes() -> None:
    """
    Call once at application startup after MongoDB.connect().
    Creates all required indexes across all collections.
    """
    db = MongoDB.db()

    await db[COLLECTION_VOICEBOTS].create_index(
        [("voicebot_id", 1)], unique=True
    )
    await db[COLLECTION_VOICEBOTS].create_index(
        [("tenant_id", 1), ("status", 1)]
    )

    await db[COLLECTION_VOICEBOT_CONFIGS].create_index(
        [("voicebot_id", 1)], unique=True
    )
    await db[COLLECTION_VOICEBOT_CONFIGS].create_index([("tenant_id", 1)])

    await db[COLLECTION_MODEL_PROVIDERS].create_index(
        [("provider_id", 1), ("type", 1)], unique=True
    )

    await db[COLLECTION_PHONE_NUMBERS].create_index(
        [("phone_number", 1)], unique=True
    )
    await db[COLLECTION_PHONE_NUMBERS].create_index([("voicebot_id", 1)])

    await db[COLLECTION_CALLER_GRAPHS].create_index(
        [("voicebot_id", 1), ("caller_phone", 1)], unique=True
    )
    await db[COLLECTION_CALLER_GRAPHS].create_index(
        [("expires_at", 1)], expireAfterSeconds=0
    )
    await db[COLLECTION_CALLER_GRAPHS].create_index([("tenant_id", 1)])
