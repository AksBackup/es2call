"""
Celery Tasks
- run_outbound_call: dispatch a single outbound call
- check_and_dispatch: schedule runner (every 60s)
"""
import asyncio
import logging
from datetime import datetime, time as dtime
from typing import Optional

import pytz

from app.core.celery_app import celery_app

logger = logging.getLogger(__name__)


def run_async(coro):
    import sys
    if sys.platform == "win32":
        import asyncio as _asyncio
        _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    except Exception as e:
        logger.error(f"run_async error: {e}", exc_info=True)
        raise
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            # Dispose the DB engine's connection pool WHILE this loop is
            # still open. Every call to run_async() creates a fresh event
            # loop, but app.core.database's `engine` is a single global
            # object — its pooled asyncpg connections are permanently
            # tied to whatever loop was running when they were opened.
            # Without this, the pool from THIS run's loop survives into
            # the next scheduled task's brand-new loop, and asyncpg fails
            # with "Event loop is closed" / "attached to a different
            # loop" — reliably, on every run after the first, since each
            # one gets its own loop. Disposing here forces the next call
            # to open clean connections under its own (also new) loop.
            from app.core.database import engine
            loop.run_until_complete(engine.dispose())
            # Same fix, same reason, different global singleton — see
            # RedisClient.aclose()'s docstring in app/core/redis_client.py.
            # This is what was causing "License gate check failed
            # (allowing call): Event loop is closed" — is_call_allowed()
            # reads cached license status from this same Redis client.
            from app.core.redis_client import redis_client
            loop.run_until_complete(redis_client.aclose())
        except Exception:
            pass
        finally:
            loop.close()
            asyncio.set_event_loop(None)


# ── Call Tasks ────────────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.call_tasks.run_outbound_call")
def run_outbound_call(lead_id: str, company_id: str, call_mode: str = "sales", provider: str = "vobiz", product_focus: Optional[str] = None):
    try:
        return run_async(_async_outbound_call(lead_id, company_id, call_mode, provider, product_focus))
    except Exception as exc:
        logger.error(f"Outbound call task failed: {exc}")
        raise self.retry(exc=exc, countdown=120)


