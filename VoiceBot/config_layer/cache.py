"""Redis cache for voicebot config."""

import json

import redis.asyncio as aioredis

from voicebot.config.settings import Settings

CONFIG_CACHE_TTL = 300  # 5 minutes in seconds
CONFIG_KEY_PREFIX = "config:"
INVALIDATION_CHANNEL = "config_invalidation"


class ConfigCache:
    """
    Redis cache for voicebot config objects.

    Key format: config:{voicebot_id}
    TTL: 5 minutes
    Value: JSON string of VoicebotConfig.to_cache_dict()

    Invalidation flow:
      Admin saves config in UI
      -> Node.js API calls publish_invalidation(voicebot_id)
      -> Python AI service subscribe_invalidation() receives message
      -> Deletes config:{voicebot_id} from Redis
      -> Next call does cache miss -> fresh load from MongoDB
    """

    def __init__(self):
        settings = Settings()
        self._redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )

    def _key(self, voicebot_id: str) -> str:
        return f"{CONFIG_KEY_PREFIX}{voicebot_id}"

    async def get(self, voicebot_id: str) -> dict | None:
        raw = await self._redis.get(self._key(voicebot_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, voicebot_id: str, config_dict: dict) -> None:
        await self._redis.setex(
            self._key(voicebot_id),
            CONFIG_CACHE_TTL,
            json.dumps(config_dict),
        )

    async def invalidate(self, voicebot_id: str) -> None:
        await self._redis.delete(self._key(voicebot_id))

    async def publish_invalidation(self, voicebot_id: str) -> None:
        """
        Called by Node.js API (via Redis publish) after config save.
        Python service subscriber receives this and deletes cache.
        """
        await self._redis.publish(INVALIDATION_CHANNEL, voicebot_id)

    async def subscribe_invalidation(self):
        """
        Long-running background task.
        Start this at AI service startup with asyncio.create_task().
        Listens for invalidation events and deletes stale cache keys.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(INVALIDATION_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] == "message":
                voicebot_id = message["data"]
                await self.invalidate(voicebot_id)
