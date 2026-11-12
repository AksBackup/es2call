"""
Vobiz Stream Webhook — low-latency bidirectional WebSocket call flow.

COMPLETELY SEPARATE from vobiz_webhook.py (Record mode). Nothing in that
file is imported, modified, or depended on here. This exists so a call
can opt into this path without any risk to the working Record-mode flow —
only calls whose /answer webhook explicitly points at THIS router's
/answer-stream route ever touch this code at all.

Confirmed from Vobiz support (this project, same thread): when
bidirectional="true" on <Stream>, audioTrack MUST be "inbound" (not
"outbound" or "both") — an unintuitive requirement, flagged here because
it's exactly the kind of thing that silently breaks if "cleaned up" later
by someone who assumes audioTrack="both" is more correct for bidirectional
audio. It is not, on this platform.

Flow:
  /answer-stream  → <Stream bidirectional="true" audioTrack="inbound"
                      contentType="audio/x-mulaw;rate=8000">wss://.../media-stream</Stream>
  /media-stream    → WebSocket. Vobiz sends connected/start/media/dtmf/stop
                      events; this hands off to vobiz_stream_pipeline.py,
                      which runs Deepgram streaming STT -> LLM -> Sarvam
                      streaming TTS -> back out, all on one persistent
                      connection instead of a webhook round-trip per turn.

HOW TO ENABLE FOR A SPECIFIC CALL: point that call's answer_url at
/answer-stream instead of /answer. The rest of the app (dialer, batch
calling, etc.) is untouched and keeps using /answer (Record mode) unless
you deliberately change where a call's answer_url points.

FIRST TEST: watch the logs for the `Vobiz stream start | ...` line the first
time a real call hits /media-stream — that confirms parse_vobiz_start()
(from the official pipecat-vobiz package) is reading Vobiz's real streamId/
callId/mediaFormat correctly.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Response, WebSocket
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import CallLog, Company, Lead
from app.services.telephony.call_session import session_manager
from app.services.telephony.vobiz_service import _get_base_url
from app.api.routes.live_ws import live_broadcaster

logger = logging.getLogger(__name__)
router = APIRouter()


def _media_stream_ws_url() -> str:
    base = _get_base_url()
    ws = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws}/api/v1/vobiz-stream/media-stream"


@router.post("/answer-stream")
async def answer_stream(
    request: Request,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
    mode:       Optional[str] = "support",
    product_focus: Optional[str] = None,
):
    form      = await request.form()
    call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
    from_num  = form.get("From", "")
    to_num    = form.get("To", "")

    logger.info(f"Vobiz answer-stream | call_uuid={call_uuid[:12] if call_uuid else '?'} | company={company_id}")

    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db) if company_id else None
        if not company:
            logger.error(f"Vobiz answer-stream — no company for company_id={company_id}")
            return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

        lead = await _get_lead(lead_id, db) if lead_id else None

        # Same idempotency pattern as the Record-mode file's /answer —
        # Vobiz can retry a webhook delivery for the same CallUUID.
        # NOTE: call_control_id, session_manager's key, and everything
        # /hangup does are all intentionally left exactly as they were —
        # /hangup (vobiz_webhook.py) always resolves CallUUID this same
        # way and looks the Redis session up by it, so changing what's
        # used here would break call finalization (duration/summary/lead
        # status). See `live_uuid` below for the actual Live-tab fix.
        existing = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
        call_log = existing.scalar_one_or_none()
        if not call_log:
            call_log = CallLog(
                company_id=company.id, lead_id=lead.id if lead else None,
                direction="outbound", status="in_progress", mode=mode or "support",
                provider="vobiz-stream",
                from_number=from_num or company.vobiz_phone_number or "",
                to_number=to_num,
                call_control_id=call_uuid, started_at=datetime.utcnow(),
            )
            db.add(call_log)
            try:
                await db.commit()
                await db.refresh(call_log)
            except Exception:
                await db.rollback()
                existing2 = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
                call_log = existing2.scalar_one_or_none()
                if not call_log:
                    raise

        # ROOT CAUSE of the Live tab never updating: for outbound calls,
        # tasks.py creates the "ringing" CallLog and broadcasts
        # `call_ringing` using call_control_id = the `request_uuid`
        # vobiz_service.make_outbound_call() returns at dial time. But
        # `call_uuid` above prefers Vobiz's `CallUUID` — a DIFFERENT id
        # from that same request_uuid — and every event
        # vobiz_stream_pipeline.py broadcasts from here on (call_answered,
        # user_msg, ai_msg, call_end) was keyed on it. request_uuid !=
        # CallUUID, so liveCallStore.ts's
        # `findIndex(x => x.call_uuid === event.call_uuid)` never matched
        # and every post-ringing event was silently dropped — the card
        # sat frozen on "ringing" forever.
        #
        # Fix: find that pre-existing ringing row (by lead_id, same as
        # tasks.py's own dispatch) and use ITS call_control_id
        # (request_uuid) as a separate `live_uuid` — used ONLY for the
        # live_broadcaster calls the pipeline makes, so the frontend's
        # card (created with that id) actually gets updated. Everything
        # else (call_uuid, the CallLog row above, session_manager,
        # /hangup) is untouched.
        live_uuid = None
        if lead_id:
            ringing = await db.execute(
                select(CallLog)
                .where(
                    CallLog.company_id == company.id,
                    CallLog.lead_id    == lead_id,
                    CallLog.status     == "ringing",
                )
                .order_by(CallLog.started_at.desc())
            )
            ringing_log = ringing.scalars().first()
            if ringing_log:
                live_uuid = ringing_log.call_control_id
        if not live_uuid:
            # No pre-existing ringing card for this call (e.g. inbound, or
            # dispatched outside tasks.py) — nothing has told the frontend
            # about it yet, so emit the initial event ourselves, same as
            # Record-mode does, keyed on the same id everything else here
            # uses so it's self-consistent.
            live_uuid = call_uuid
            await live_broadcaster.call_start(company_id, live_uuid, to_num or (lead.phone if lead else ""), mode or "support")

        await session_manager.create(
            call_control_id=call_uuid, company_id=company.id,
            lead_id=lead.id if lead else None,
            direction="outbound", mode=mode or "support", call_log_id=call_log.id,
            live_call_uuid=live_uuid,
            product_focus=product_focus or company.active_product,
        )

        agent = company.agent_name or "Alex"
        is_male = (getattr(company, "voice_gender", None) or "female").lower() == "male"
        if mode == "sales":
            first = lead.name.split()[0] if lead and lead.name else ""
            greeting = (
                company.greeting_outbound_hi
                or (
                    f"Namaste{' ' + first if first else ''} ji! Main {agent} "
                    f"{'bol raha hoon' if is_male else 'bol rahi hoon'} "
                    f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
                )
            )
        else:
            greeting = (
                company.greeting_inbound_hi
                or (
                    f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
                    f"main {agent} hoon. Main aapki kaise madad "
                    f"{'kar sakta hoon' if is_male else 'kar sakti hoon'}?"
                )
            )

    from urllib.parse import quote
    ws_url = (
        f"{_media_stream_ws_url()}?company_id={company_id}&amp;lead_id={lead_id or ''}"
        f"&amp;mode={mode or 'support'}&amp;product_focus={quote(product_focus or company.active_product or '')}&amp;call_uuid={quote(call_uuid)}"
        f"&amp;live_uuid={quote(live_uuid)}"
        f"&amp;greeting={quote(greeting)}"
    )
    # audioTrack="inbound" is REQUIRED when bidirectional="true" — confirmed
    # by Vobiz support. Do not "fix" this to "both" — that's what looks
    # more correct and is wrong.
    xml = (
        f'<Response><Stream bidirectional="true" audioTrack="inbound" '
        f'contentType="audio/x-mulaw;rate=8000" keepCallAlive="true">{ws_url}</Stream></Response>'
    )
    logger.info(f"Vobiz answer-stream XML | call_uuid={call_uuid[:12] if call_uuid else '?'} | ws={ws_url}")
    return Response(content=xml, media_type="text/xml")


@router.websocket("/media-stream")
async def media_stream(
    websocket: WebSocket,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
    mode:       Optional[str] = "support",
    product_focus: Optional[str] = None,
    greeting:   Optional[str] = "",
    call_uuid:  Optional[str] = "",
    live_uuid:  Optional[str] = "",
):
    await websocket.accept()
    logger.info(f"Vobiz media-stream connected | call_uuid={(call_uuid or '')[:12]} | company={company_id}")

    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db) if company_id else None
        if not company:
            logger.error(f"Vobiz media-stream — no company for company_id={company_id}, closing socket")
            await websocket.close()
            return
        lead = await _get_lead(lead_id, db) if lead_id else None

    try:
        from app.services.telephony.vobiz_stream_pipeline import run_vobiz_stream_pipeline
        await run_vobiz_stream_pipeline(
            websocket=websocket,
            call_uuid=call_uuid or "",
            live_call_uuid=live_uuid or call_uuid or "",
            company=company,
            lead=lead,
            mode=mode or "support",
            greeting=greeting or "",
            product_focus=product_focus or company.active_product,
        )
    except Exception as e:
        # If this fires within ~1s of "media-stream connected" above, the
        # call hung up immediately — check the exception type/traceback
        # right here first. A ModuleNotFoundError / ImportError means a
        # pipecat-ai version mismatch (see requirements.txt comment on the
        # pipecat-ai pin); anything else is a real runtime bug in the
        # pipeline itself, not a Vobiz-side issue.
        logger.error(
            f"*** VOBIZ STREAM PIPELINE CRASHED *** call_uuid={(call_uuid or '')[:12]} "
            f"| {type(e).__name__}: {e}",
            exc_info=True,
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _get_company(company_id: str, db) -> Optional[Company]:
    r = await db.execute(select(Company).where(Company.id == company_id))
    return r.scalar_one_or_none()

async def _get_lead(lead_id: Optional[str], db) -> Optional[Lead]:
    if not lead_id:
        return None
    r = await db.execute(select(Lead).where(Lead.id == lead_id))
    return r.scalar_one_or_none()