async def _async_outbound_call(lead_id: str, company_id: str, call_mode: str, provider: str = "vobiz", product_focus: Optional[str] = None):
    from app.core.database import AsyncSessionLocal
    from app.models.models import Lead, Company, BatchLead, CallLog
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        lead    = await db.get(Lead, lead_id)
        company = await db.get(Company, company_id)

        if not lead or not company:
            return {"error": "lead or company not found"}
        if not lead.is_active or lead.status == "do_not_call":
            return {"skipped": "inactive or do_not_call"}
        if not lead.phone:
            return {"skipped": "no phone number"}

        # ── Plan gate: block call if this company is out of minutes / expired ──
        # Replaces the old license-activation-key gate. Purely local DB state
        # now (no external service call), so — per confirmed product
        # behavior — this fails CLOSED, not open: zero minutes or an
        # expired plan blocks the call immediately, no exceptions.
        from app.services import plan_service
        if not plan_service.has_minutes_available(company):
            reason = "plan expired" if plan_service.is_plan_expired(company) else "out of minutes"
            logger.warning(f"Call BLOCKED — {reason} | company={company_id}")
            return {"skipped": reason}

        # ── Demo account gate: block + decrement the shared counter ────────────
        # Real customer accounts have is_demo_account=False and are never
        # touched by this. For a demo account, atomically decrements
        # demo_calls_remaining as part of the same UPDATE that checks it's
        # > 0 — an ordinary "read the count, check it, then write" would
        # have a race: two calls placed within the same second could both
        # read "1 remaining" before either writes, and both would go
        # through. The WHERE clause below makes the decrement and the
        # check the same atomic database operation, so only one of two
        # simultaneous requests can ever win the last call.
        if company.is_demo_account:
            from sqlalchemy import update as _update
            result = await db.execute(
                _update(Company)
                .where(Company.id == company_id, Company.demo_calls_remaining > 0)
                .values(demo_calls_remaining=Company.demo_calls_remaining - 1)
            )
            await db.commit()
            if result.rowcount == 0:
                logger.warning(f"Demo call BLOCKED — no demo calls remaining | company={company_id}")
                return {"skipped": "demo call limit reached — ask for a new demo login"}
            logger.info(f"Demo call allowed — decremented counter | company={company_id}")

        from app.services.telephony.vobiz_service import vobiz_service
        call_control_id = await vobiz_service.make_outbound_call(
            to_number=lead.phone,
            company_id=company_id,
            lead_id=lead_id,
            call_mode=call_mode,
            company=company,
            product_focus=product_focus or company.active_product,
        )

        if call_control_id:
            logger.info(f"Outbound call dispatched (vobiz) → {lead.phone} | cid={call_control_id}")

            # Create the CallLog now, in "ringing" state, BEFORE the callee
            # has picked up. vobiz_webhook.py's /answer reuses this same row
            # (by call_control_id) once answered, and /hangup closes it out
            # as "no_answer" if the callee never picks up — this is what
            # makes the ringing→called-no-answer lead transition possible.
            call_log = CallLog(
                company_id=company_id, lead_id=lead_id,
                direction="outbound", status="ringing", mode=call_mode,
                provider="vobiz",
                from_number=company.vobiz_phone_number or "",
                to_number=lead.phone,
                call_control_id=call_control_id, started_at=datetime.utcnow(),
            )
            db.add(call_log)
            await db.commit()

            try:
                from app.api.routes.live_ws import live_broadcaster
                await live_broadcaster.call_ringing(
                    company_id, call_control_id, lead.phone, call_mode, lead_name=lead.name or "",
                )
            except Exception as e:
                logger.debug(f"Live broadcast (ringing) error: {e}")

            return {"call_control_id": call_control_id, "lead_id": lead_id}
        else:
            # Call failed (network error, API error, missing credentials, etc.)
            # Clear the batch lock so the scheduler can retry the next lead.
            # Without this, a failure leaves the lock stuck as "pending"
            # forever, blocking all future calls in the batch. Same fix as
            # the original Telnyx-only failure path, now shared by both providers.
            logger.error(f"{provider} call failed for lead {lead_id} — clearing batch lock")
            try:
                r2 = await db.execute(
                    select(BatchLead).where(
                        BatchLead.lead_id == lead_id,
                        BatchLead.result  == "dispatched",
                    ).order_by(BatchLead.processed_at.desc())
                )
                bl = r2.scalars().first()
                if bl:
                    _r = _sync_redis()
                    _r.delete(f"batch_call_active:{bl.batch_id}")
                    # Reset processed=False so the scheduler picks this lead up
                    # again on the next tick. Without this, processed=True means
                    # the lead is permanently skipped even after the lock clears.
                    bl.processed = False
                    bl.result    = "failed"
                    await db.commit()
                    logger.info(f"Batch lock cleared + lead reset for retry | batch={bl.batch_id}")
            except Exception as e:
                logger.warning(f"Could not clear batch lock after failure: {e}")
            return {"error": f"{provider} call failed"}


# retry_failed_calls_task removed — was bypassing plan gate and batch system


# ── License Tasks — REMOVED ────────────────────────────────────────────────
# revalidate_all_licenses() used to re-check every activated company's
# license against the license table on a schedule. The yearly license
# system is gone (see app/services/plan_service.py) — there's nothing
# external to revalidate; minute balances are plain DB columns, checked
# synchronously wherever a call is about to be placed. If you still see
# a beat-schedule entry named "app.tasks.license_tasks.revalidate_all_licenses"
# in app/core/celery_app.py, remove it too — it now points at nothing.


# ── Schedule Tasks ────────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.schedule_tasks.check_and_dispatch")
def check_and_dispatch():
    return run_async(_async_check_schedules())


