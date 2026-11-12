import sys
from celery import Celery

from app.core.config import settings

# Was hardcoded to redis://localhost:6379/0 before — harmless for the Azure
# single-container/supervisord deployment (Redis really is on localhost
# there), but silently wrong for the VM/docker-compose deployment, where
# Redis is a separate container reachable at redis://redis:6379/0. Reading
# from settings means both deployment targets Just Work from their own
# .env, with localhost as the sane default for local/Azure use.
celery_app = Celery(
    "callcenter",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.tasks.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=False,
    worker_pool="solo",
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",
    task_queues={
        "default": {"exchange": "default", "routing_key": "default"},
        "calls":   {"exchange": "calls",   "routing_key": "calls"},
    },
    task_routes={
        "app.tasks.call_tasks.run_outbound_call":          {"queue": "calls"},
        "app.tasks.schedule_tasks._dispatch_voice_batch":  {"queue": "calls"},
        "app.tasks.schedule_tasks.check_and_dispatch":     {"queue": "default"},
    },
    beat_schedule={
        "check-schedules": {
            "task":    "app.tasks.schedule_tasks.check_and_dispatch",
            "schedule": 60.0,
            "options": {"queue": "default"},
        },
    },
)

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())