"""
Live Call Tracking — WebSocket broadcaster

Architecture (fixed Aug 2026 — see BUG FIX note below):
  - One WebSocket connection per browser tab at /api/v1/live/ws?company_id=...
  - vobiz_webhook.py / vobiz_stream_pipeline.py / tasks.py call
    live_broadcaster.emit(company_id, event) at each call lifecycle point
    (call_ringing, call_answered, user_msg, ai_msg, call_end).
  - LiveBroadcaster PUBLISHES that event to a Redis pub/sub channel
    (live_call:{company_id}). Every uvicorn worker/replica that has a
    browser subscribed to that company forwards it to its WebSocket(s).

BUG FIX — "Live call tab shows nothing, no error anywhere":
  The old version fanned events out via a plain in-memory dict
  (company_id -> set of WebSocket objects) with NO Redis involved. That
  only works if the code calling emit() runs in the exact same OS
  process as the browser's open WebSocket connection.

  It didn't, on this deployment. Outbound AI calls are dispatched via
  Celery (see app/tasks/tasks.py -> _async_outbound_call, run by the
  separate `[program:worker]` process in supervisord.azure.conf) — a
  totally different process from the uvicorn `[program:web]` process
  serving /api/v1/live/ws. Importing `live_broadcaster` in the Celery
  worker creates a SECOND, independent LiveBroadcaster instance with its
  own empty `_connections` dict. Every emit() from that process checked
  `company_id not in self._connections` (always true — that dict never
  had any WS added to it) and returned immediately. No exception raised,
  nothing to log — that's correct behavior for an in-memory dict, it
  just silently couldn't reach across processes. Same failure mode would
  also hit the moment this app runs >1 uvicorn worker or >1 container
  replica, even without Celery in the picture.

  Fix: publish to Redis (already running here for Celery — see
  CELERY_BROKER_URL) instead of relying on shared process memory. Every
  process publishes to the same channel; only the process(es) actually
  holding a live WebSocket for that company need to subscribe and
  forward. The in-memory dict is kept ONLY as a same-process fallback
  for local dev without Redis running — see _get_redis() below.

Event shapes:
  { "type": "call_ringing",   "call_uuid": "...", "phone": "...", "mode": "...", "lead_name": "...", "started_at": "..." }
  { "type": "call_answered",  "call_uuid": "...", "answered_at": "..." }
  { "type": "call_no_answer", "call_uuid": "...", "reason": "...", "ended_at": "..." }
  { "type": "user_msg",       "call_uuid": "...", "text": "...", "ts": "..." }
  { "type": "ai_msg",         "call_uuid": "...", "text": "...", "ts": "..." }
  { "type": "call_end",       "call_uuid": "...", "duration_sec": 0, "ended_at": "..." }
  { "type": "ping" }
"""
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

REDIS_CHANNEL_PREFIX = "live_call:"