async def _async_check_schedules():
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, and_
    from app.models.models import Schedule, Batch

    dispatched = 0
    skipped    = 0
    now_utc    = datetime.utcnow()
    logger.info(f"check_and_dispatch running | now_utc={now_utc.strftime('%Y-%m-%d %H:%M:%S')}")

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(Schedule, Batch)
            .join(Batch, Schedule.batch_id == Batch.id)
            .where(
                and_(
                    Schedule.is_active == True,
                    Batch.status.in_(["scheduled", "running"]),
                )
            )
        )
        rows = r.all()
        logger.info(f"Active schedules found: {len(rows)}")

        for row in rows:
            schedule, batch = row.Schedule, row.Batch
            tz_name = schedule.base_timezone or "UTC"

            try:
                tz = _resolve_tz(tz_name)
                start_local = schedule.start_datetime
                start_aware = tz.localize(start_local) if start_local.tzinfo is None else start_local
                start_utc   = start_aware.astimezone(pytz.utc).replace(tzinfo=None)
            except Exception as e:
                logger.warning(f"Schedule {schedule.id}: timezone error '{tz_name}': {e}")
                start_utc = schedule.start_datetime

            if start_utc > now_utc:
                logger.info(f"Schedule {schedule.id}: not yet — starts {start_utc} UTC")
                skipped += 1
                continue

            if schedule.end_datetime:
                try:
                    tz = _resolve_tz(tz_name)
                    end_local = schedule.end_datetime
                    end_aware = tz.localize(end_local) if end_local.tzinfo is None else end_local
                    end_utc   = end_aware.astimezone(pytz.utc).replace(tzinfo=None)
                except Exception:
                    end_utc = schedule.end_datetime
                if now_utc > end_utc:
                    logger.info(f"Schedule {schedule.id}: expired — deactivating")
                    schedule.is_active = False
                    await db.commit()
                    continue

            in_win, reason = _in_window_debug(schedule)
            if not in_win:
                logger.info(f"Schedule {schedule.id}: outside window — {reason}")
                skipped += 1
                continue

            logger.info(f"Dispatching schedule {schedule.id} (batch={batch.name}, type={batch.batch_type}, agent_type={batch.agent_type})")
            # agent_type="human" batches are NEVER auto-dialed — those leads
            # are worked manually from the Human Call tab (agent dials via
            # the Vobiz WebRTC softphone). Only "ai" batches go through the
            # automated dispatch pipeline below.
            if batch.batch_type == "voice" and batch.agent_type != "human":
                _dispatch_voice_batch.delay(batch.id, schedule.id)

            dispatched += 1

    logger.info(f"check_and_dispatch done | dispatched={dispatched} skipped={skipped}")
    return {"dispatched": dispatched, "skipped": skipped}


@celery_app.task(name="app.tasks.schedule_tasks._dispatch_voice_batch")
def _dispatch_voice_batch(batch_id: str, schedule_id: str):
    return run_async(_async_voice_batch(batch_id, schedule_id))


async def _async_voice_batch(batch_id: str, schedule_id: str):
    """
    FIX: One call at a time per batch.

    Root cause of concurrent calls:
    1. countdown=i*delay with i=0 meant first call fired immediately with no delay.
    2. No check for whether a previous call from this batch is still active.
       The scheduler fired the next lead regardless of call state.

    Fix:
    - Always dispatch exactly ONE lead per tick (per_tick forced to 1).
    - Before dispatching, check Redis for an active call on this batch.
      If one is in progress, skip this tick entirely and wait for it to finish.
    - The call sets a Redis lock on answer and releases it on hangup.
      (See telephony.py _on_answered and _on_hangup — they call
       batch_call_lock.acquire/release via the helpers below.)
    - delay_between_seconds now means minimum gap AFTER the previous call ends,
      not a countdown timer that ignores call duration.
    """
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, and_
    from app.models.models import Batch, BatchLead, Lead, Schedule

    async with AsyncSessionLocal() as db:
        batch    = await db.get(Batch, batch_id)
        schedule = await db.get(Schedule, schedule_id)
        if not batch or batch.status == "completed":
            return {"skipped": "batch completed or missing"}

        # ── FIX: Check if a call from this batch is still active ──
        active_cid = await _get_active_batch_call(batch_id)
        if active_cid:
            logger.info(
                f"_dispatch_voice_batch | batch={batch.name} | "
                f"skipping — call {active_cid[:12]} still active"
            )
            return {"skipped": "call in progress"}

        # ── FIX: Always dispatch exactly 1 lead per tick ──
        # delay_between_seconds is enforced via Redis lock TTL after hangup,
        # not via countdown (which ran concurrently).
        delay = schedule.delay_between_seconds if schedule else 30

        # Check if we're still within the post-call cooldown window
        in_cooldown = await _batch_in_cooldown(batch_id)
        if in_cooldown:
            logger.info(f"_dispatch_voice_batch | batch={batch.name} | in cooldown, waiting")
            return {"skipped": "cooldown"}

        r = await db.execute(
            select(BatchLead, Lead)
            .join(Lead, BatchLead.lead_id == Lead.id)
            .where(and_(
                BatchLead.batch_id == batch_id,
                BatchLead.processed == False,
                Lead.is_active == True,
                Lead.status.notin_(["do_not_call", "closed_won", "closed_lost"]),
            ))
            .limit(1)   # FIX: always exactly 1
        )
        row = r.first()

        if not row:
            # No more leads — mark completed
            batch.status       = "completed"
            batch.completed_at = datetime.utcnow()
            await db.commit()
            logger.info(f"Batch {batch.name} completed — all leads processed")
            return {"dispatched": 0}

        bl, lead = row.BatchLead, row.Lead

        if schedule and schedule.use_lead_timezone and lead.timezone:
            if not _in_window_tz(schedule, lead.timezone):
                logger.debug(f"Lead {lead.id} skipped — outside window in {lead.timezone}")
                return {"skipped": "lead timezone window"}

        batch.status     = "running"
        batch.started_at = batch.started_at or datetime.utcnow()

        # Mark as processed before dispatching to prevent double-dispatch
        # if check_and_dispatch fires again before the call completes
        bl.processed    = True
        bl.processed_at = datetime.utcnow()
        bl.result       = "dispatched"
        batch.leads_processed += 1
        await db.commit()

        # Dispatch — no countdown, fire immediately (cooldown already checked above)
        run_outbound_call.apply_async(
            args=[lead.id, batch.company_id, batch.call_mode or "sales", batch.provider or "vobiz", batch.product_focus or None],
        )
        logger.info(
            f"_dispatch_voice_batch | batch={batch.name} | "
            f"dispatched → {lead.phone} (lead={lead.id})"
        )

        # Set active call marker in Redis — cleared by telephony._on_hangup
        # TTL of 5min as safety net in case hangup webhook never arrives
        await _set_active_batch_call(batch_id, "pending", ttl=300)

        # Store batch_id in a lead→batch lookup key so telephony.py can find
        # the correct batch_id on hangup without a fragile BatchLead DB query.
        # The BatchLead query was returning wrong batch_id when multiple schedules
        # existed for the same batch name, causing lock mismatch.
        try:
            _r = _sync_redis()
            _r.setex(f"lead_batch:{lead.id}", 3600, batch_id)
            logger.debug(f"lead_batch key set | lead={lead.id} | batch={batch_id}")
        except Exception as e:
            logger.warning(f"Could not set lead_batch key: {e}")

        # Set cooldown so next tick waits delay_between_seconds after this call ends
        await _set_batch_cooldown(batch_id, delay)

        # Pre-load company/lead/RAG into cache during cooldown window.
        # Run directly (not as task) because we're already inside run_async()
        # and create_task() fails after the event loop closes.
        try:
            await _preload_next_call_cache(
                cid="pending",
                company_id=batch.company_id,
                lead_id=lead.id,
            )
        except Exception as e:
            logger.warning(f"Pre-load cache error (non-fatal): {e}")

        return {"dispatched": 1, "lead": lead.phone}


