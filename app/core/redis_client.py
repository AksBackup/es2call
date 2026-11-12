"""
Redis-backed shared store — used for license status caching, login rate
limiting, and anything else that needs to be consistent across multiple
uvicorn worker processes / multiple container replicas.

REPLACES the previous in-memory-dict implementation. That version only
worked correctly with a single worker process; anything using >1 worker
(uvicorn --workers N, or multiple container replicas behind a load
balancer, which is exactly the multi-user/Azure deployment this is now
built for) would see each process with its own independent, inconsistent
copy of "shared" state — e.g. login rate-limit counters that don't
actually add up across workers, letting an attacker get N workers *
5 attempts instead of 5 total.

Falls back to the in-memory store automatically if REDIS_URL isn't set or
Redis is unreachable, so local single-process dev still works without
requiring a running Redis — but logs a clear warning so that fallback is
never silently relied on in production.
"""
import json
import logging
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class _InMemoryFallback:
    """Single-process fallback — same limitation as before. Only used
    when REDIS_URL is unset or Redis is unreachable at startup."""
    _store: dict = {}

    async def set(self, key: str, value: Any, expire: int = 3600):
        self._store[key] = json.dumps(value) if not isinstance(value, str) else value

    async def get(self, key: str) -> Optional[Any]:
        val = self._store.get(key)
        if val is None:
            return None
        try:
            return json.loads(val)
        except Exception:
            return val

    async def delete(self, key: str):
        self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def incr(self, key: str, expire: int = 3600) -> int:
        current = int(self._store.get(key, 0)) + 1
        self._store[key] = str(current)
        return current


class RedisClient:
    """Thin wrapper around redis.asyncio with the same method surface the
    rest of the app already expects (get/set/delete/exists), plus incr()
    for rate limiting. Connects lazily on first use so import order never
    matters and app startup never blocks on Redis being reachable yet."""

    def __init__(self):
        self._client = None
        self._fallback = _InMemoryFallback()
        self._connect_failed_logged = False

    async def _get_client(self):
        if self._client is not None:
            return self._client
        if not settings.REDIS_URL:
            if not self._connect_failed_logged:
                logger.warning(
                    "REDIS_URL not set — using single-process in-memory fallback. "
                    "Fine for local dev; DO NOT run multiple workers/replicas in "
                    "production without a real REDIS_URL, rate limiting and license "
                    "caching will be inconsistent across processes."
                )
                self._connect_failed_logged = True
            return None
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await client.ping()
            self._client = client
            logger.info("Connected to Redis")
            return self._client
        except Exception as e:
            if not self._connect_failed_logged:
                logger.warning(f"Redis unreachable ({e}) — using single-process in-memory fallback")
                self._connect_failed_logged = True
            return None

    async def aclose(self):
        """Closes and forgets the cached client so the NEXT call to
        _get_client() reconnects from scratch. Needed because this
        client is a module-level singleton, but Celery's run_async()
        (app/tasks/tasks.py) gives every single task its own brand-new
        event loop — this client's underlying connection pool is tied
        to whatever loop was running the first time it connected, so
        reusing it from a later task's different loop fails with
        "Event loop is closed", the same class of bug the SQLAlchemy
        engine had. Call this at the end of each run_async() call."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def set(self, key: str, value: Any, expire: int = 3600):
        client = await self._get_client()
        payload = json.dumps(value) if not isinstance(value, str) else value
        if client is None:
            return await self._fallback.set(key, value, expire)
        await client.set(key, payload, ex=expire)

    async def get(self, key: str) -> Optional[Any]:
        client = await self._get_client()
        if client is None:
            return await self._fallback.get(key)
        val = await client.get(key)
        if val is None:
            return None
        try:
            return json.loads(val)
        except Exception:
            return val

    async def delete(self, key: str):
        client = await self._get_client()
        if client is None:
            return await self._fallback.delete(key)
        await client.delete(key)

    async def exists(self, key: str) -> bool:
        client = await self._get_client()
        if client is None:
            return await self._fallback.exists(key)
        return bool(await client.exists(key))

    async def incr(self, key: str, expire: int = 3600) -> int:
        """Atomic increment with expiry — used for rate limiting. Sets
        the expiry only on the first increment (when the key is new) so a
        counter's window doesn't keep sliding forward on every request."""
        client = await self._get_client()
        if client is None:
            return await self._fallback.incr(key, expire)
        val = await client.incr(key)
        if val == 1:
            await client.expire(key, expire)
        return val

    async def set_with_check(self, key: str, value: Any, expire: int = 3600) -> bool:
        await self.set(key, value, expire)
        return True


redis_client = RedisClient()
