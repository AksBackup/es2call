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
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, AsyncSessionLocal
from app.core.rate_limit import rate_limit
from app.core.security import get_current_active_user
from app.models.models import Batch, BatchLead, CallLog, Company, Lead
from app.services import plan_service
from app.services.telephony.vobiz_service import _get_base_url
from app.services.telephony.telnyx_service import enforce_webhook_signature, telnyx_service
from app.utils.phone import INDIA_PREFIX

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
        "leads": [_lead_dict(lead, batch.name) | {"batch_id": batch.id, "dialed": bool(bl.processed), "provider": (batch.provider or "vobiz")} for bl, lead, batch in rows],
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /human-calls/dial — rings the agent's own phone, bridges to the lead
# ─────────────────────────────────────────────────────────────────────────────
class DialRequest(BaseModel):
    lead_id: str
    agent_phone: str   # the agent's own number — the carrier rings this first
    # vobiz | telnyx. Optional: defaults to the provider of the human batch
    # this lead is queued in, else vobiz.
    provider: Optional[str] = None


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

    provider = (data.provider or "").lower()
    if provider not in ("vobiz", "telnyx"):
        r_pb = await db.execute(
            select(Batch.provider).join(BatchLead, BatchLead.batch_id == Batch.id).where(
                BatchLead.lead_id == lead.id, Batch.company_id == company.id,
                Batch.agent_type == "human",
            ).order_by(Batch.created_at.desc())
        )
        provider = (r_pb.scalars().first() or "vobiz").lower()
    if provider == "telnyx":
        return await _dial_telnyx(data, company, lead, current_user, db)

    if not company.vobiz_auth_id or not company.vobiz_auth_token or not company.vobiz_phone_number:
        raise HTTPException(400, "Vobiz isn't configured for this company yet — set it up in Settings first.")

    # BUG FIX: Vobiz needs an E.164-style number (country code included) to
    # actually place a call — every other number in this system already
    # comes in that shape (lead.phone is normalized to "+91..." on import,
    # see leads.py; company.vobiz_phone_number is stored as E.164 too, see
    # models.py). This endpoint was passing the agent's phone straight
    # through as whatever bare digits the frontend collected (a 10-digit
    # Indian mobile, no "+91"). Vobiz's Call-creation API still accepts
    # that request and returns a call_uuid — it doesn't reject it outright
    # — it just never actually rings an unqualified number, so nothing
    # ever throws and nothing ever gets logged as an error; the agent's
    # phone simply stays silent. Normalize it the same way here.
    agent_digits = re.sub(r"\D", "", data.agent_phone)
    if len(agent_digits) != 10:
        raise HTTPException(400, "Enter a valid 10-digit agent phone number")
    agent_phone_e164 = f"{INDIA_PREFIX}{agent_digits}"

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
        # BUG FIX: lead.phone is E.164 ("+919140971036") and was being
        # dropped straight into the query string unescaped. A literal "+"
        # in a URL query string is the standard encoding for a space —
        # Vobiz's own outbound request round-trips it fine (the "+" you
        # see in this file's log line is just the raw request line), but
        # when Vobiz calls this URL back on bridge-answer, FastAPI/
        # Starlette's query-string parser decodes that raw "+" into a
        # space, so `lead_phone` arrives here as " 919140971036" — no
        # "+", a leading space instead. The <Dial><Number> then tries to
        # ring that mangled, invalid number, the second leg never
        # connects, and Vobiz drops the whole bridge a couple seconds
        # after the agent picks up — exactly the "connects then hangs up
        # in 2-3s" symptom. quote(..., safe='') percent-encodes the "+"
        # to "%2B", which decodes back to a literal "+" correctly.
        f"{_get_base_url()}/api/v1/human-calls/bridge-answer"
        f"?call_log_id={call_log.id}&lead_phone={quote(lead.phone, safe='')}"
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
                    "to": agent_phone_e164,
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
# Telnyx click-to-call bridge
#   1. /dial  -> Telnyx rings the AGENT's phone (leg "agent")
#   2. agent answers -> call.answered webhook (leg=agent) -> we issue a
#      Telnyx `transfer` that dials the lead (leg B) and bridges it
#   3. lead answers -> call.answered webhook (leg=lead) -> status in_progress,
#      billing clock starts
#   4. either side hangs up -> call.hangup -> duration + plan minutes
# CallLog.call_control_id = the AGENT leg's call_control_id.
# ─────────────────────────────────────────────────────────────────────────────
def _agent_number_e164(raw: str) -> str:
    """Telnyx serves any country, so accept either E.164 (+CC...) or a bare
    10-digit Indian mobile (+91 assumed, same as the Vobiz path)."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"{INDIA_PREFIX}{digits}"
    raise HTTPException(400, "Enter your phone number in international format (+CC…) or a 10-digit Indian number")


def _telnyx_leg_webhook(call_log_id: str, leg: str) -> str:
    return f"{_get_base_url()}/api/v1/human-calls/telnyx-webhook?call_log_id={call_log_id}&leg={leg}"


async def _dial_telnyx(data, company: Company, lead: Lead, current_user, db: AsyncSession):
    creds = telnyx_service._creds(company)
    if not (creds["api_key"] and creds["connection_id"] and creds["phone"]):
        raise HTTPException(400, "Telnyx isn't configured for this company yet — add API key, Call Control Application ID and phone number in Settings → Telephony.")
    agent_phone = _agent_number_e164(data.agent_phone)

    call_log = CallLog(
        company_id=company.id, lead_id=lead.id, direction="outbound",
        status="ringing", mode="sales", provider="telnyx", channel="human",
        dialed_by=current_user.id, from_number=creds["phone"], to_number=lead.phone,
        started_at=datetime.utcnow(),
    )
    db.add(call_log)
    await db.commit()
    await db.refresh(call_log)

    ccid = await telnyx_service.dial_agent_leg(
        agent_phone, company, _telnyx_leg_webhook(call_log.id, "agent"),
    )
    if not ccid:
        call_log.status = "failed"
        await db.commit()
        raise HTTPException(502, "Could not place the call — check your Telnyx API key, application ID, outbound voice profile and balance.")
    call_log.call_control_id = ccid
    await db.commit()

    # Same batch-status flip the Vobiz path does on first dial.
    r_bl = await db.execute(
        select(Batch).join(BatchLead, BatchLead.batch_id == Batch.id).where(
            BatchLead.lead_id == lead.id, Batch.company_id == company.id,
            Batch.agent_type == "human", Batch.status.in_(["draft", "scheduled"]),
        )
    )
    for batch in r_bl.scalars().all():
        batch.status = "running"
        if not batch.started_at:
            batch.started_at = datetime.utcnow()
    await db.commit()
    return {"call_log_id": call_log.id, "call_uuid": ccid, "status": "ringing", "provider": "telnyx"}


@router.post("/telnyx-webhook")
async def telnyx_bridge_webhook(call_log_id: str, request: Request, leg: str = "agent"):
    raw = await request.body()
    try:
        import json as _json
        body = _json.loads(raw or b"{}")
    except Exception:
        return {"ok": True}
    data = body.get("data") or {}
    event_type = data.get("event_type") or ""
    payload = data.get("payload") or {}
    ccid = payload.get("call_control_id") or ""

    async with AsyncSessionLocal() as db:
        call_log = await db.get(CallLog, call_log_id)
        if not call_log:
            return {"ok": True}
        company = await db.get(Company, call_log.company_id)
        enforce_webhook_signature(raw, request.headers, company)
        logger.info(f"Human call (telnyx) | {event_type} | leg={leg} | call_log={call_log_id}")

        if event_type == "call.answered" and leg == "agent":
            # Agent picked up -> dial the lead and bridge.
            ok = await telnyx_service.bridge_to_lead(
                call_log.call_control_id or ccid, call_log.to_number, company,
                _telnyx_leg_webhook(call_log_id, "lead"),
            )
            if not ok:
                logger.error(f"Human call (telnyx) bridge-to-lead failed | call_log={call_log_id}")
                await telnyx_service.hangup(call_log.call_control_id or ccid, company)

        elif event_type == "call.answered" and leg == "lead":
            # Lead picked up — conversation starts NOW; billing clock too.
            if call_log.status == "ringing":
                call_log.status = "in_progress"
                call_log.started_at = datetime.utcnow()
                await db.commit()

        elif event_type == "call.hangup":
            cause = payload.get("hangup_cause") or ""
            if call_log.status in ("completed", "failed", "no_answer"):
                return {"ok": True}
            # Lead leg failed/unanswered before the bridge: Telnyx keeps the
            # agent leg alive, so hang it up ourselves.
            if leg == "lead" and call_log.status == "ringing":
                await telnyx_service.hangup(call_log.call_control_id or "", company)
            call_log.ended_at = datetime.utcnow()
            if call_log.status == "in_progress":
                call_log.status = "completed"
                call_log.duration_seconds = int((call_log.ended_at - call_log.started_at).total_seconds())
                if company and call_log.duration_seconds > 0:
                    plan_service.record_minutes_used(company, call_log.duration_seconds)
            else:
                call_log.status = "no_answer"   # agent or lead never connected
                call_log.duration_seconds = 0
            logger.info(f"Human call (telnyx) ended | call_log={call_log_id} | leg={leg} | cause={cause} | status={call_log.status}")
            await db.commit()
    return {"ok": True}


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
    drop 1-2 seconds after the agent answered.

    BUG FIX 2 (Aug 2026): the docstring above always claimed this uses
    "the same caller ID the company already dials out with", but the XML
    never actually set one — <Dial> had no `callerId` attribute. Without
    an explicit, registered caller ID, Vobiz/Indian-carrier DLT rules
    commonly reject or immediately drop an outbound leg with no caller ID
    attached. That's why the bridge could connect (agent answers, this
    endpoint returns 200) and then hang up again within a couple of
    seconds the moment Vobiz tried to actually Dial the lead — the second
    leg never went through. Explicitly setting callerId to the company's
    own registered Vobiz number (the same one already used as `from` on
    the original outbound leg) fixes that."""
    async with AsyncSessionLocal() as db:
        call_log = await db.get(CallLog, call_log_id)
        caller_id = None
        if call_log:
            call_log.status = "in_progress"
            company = await db.get(Company, call_log.company_id)
            if company:
                caller_id = company.vobiz_phone_number
            await db.commit()

    caller_id_attr = f' callerId="{caller_id}"' if caller_id else ''
    xml = f'<Response><Dial{caller_id_attr}><Number>{lead_phone}</Number></Dial></Response>'
    return Response(content=xml, media_type="text/xml")


# ─────────────────────────────────────────────────────────────────────────────
# POST /human-calls/bridge-hangup — Vobiz hangup webhook, records duration
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/bridge-hangup")
async def bridge_hangup(call_log_id: str, request: Request):
    # DIAGNOSTIC (Aug 2026): log whatever Vobiz sends on hangup — Plivo-
    # compatible providers typically include a cause code/reason (e.g.
    # HangupCause, HangupCauseName, CallStatus) in the POST body. This
    # endpoint never looked at it before, so every hangup — expected or a
    # failed bridge — logged identically as a bare 200 OK with no way to
    # tell why a call actually ended. Logging it here costs nothing and
    # means the NEXT log capture will show the real reason if a call still
    # drops early after the callerId fix above.
    try:
        form = await request.form()
        if form:
            logger.info(f"Human call bridge-hangup | call_log_id={call_log_id} | payload={dict(form)}")
    except Exception:
        pass

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