async def _preload_next_call_cache(cid: str, company_id: str, lead_id: str):
    """
    Pre-fetch company+lead into the call cache during the cooldown window.
    Only fetches DB data and RAG — no external identity/service dependencies.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select
        from app.models.models import Company, Lead
        from app.core.redis_client import redis_client

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Company).where(Company.id == company_id))
            company = r.scalar_one_or_none()
            r2 = await db.execute(select(Lead).where(Lead.id == lead_id))
            lead = r2.scalar_one_or_none()

        if not company:
            return

        # Build plain dict cache — no ORM objects, no Firebase
        cache = {
            "company": {
                "id": company.id,
                "name": company.name,
                "description": company.description or "",
                "services": company.services or "",
                "faqs": company.faqs or "",
                "products": company.products or [],
                "active_product": company.active_product,
                "agent_name": company.agent_name or "Aria",
                "voice_language": company.voice_language or "en-US",
                "voice_gender": company.voice_gender or "female",
                "forward_number": company.forward_number,
                "inbound_system_prompt": company.inbound_system_prompt,
                "outbound_sales_prompt": company.outbound_sales_prompt,
                "greeting_inbound": company.greeting_inbound,
                "greeting_outbound": company.greeting_outbound,
                "vobiz_phone_number": company.vobiz_phone_number,
            },
            "lead": {
                "id": lead.id,
                "name": lead.name,
                "phone": lead.phone,
                "status": lead.status,
                "notes": lead.notes or "",
                "key_info": lead.key_info or {},
                "call_attempts": lead.call_attempts or 0,
                "language": lead.language or "english",
                "timezone": lead.timezone or "Asia/Kolkata",
            } if lead else None,
            "rag_context": "",
        }

        await redis_client.set(f"call_cache:preload:{lead_id}", cache, expire=600)
        logger.info(f"Pre-loaded call cache for lead={lead_id[:8]}")
    except Exception as e:
        logger.warning(f"Pre-load cache error (non-fatal): {e}")


# ── Redis batch call lock helpers ─────────────────────────────────────────────
# These are lightweight Redis keys used to serialize calls within a batch.
# Key: batch_call_active:{batch_id}  → call_control_id or "pending"
# Key: batch_call_cooldown:{batch_id} → "1" during delay_between_seconds window

# ── Sync Redis helpers for batch call locking ────────────────────────────────
# These use synchronous redis-py calls because they run inside Celery tasks
# which use run_async() with a fresh event loop per task. Using an async Redis
# client across different event loops causes silent failures where get() always
# returns None, making the lock check useless. Sync Redis avoids this entirely.

def _sync_redis():
    """Get a synchronous Redis connection."""
    import redis as redis_sync
    from app.core.config import settings
    url = getattr(settings, "REDIS_URL", "redis://localhost:6379")
    return redis_sync.from_url(url, decode_responses=True)


async def _get_active_batch_call(batch_id: str) -> Optional[str]:
    """Returns cid if a call from this batch is active, else None."""
    try:
        r = _sync_redis()
        val = r.get(f"batch_call_active:{batch_id}")
        logger.debug(f"Batch lock check | batch={batch_id} | active={val}")
        return val if val else None
    except Exception as e:
        logger.warning(f"Redis get active call error: {e}")
        return None


async def _set_active_batch_call(batch_id: str, cid: str, ttl: int = 300):
    """Mark a call as active for this batch. TTL=300s safety net."""
    try:
        r = _sync_redis()
        r.setex(f"batch_call_active:{batch_id}", ttl, cid)
        logger.debug(f"Batch lock set | batch={batch_id} | cid={cid[:12]} | ttl={ttl}")
    except Exception as e:
        logger.warning(f"Redis set active call error: {e}")


async def _clear_active_batch_call(batch_id: str):
    """Clear the active call marker when call ends."""
    try:
        r = _sync_redis()
        r.delete(f"batch_call_active:{batch_id}")
        logger.debug(f"Batch lock cleared | batch={batch_id}")
    except Exception as e:
        logger.warning(f"Redis clear active call error: {e}")


async def _batch_in_cooldown(batch_id: str) -> bool:
    """Returns True if within delay_between_seconds cooldown after last call."""
    try:
        r = _sync_redis()
        val = r.get(f"batch_call_cooldown:{batch_id}")
        return bool(val)
    except Exception as e:
        logger.warning(f"Redis cooldown check error: {e}")
        return False


async def _set_batch_cooldown(batch_id: str, delay_seconds: int):
    """Set cooldown key that expires after delay_between_seconds."""
    try:
        r = _sync_redis()
        r.setex(f"batch_call_cooldown:{batch_id}", delay_seconds, "1")
    except Exception as e:
        logger.warning(f"Redis set cooldown error: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_tz(name: str):
    aliases = {
        "Asia/Mumbai":   "Asia/Kolkata",
        "Asia/Calcutta": "Asia/Kolkata",
        "Asia/Bombay":   "Asia/Kolkata",
        "Asia/Delhi":    "Asia/Kolkata",
        "IST":           "Asia/Kolkata",
        "EST":           "America/New_York",
        "PST":           "America/Los_Angeles",
        "CST":           "America/Chicago",
        "MST":           "America/Denver",
        "GMT":           "UTC",
    }
    name = (name or "UTC").strip()
    return pytz.timezone(aliases.get(name, name))


def _in_window_debug(schedule) -> tuple:
    try:
        tz  = _resolve_tz(schedule.base_timezone or "UTC")
        now = datetime.now(tz)
        day = now.strftime("%A")

        if schedule.allowed_days and day not in schedule.allowed_days:
            return False, f"today is {day}, allowed={schedule.allowed_days}"

        t  = now.time()
        sh, sm = map(int, (schedule.window_start_time or "00:00").split(":"))
        eh, em = map(int, (schedule.window_end_time   or "23:59").split(":"))
        start  = dtime(sh, sm)
        end    = dtime(eh, em)

        if not (start <= t <= end):
            return False, f"time {t.strftime('%H:%M')} not in {start}–{end} {tz.zone}"

        return True, "ok"
    except Exception as e:
        return True, f"window check error {e} — defaulting to allowed"


def _in_window(schedule) -> bool:
    ok, _ = _in_window_debug(schedule)
    return ok


def _in_window_tz(schedule, tz_str: str) -> bool:
    try:
        tz  = _resolve_tz(tz_str)
        now = datetime.now(tz)
        return _check(now, schedule)
    except Exception:
        return _in_window(schedule)


def _check(now_local: datetime, schedule) -> bool:
    day = now_local.strftime("%A")
    if schedule.allowed_days and day not in schedule.allowed_days:
        return False
    t = now_local.time()
    sh, sm = map(int, (schedule.window_start_time or "00:00").split(":"))
    eh, em = map(int, (schedule.window_end_time   or "23:59").split(":"))
    return dtime(sh, sm) <= t <= dtime(eh, em)