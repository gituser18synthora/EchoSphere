"""Quick Redis connectivity check. Run from project root: python scripts/check_redis.py"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def main():
    from config.settings import Settings
    import redis.asyncio as aioredis

    settings = Settings()
    print(f"Connecting to Redis at {settings.redis_url} ...")
    r = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        await r.ping()
        print("Redis PING: OK")
        await r.set("voicebot:health", "ok", ex=10)
        val = await r.get("voicebot:health")
        print(f"SET/GET test: {val}")
        await r.delete("voicebot:health")
        print("Redis is connected and working.")
    except Exception as e:
        print(f"Redis error: {e}")
    finally:
        await r.aclose()

if __name__ == "__main__":
    asyncio.run(main())