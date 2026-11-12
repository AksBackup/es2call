# app/api/routes/human_calls.py
#
# "Human Call" tab — leads assigned to a Batch with agent_type="human" are
# NOT auto-dialed by the AI (see the agent_type check in
# app/tasks/tasks.py's schedule dispatch loop). Instead they show up here
# for an agent to work manually.
#
# DIAL MECHANISM — click-to-call bridge, not literal browser WebRTC audio:
# This project's entire Vobiz integration (see
# app/services/telephony/vobiz_service.py) is a Plivo-compatible REST
# call-creation + XML answer_url flow — there is no WebRTC SDK usage
# anywhere in this codebase, and nothing here can verify Vobiz's account
# actually supports one. Rather than build a telephony feature against an
# unverified API surface, "Dial" here does the same thing proven to work
# for AI calls (make_outbound_call): Vobiz rings the AGENT's own phone
# first; once they pick up, the answer_url below returns Plivo/Vobiz XML
# that bridges the live leg straight into the lead's number. The agent
# ends up talking to the lead over a normal phone call, using the
# company's Vobiz caller ID — same underlying call quality/reliability as
# every AI call this system already places, just with a human on the
# audio instead of the AI pipeline. If a true in-browser softphone is
# still wanted, it needs confirming with Vobiz support first (ask for
# "Endpoint"/WebRTC SDK docs for this account) — swap this file's dial()
# for that once confirmed; nothing else in the Human Call flow depends on
# which transport actually carries the audio.
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, AsyncSessionLocal
from app.core.rate_limit import rate_limit
from app.core.security import get_current_active_user
from app.models.models import Batch, BatchLead, CallLog, Company, Lead
from app.services import plan_service
from app.services.telephony.vobiz_service import _get_base_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/human-calls", tags=["human-calls"])


async def _company(user_id: str, db: AsyncSession) -> Company:
    r = await db.execute(select(Company).where(Company.owner_id == user_id))
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Company not found. Please set up your company first.")
    return c


