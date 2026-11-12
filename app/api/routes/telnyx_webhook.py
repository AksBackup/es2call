"""
Telnyx routes — mounted at /api/v1/telnyx (see main.py).

  POST /webhook       Telnyx Call Control webhooks (call.hangup -> finalize,
                      call.initiated incoming -> answer + start stream).
  WS   /media-stream  Telnyx bidirectional media stream -> Pipecat pipeline
                      (the same pipeline Vobiz uses; see
                      vobiz_stream_pipeline.run_vobiz_stream_pipeline,
                      provider="telnyx").

Deliberately separate from the Vobiz routers — nothing there is modified.
Call finalization (transcript, summary, lead status, plan minutes) is NOT
re-implemented: the hangup webhook calls the same _finalize_hangup() that
Vobiz's /hangup uses, keyed on call_control_id.

Telnyx specifics worth knowing:
  - Webhooks must be answered with HTTP 200 quickly, otherwise Telnyx
    retries / fails over. Heavy work runs in BackgroundTasks.
  - Outbound calls carry company_id/lead_id in the webhook_url query string
    (sent per call by TelnyxService.make_outbound_call), so no static
    webhook URL is needed. INBOUND calls arrive on the webhook URL set on
    the Call Control Application in Telnyx Mission Control — set it to
    https://<PUBLIC_BASE_URL>/api/v1/telnyx/webhook ; the company is then
    found by matching the dialed number to Company.telnyx_phone_number.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, WebSocket
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import CallLog, Company, Lead
from app.services.telephony.call_session import session_manager
from app.services.telephony.telnyx_service import (
    enforce_webhook_signature,
    parse_telnyx_start,
    telnyx_answer_stream_url,
    telnyx_service,
    telnyx_webhook_url,
    verify_stream_token,
)
from app.api.routes.live_ws import live_broadcaster

logger = logging.getLogger(__name__)
router = APIRouter()

_NORMAL_CAUSES = {"normal_clearing", "normal_unspecified"}


# ── helpers ─────────────────────────────────────────────────────────────────
async def _get_company(company_id: Optional[str], db) -> Optional[Company]:
    if not company_id:
        return None
    r = await db.execute(select(Company).where(Company.id == company_id))
    return r.scalar_one_or_none()


async def _get_lead(lead_id: Optional[str], db) -> Optional[Lead]:
    if not lead_id:
        return None
    r = await db.execute(select(Lead).where(Lead.id == lead_id))
    return r.scalar_one_or_none()


async def _company_by_number(number: str, db) -> Optional[Company]:
    if not number:
        return None
    r = await db.execute(select(Company).where(Company.telnyx_phone_number == number))
    return r.scalars().first()


def _build_greeting(company: Company, lead: Optional[Lead], direction: str) -> str:
    """Same wording rules as vobiz_stream_webhook.answer_stream."""
    agent = company.agent_name or "Alex"
    is_male = (getattr(company, "voice_gender", None) or "female").lower() == "male"
    first = lead.name.split()[0] if lead and lead.name else ""
    if direction == "outbound":
        if lead and int(lead.call_attempts or 0) >= 2:
            return (
                f"{first + ' ji, ' if first else ''}"
                f"pichli baar ki baat ko continue karte hain. "
                f"Aap bataiye, abhi kya update hai?"
            )
        return company.greeting_outbound_hi or (
            f"Namaste{' ' + first if first else ''} ji! Main {agent} "
            f"{'bol raha hoon' if is_male else 'bol rahi hoon'} "
            f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
        )
    return company.greeting_inbound_hi or (
        f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
        f"main {agent} hoon. Main aapki kaise madad "
        f"{'kar sakta hoon' if is_male else 'kar sakti hoon'}?"
    )


# ── Webhook ─────────────────────────────────────────────────────────────────
@router.post("/webhook")
async def telnyx_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    company_id: Optional[str] = None,
    lead_id: Optional[str] = None,
):
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {"ok": True}  # never make Telnyx retry garbage

    data = body.get("data") or {}
    event_type = data.get("event_type") or ""
    payload = data.get("payload") or {}
    ccid = payload.get("call_control_id") or ""
    direction = (payload.get("direction") or "").lower()

    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db)
        if not company and direction == "incoming":
            company = await _company_by_number(payload.get("to") or "", db)

        # Signature check — Ed25519 over "{timestamp}|{raw body}", verified
        # against THIS company's Telnyx public key.
        enforce_webhook_signature(raw, request.headers, company)

        if company is None:
            logger.info(f"Telnyx webhook {event_type} — no matching company, ignored | ccid={ccid[:16]}")
            return {"ok": True}

        logger.info(f"Telnyx webhook | {event_type} | ccid={ccid[:16]} | company={company.id}")

        if event_type == "call.hangup":
            cause = payload.get("hangup_cause") or ""
            source = payload.get("hangup_source") or ""
            from app.api.routes.vobiz_webhook import _finalize_hangup
            background_tasks.add_task(
                _finalize_hangup, ccid, str(company.id), lead_id or None,
                cause, f"{cause} {source}".strip(),
                "completed" if cause in _NORMAL_CAUSES or not cause else "failed",
            )

        elif event_type == "call.initiated" and direction == "incoming":
            background_tasks.add_task(_answer_inbound, ccid, str(company.id), payload)

    return {"ok": True}


async def _answer_inbound(ccid: str, company_id: str, payload: dict):
    from app.services import plan_service
    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db)
        if not company:
            return
        lead = None
        from_num = payload.get("from") or ""
        if from_num:
            r = await db.execute(select(Lead).where(Lead.company_id == company.id, Lead.phone == from_num))
            lead = r.scalars().first()

    if not plan_service.has_minutes_available(company):
        logger.warning(f"Telnyx inbound BLOCKED — no plan minutes | company={company_id}")
        await telnyx_service.hangup(ccid, company)
        return

    lead_id = str(lead.id) if lead else None
    ok = await telnyx_service.answer_with_stream(
        ccid, company,
        stream_url=telnyx_answer_stream_url(company_id, lead_id, "support", company.active_product),
        webhook_url=telnyx_webhook_url(company_id, lead_id),
    )
    if not ok:
        logger.error(f"Telnyx inbound answer failed | ccid={ccid[:16]} | company={company_id}")


# ── Media stream ────────────────────────────────────────────────────────────
@router.websocket("/media-stream")
async def media_stream(
    websocket: WebSocket,
    company_id: Optional[str] = None,
    lead_id: Optional[str] = "",
    mode: Optional[str] = "sales",
    product_focus: Optional[str] = "",
    token: Optional[str] = "",
):
    mode = mode or "sales"
    if not company_id or not verify_stream_token(token, company_id, lead_id or "", mode):
        logger.warning("Telnyx media-stream rejected — missing/invalid token")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        parsed = await parse_telnyx_start(websocket)
    except Exception as e:
        logger.error(f"Telnyx media-stream handshake failed: {e}")
        await websocket.close()
        return
    ccid = parsed["call_control_id"]
    logger.info(f"Telnyx media-stream connected | ccid={ccid[:16]} | company={company_id}")

    # ── per-call setup (CallLog, lead counter, Redis session, Live card) ────
    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db)
        if not company:
            logger.error(f"Telnyx media-stream — no company {company_id}, closing")
            await websocket.close()
            return
        lead = await _get_lead(lead_id, db) if lead_id else None
        focus = product_focus or company.active_product

        r = await db.execute(select(CallLog).where(CallLog.call_control_id == ccid))
        call_log = r.scalar_one_or_none()
        is_new_card = call_log is None
        if call_log:
            # Outbound: the task created this row in "ringing" at dial time.
            direction = call_log.direction or "outbound"
            call_log.status = "in_progress"
            call_log.provider = "telnyx"
            await db.commit()
        else:
            # Inbound (or the task hasn't committed yet — unique constraint
            # on call_control_id arbitrates, same as Vobiz's answer-stream).
            direction = "inbound"
            call_log = CallLog(
                company_id=company.id, lead_id=lead.id if lead else None,
                direction=direction, status="in_progress", mode=mode,
                provider="telnyx",
                from_number=parsed["from"], to_number=parsed["to"],
                call_control_id=ccid, started_at=datetime.utcnow(),
            )
            db.add(call_log)
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                r2 = await db.execute(select(CallLog).where(CallLog.call_control_id == ccid))
                call_log = r2.scalar_one_or_none()
                if not call_log:
                    raise
                is_new_card = False
                direction = call_log.direction or "outbound"
        await db.refresh(call_log)
        call_log_id = call_log.id

        if is_new_card:
            await live_broadcaster.call_start(
                company_id, ccid,
                (parsed["from"] if direction == "inbound" else parsed["to"]), mode,
            )

        existing_session = await session_manager.get(ccid)
        if lead and not existing_session:
            lead.call_attempts = int(lead.call_attempts or 0) + 1
            lead.last_called_at = datetime.utcnow()
            lead.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(lead)

        greeting = _build_greeting(company, lead, direction)

    await session_manager.create(
        call_control_id=ccid, company_id=company_id,
        lead_id=lead_id or None, direction=direction, mode=mode,
        call_log_id=call_log_id, live_call_uuid=ccid, product_focus=focus,
    )

    # Fresh objects for the pipeline (setup session is closed above).
    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db)
        lead = await _get_lead(lead_id, db) if lead_id else None

    try:
        from app.services.telephony.vobiz_stream_pipeline import run_vobiz_stream_pipeline
        await run_vobiz_stream_pipeline(
            websocket=websocket,
            call_uuid=ccid,
            live_call_uuid=ccid,
            company=company,
            lead=lead,
            mode=mode,
            greeting=greeting,
            product_focus=focus,
            carrier="telnyx",
            telnyx_start=parsed,
        )
    except Exception as e:
        logger.error(
            f"*** TELNYX STREAM PIPELINE CRASHED *** ccid={ccid[:16]} | {type(e).__name__}: {e}",
            exc_info=True,
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