class LiveBroadcaster:
    def __init__(self):
        # Same-process fallback only (used when Redis is unset/unreachable).
        self._connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._redis = None
        self._redis_connect_failed_logged = False

    def _now(self) -> str:
        return datetime.utcnow().isoformat() + "Z"

    def _channel(self, company_id: str) -> str:
        return f"{REDIS_CHANNEL_PREFIX}{company_id}"

    async def _get_redis(self):
        """Deliberately a separate connection from app.core.redis_client's
        RedisClient — pub/sub sockets are stateful (blocking listen loop)
        and shouldn't share a pool with plain get/set/incr traffic."""
        if self._redis is not None:
            return self._redis
        if not settings.REDIS_URL:
            if not self._redis_connect_failed_logged:
                logger.warning(
                    "Live broadcaster: REDIS_URL not set — falling back to "
                    "same-process delivery only. Fine for local single-process "
                    "dev; WILL silently break the Live Call tab in production "
                    "if calls are dispatched from a different process (Celery "
                    "worker) than the one holding the browser's WebSocket."
                )
                self._redis_connect_failed_logged = True
            return None
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await client.ping()
            self._redis = client
            logger.info("Live broadcaster connected to Redis pub/sub")
            return self._redis
        except Exception as e:
            if not self._redis_connect_failed_logged:
                logger.warning(f"Live broadcaster: Redis unreachable ({e}) — falling back to same-process delivery only")
                self._redis_connect_failed_logged = True
            return None

    async def connect(self, company_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[company_id].add(ws)
        logger.info(f"Live WS connected | company={company_id[:8]} | total={len(self._connections[company_id])}")

    def disconnect(self, company_id: str, ws: WebSocket):
        self._connections[company_id].discard(ws)
        logger.info(f"Live WS disconnected | company={company_id[:8]} | remaining={len(self._connections[company_id])}")

    async def emit(self, company_id: str, event: dict):
        """Publish to Redis (reaches every process/replica that's
        subscribed). Falls back to direct same-process delivery only if
        Redis is unavailable — NOT in addition to it, to avoid double
        delivery to a WS that's also subscribed via Redis."""
        if not company_id:
            return
        payload = json.dumps(event)

        redis = await self._get_redis()
        if redis is not None:
            try:
                await redis.publish(self._channel(company_id), payload)
                return
            except Exception as e:
                logger.warning(f"Live broadcaster: Redis publish failed ({e}) — falling back to same-process delivery")

        if company_id in self._connections:
            dead: List[WebSocket] = []
            for ws in list(self._connections[company_id]):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections[company_id].discard(ws)

    # ── Convenience emit helpers called from tasks.py / vobiz_webhook / pipeline ──

    async def call_ringing(self, company_id: str, call_uuid: str, phone: str, mode: str, lead_name: str = ""):
        """Emitted the moment we dial out — before the callee has picked up."""
        await self.emit(company_id, {
            "type":       "call_ringing",
            "call_uuid":  call_uuid,
            "phone":      phone,
            "mode":       mode,
            "lead_name":  lead_name,
            "started_at": self._now(),
        })

    # Back-compat alias — call_start == call_ringing (kept so nothing else breaks)
    async def call_start(self, company_id: str, call_uuid: str, phone: str, mode: str):
        await self.call_ringing(company_id, call_uuid, phone, mode)

    async def call_answered(self, company_id: str, call_uuid: str):
        """Emitted once the callee actually picks up."""
        await self.emit(company_id, {
            "type":        "call_answered",
            "call_uuid":   call_uuid,
            "answered_at": self._now(),
        })

    async def call_no_answer(self, company_id: str, call_uuid: str, reason: str = "no_answer"):
        """Emitted when the callee never picks up (rang out, busy, failed)."""
        await self.emit(company_id, {
            "type":      "call_no_answer",
            "call_uuid": call_uuid,
            "reason":    reason,
            "ended_at":  self._now(),
        })

    async def user_msg(self, company_id: str, call_uuid: str, text: str):
        await self.emit(company_id, {
            "type":      "user_msg",
            "call_uuid": call_uuid,
            "text":      text,
            "ts":        self._now(),
        })

    async def ai_msg(self, company_id: str, call_uuid: str, text: str):
        await self.emit(company_id, {
            "type":      "ai_msg",
            "call_uuid": call_uuid,
            "text":      text,
            "ts":        self._now(),
        })

    async def call_end(self, company_id: str, call_uuid: str, duration_sec: int = 0):
        await self.emit(company_id, {
            "type":         "call_end",
            "call_uuid":    call_uuid,
            "duration_sec": duration_sec,
            "ended_at":     self._now(),
        })


# Singleton — imported by vobiz_webhook.py, vobiz_stream_pipeline.py, tasks.py.
# NOTE: this is now one instance PER PROCESS (as it always was) — the fix
# isn't "make it a true cross-process singleton" (impossible in Python
# across separate OS processes), it's "make every instance publish to and
# read from the same external Redis channel" instead of relying on the
# object identity being shared, which it never was.
live_broadcaster = LiveBroadcaster()


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws")
async def live_ws(websocket: WebSocket, company_id: str):
    """
    Connect: ws://<host>/api/v1/live/ws?company_id=<uuid>
    Stays open indefinitely; server pushes events as they happen.
    Client sends nothing (ping frames handled by keep-alive below).
    """
    if not company_id:
        await websocket.close(code=4001)
        return

    await live_broadcaster.connect(company_id, websocket)

    pubsub = None
    reader_task = None
    redis = await live_broadcaster._get_redis()
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(live_broadcaster._channel(company_id))

            async def _forward():
                try:
                    async for message in pubsub.listen():
                        if message.get("type") != "message":
                            continue
                        try:
                            await websocket.send_text(message["data"])
                        except Exception:
                            return
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"Live WS redis forward error: {e}")

            reader_task = asyncio.create_task(_forward())
        except Exception as e:
            logger.warning(f"Live WS: could not subscribe to Redis channel ({e}) — same-process events only for this connection")

    try:
        # Keep-alive: send ping every 25s so the connection doesn't timeout
        # through Cloudflare (which drops idle WS at 100s).
        while True:
            await asyncio.sleep(25)
            await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Live WS error: {e}")
    finally:
        live_broadcaster.disconnect(company_id, websocket)
        if reader_task is not None:
            reader_task.cancel()
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(live_broadcaster._channel(company_id))
                await pubsub.aclose()
            except Exception:
                pass