def _lead_dict(l: Lead, batch_name: Optional[str] = None) -> dict:
    """Same shape as leads.py's _lead_dict, plus the human batch this lead
    is currently queued under — the frontend table reuses the Leads
    columns per the product spec ("same as in the leads tab, every thing
    from lead columns")."""
    return {
        "id": l.id, "name": l.name, "phone": l.phone, "email": l.email,
        "status": l.status, "interest_level": l.interest_level,
        "source": l.source, "language": l.language, "timezone": l.timezone,
        "notes": l.notes, "key_info": l.key_info,
        "follow_up_question": l.follow_up_question,
        "call_attempts": l.call_attempts, "last_called_at": l.last_called_at,
        "campaign_name": l.campaign_name,
        "batch_name": batch_name,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /human-calls/leads — leads sitting in an active "human" batch
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/leads")
async def list_human_call_leads(
    batch_id: Optional[str] = None,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)

    conds = [Batch.company_id == company.id, Batch.agent_type == "human", Batch.batch_type == "voice"]
    if batch_id:
        conds.append(Batch.id == batch_id)
    else:
        # Default view: everything except failed batches. "completed" is
        # included deliberately — a batch flips to completed the moment
        # its last lead is processed (see complete_call() below), but
        # completed leads still need to show up here so the Re-call
        # button (frontend) has something to act on. Excluding
        # "completed" would make every lead vanish from this screen the
        # instant its batch finished, which defeats the point of re-call
        # existing at all.
        conds.append(Batch.status != "failed")

    r = await db.execute(
        select(BatchLead, Lead, Batch)
        .join(Lead, BatchLead.lead_id == Lead.id)
        .join(Batch, BatchLead.batch_id == Batch.id)
        .where(and_(*conds))
        .order_by(BatchLead.processed.asc(), Lead.created_at.desc())
    )
    rows = r.all()
    return {
        "total": len(rows),
        "leads": [_lead_dict(lead, batch.name) | {"batch_id": batch.id, "dialed": bool(bl.processed)} for bl, lead, batch in rows],
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /human-calls/dial — rings the agent's own phone, bridges to the lead
# ─────────────────────────────────────────────────────────────────────────────
class DialRequest(BaseModel):
    lead_id: str
    agent_phone: str   # the agent's own number — Vobiz rings this first


@router.post("/dial", dependencies=[Depends(rate_limit("human_dial", 60, 3600))])
async def dial(
    data: DialRequest,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)

    if not plan_service.has_minutes_available(company):
        reason = "plan expired" if plan_service.is_plan_expired(company) else "out of minutes"
        raise HTTPException(402, f"Can't place call — {reason}. Buy a plan to continue.")

    lead = await db.get(Lead, data.lead_id)
    if not lead or lead.company_id != company.id:
        raise HTTPException(404, "Lead not found")

    if not company.vobiz_auth_id or not company.vobiz_auth_token or not company.vobiz_phone_number:
        raise HTTPException(400, "Vobiz isn't configured for this company yet — set it up in Settings first.")

    call_log = CallLog(
        company_id=company.id,
        lead_id=lead.id,
        direction="outbound",
        status="ringing",
        mode="sales",
        provider="vobiz",
        channel="human",
        dialed_by=current_user.id,
        from_number=company.vobiz_phone_number,
        to_number=lead.phone,
        started_at=datetime.utcnow(),
    )
    db.add(call_log)
    await db.commit()
    await db.refresh(call_log)

    answer_url = (
        f"{_get_base_url()}/api/v1/human-calls/bridge-answer"
        f"?call_log_id={call_log.id}&lead_phone={lead.phone}"
    )
    hangup_url = f"{_get_base_url()}/api/v1/human-calls/bridge-hangup?call_log_id={call_log.id}"

    # NOTE: deliberately NOT using vobiz_service.make_outbound_call() here —
    # that helper hardcodes answer_url to the AI pipeline route
    # (/vobiz-stream/answer-stream). This call needs OUR bridge-answer XML
    # instead, so it talks to the Vobiz REST API directly with the same
    # request shape make_outbound_call uses internally.
    import httpx
    from app.services.telephony.vobiz_service import VOBIZ_BASE
    call_uuid = None
    try:
        async with httpx.AsyncClient(
            base_url=VOBIZ_BASE,
            headers={"X-Auth-ID": company.vobiz_auth_id, "X-Auth-Token": company.vobiz_auth_token, "Content-Type": "application/json"},
            timeout=20.0,
        ) as client:
            resp = await client.post(
                f"/Account/{company.vobiz_auth_id}/Call/",
                json={
                    "from": company.vobiz_phone_number,
                    "to": data.agent_phone,
                    "answer_url": answer_url,
                    "answer_method": "POST",
                    "hangup_url": hangup_url,
                    "hangup_method": "POST",
                },
            )
            resp.raise_for_status()
            call_uuid = resp.json().get("request_uuid") or resp.json().get("call_uuid")
    except Exception as e:
        logger.error(f"Human dial failed | lead={lead.id} | {e}")
        call_log.status = "failed"
        await db.commit()
        raise HTTPException(502, "Could not place the call — check your Vobiz credentials/balance and try again.")

    call_log.call_control_id = call_uuid
    await db.commit()

    # BUG FIX: Batch.status never moved off "draft"/"scheduled" for human
    # batches — nothing was watching for a human agent actually starting
    # to work one, unlike AI batches where the dispatch task flips this.
    # Flip to "running" (and stamp started_at) the first time any lead in
    # this batch is actually dialed, so the Batches tab reflects reality.
    r_bl = await db.execute(
        select(Batch).join(BatchLead, BatchLead.batch_id == Batch.id).where(
            BatchLead.lead_id == lead.id,
            Batch.company_id == company.id,
            Batch.agent_type == "human",
            Batch.status.in_(["draft", "scheduled"]),
        )
    )
    for batch in r_bl.scalars().all():
        batch.status = "running"
        if not batch.started_at:
            batch.started_at = datetime.utcnow()
    await db.commit()

    return {"call_log_id": call_log.id, "call_uuid": call_uuid, "status": "ringing"}


# ─────────────────────────────────────────────────────────────────────────────
# POST /human-calls/bridge-answer — Vobiz XML: agent's leg answered, bridge to lead
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/bridge-answer")
async def bridge_answer(call_log_id: str, lead_phone: str):
    """Vobiz hits this once the AGENT picks up. Plivo-compatible XML —
    <Dial> bridges the now-live agent leg straight to the lead's number,
    using the same caller ID the company already dials out with.

    BUG FIX (Aug 2026): <Dial> requires a nested <Number> element —
    <Dial>{phone}</Dial> with the number as bare text content is NOT
    valid Plivo/Vobiz XML (confirmed against Plivo's own XML reference:
    "You must nest a Number or User element within the Dial element").
    Vobiz received that malformed instruction, couldn't execute it, and
    hung up almost immediately — this is what was causing the call to
    drop 1-2 seconds after the agent answered."""
    async with AsyncSessionLocal() as db:
        call_log = await db.get(CallLog, call_log_id)
        if call_log:
            call_log.status = "in_progress"
            await db.commit()

    xml = f'<Response><Dial><Number>{lead_phone}</Number></Dial></Response>'
    return Response(content=xml, media_type="text/xml")


# ─────────────────────────────────────────────────────────────────────────────
# POST /human-calls/bridge-hangup — Vobiz hangup webhook, records duration
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/bridge-hangup")
async def bridge_hangup(call_log_id: str):
    async with AsyncSessionLocal() as db:
        call_log = await db.get(CallLog, call_log_id)
        if not call_log:
            return {"ok": True}
        if call_log.status not in ("completed", "failed", "no_answer"):
            call_log.ended_at = datetime.utcnow()
            call_log.status = "completed" if call_log.started_at else "no_answer"
            if call_log.started_at:
                call_log.duration_seconds = int((call_log.ended_at - call_log.started_at).total_seconds())
                company = await db.get(Company, call_log.company_id)
                if company and call_log.duration_seconds > 0:
                    plan_service.record_minutes_used(company, call_log.duration_seconds)
            await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# POST /human-calls/{call_log_id}/complete — the post-call dialog submission
# ─────────────────────────────────────────────────────────────────────────────
class CompleteCallRequest(BaseModel):
    lead_status: str        # new value for Lead.status
    summary: str
    notes: Optional[str] = None


@router.post("/{call_log_id}/complete")
async def complete_call(
    call_log_id: str,
    data: CompleteCallRequest,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    call_log = await db.get(CallLog, call_log_id)
    if not call_log or call_log.company_id != company.id:
        raise HTTPException(404, "Call not found")

    call_log.summary = data.summary
    call_log.lead_status_after = data.lead_status
    if data.notes:
        call_log.transcript = data.notes   # same field AI notes land in — keeps Call Log detail view uniform
    if call_log.status not in ("completed", "failed", "no_answer"):
        call_log.status = "completed"
        if not call_log.ended_at:
            call_log.ended_at = datetime.utcnow()
            if call_log.started_at and not call_log.duration_seconds:
                call_log.duration_seconds = int((call_log.ended_at - call_log.started_at).total_seconds())
                if call_log.duration_seconds > 0:
                    plan_service.record_minutes_used(company, call_log.duration_seconds)

    if call_log.lead_id:
        lead = await db.get(Lead, call_log.lead_id)
        if lead:
            lead.status = data.lead_status
            lead.call_attempts = (lead.call_attempts or 0) + 1
            lead.last_called_at = datetime.utcnow()
            if data.notes:
                lead.notes = data.notes

        # Mark this lead processed on whichever human batch it came from,
        # so it drops off the default (unprocessed-first) Human Call list.
        r = await db.execute(
            select(BatchLead).join(Batch, BatchLead.batch_id == Batch.id).where(
                BatchLead.lead_id == call_log.lead_id,
                Batch.company_id == company.id,
                Batch.agent_type == "human",
                BatchLead.processed == False,
            )
        )
        touched_batch_ids = set()
        for bl in r.scalars().all():
            bl.processed = True
            bl.processed_at = datetime.utcnow()
            bl.result = "success"
            touched_batch_ids.add(bl.batch_id)

        # BUG FIX: Batch.status never moved to "completed" for human
        # batches either — nothing checked whether every lead had been
        # worked. Once the last unprocessed BatchLead for a batch is
        # cleared above, flip that batch to "completed" too. autoflush
        # (SQLAlchemy's default) means the processed=True writes above
        # are visible to this count query even before the outer commit.
        for batch_id in touched_batch_ids:
            remaining = await db.execute(
                select(func.count()).select_from(BatchLead).where(
                    BatchLead.batch_id == batch_id,
                    BatchLead.processed == False,
                )
            )
            if remaining.scalar() == 0:
                batch = await db.get(Batch, batch_id)
                if batch and batch.status != "completed":
                    batch.status = "completed"
                    batch.completed_at = datetime.utcnow()

    await db.commit()
    return {"success": True}
