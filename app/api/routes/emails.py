from datetime import datetime
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.models import Batch, BatchLead, Company, EmailLog, Lead

router = APIRouter()


class SendRequest(BaseModel):
    lead_id: str
    subject: str
    body_text: str
    batch_id: Optional[str] = None


class BatchSendRequest(BaseModel):
    batch_id: str
    subject_template: str
    body_template: str


class ApproveRequest(BaseModel):
    email_log_id: str
    edited_body: Optional[str] = None


class ManualReplyRequest(BaseModel):
    email_log_id: str
    reply_body: str


def _dict(l: EmailLog) -> dict:
    return {
        "id": l.id, "lead_id": l.lead_id, "batch_id": l.batch_id,
        "to_email": l.to_email, "to_name": l.to_name,
        "subject": l.subject, "status": l.status,
        "sent_at": l.sent_at, "opened_at": l.opened_at, "replied_at": l.replied_at,
        "reply_status": l.reply_status, "reply_body": l.reply_body,
        "ai_reply_draft": l.ai_reply_draft, "ai_reply_confidence": l.ai_reply_confidence,
        "ai_reply_sent": l.ai_reply_sent, "reply_sentiment": l.reply_sentiment,
        "reply_intent": l.reply_intent, "email_thread": l.email_thread,
        "created_at": l.created_at,
    }


async def _company(user_id: str, db: AsyncSession) -> Company:
    r = await db.execute(select(Company).where(Company.owner_id == user_id))
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Company not found")
    return c


@router.get("/logs")
async def list_logs(
    batch_id: Optional[str] = None,
    status: Optional[str] = None,
    reply_status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    conds = [EmailLog.company_id == company.id]
    if batch_id:
        conds.append(EmailLog.batch_id == batch_id)
    if status:
        conds.append(EmailLog.status == status)
    if reply_status:
        conds.append(EmailLog.reply_status == reply_status)
    r = await db.execute(
        select(EmailLog).where(and_(*conds))
        .order_by(EmailLog.created_at.desc()).limit(limit).offset(offset)
    )
    return [_dict(l) for l in r.scalars().all()]


@router.get("/queue")
async def review_queue(
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Emails where AI reply needs human approval."""
    company = await _company(current_user.id, db)
    r = await db.execute(
        select(EmailLog).where(
            EmailLog.company_id == company.id,
            EmailLog.reply_status == "queued_for_review",
        ).order_by(EmailLog.replied_at.desc())
    )
    return [_dict(l) for l in r.scalars().all()]


@router.get("/stats")
async def email_stats(
    batch_id: Optional[str] = None,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    conds = [EmailLog.company_id == company.id]
    if batch_id:
        conds.append(EmailLog.batch_id == batch_id)
    r = await db.execute(select(EmailLog).where(and_(*conds)))
    logs = r.scalars().all()
    sent = sum(1 for l in logs if l.status in ("sent","delivered","opened","replied"))
    return {
        "total": len(logs),
        "sent": sent,
        "opened": sum(1 for l in logs if l.status == "opened"),
        "replied": sum(1 for l in logs if l.status == "replied"),
        "bounced": sum(1 for l in logs if l.status == "bounced"),
        "pending_review": sum(1 for l in logs if l.reply_status == "queued_for_review"),
        "ai_auto_replied": sum(1 for l in logs if l.ai_reply_sent),
        "open_rate": round(sum(1 for l in logs if l.status == "opened") / max(sent, 1) * 100, 1),
        "reply_rate": round(sum(1 for l in logs if l.status == "replied") / max(sent, 1) * 100, 1),
    }


@router.post("/send")
async def send_single(
    data: SendRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.email.email_service import email_service
    company = await _company(current_user.id, db)
    r = await db.execute(select(Lead).where(Lead.id == data.lead_id, Lead.company_id == company.id))
    lead = r.scalar_one_or_none()
    if not lead or not lead.email:
        raise HTTPException(400, "Lead not found or has no email")
    if lead.email_opt_out:
        raise HTTPException(400, "Lead has opted out of emails")

    log = EmailLog(
        company_id=company.id, lead_id=lead.id, batch_id=data.batch_id,
        to_email=lead.email, to_name=lead.name, subject=data.subject,
        body_text=data.body_text,
        from_email=company.email_from_address, from_name=company.email_from_name,
        status="queued",
        email_thread=[{"role": "ai", "body": data.body_text, "sent_at": datetime.utcnow().isoformat()}],
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)

    async def _send():
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
            _log = await _db.get(EmailLog, log.id)
            _lead = await _db.get(Lead, lead.id)
            success, msg_id = await email_service.send(
                to_email=_log.to_email, to_name=_log.to_name or "",
                subject=_log.subject, body_text=_log.body_text or "",
                from_email=_log.from_email, from_name=_log.from_name,
                email_log_id=_log.id,
            )
            _log.status = "sent" if success else "failed"
            _log.sendgrid_message_id = msg_id
            _log.sent_at = datetime.utcnow()
            if _lead:
                _lead.last_emailed_at = datetime.utcnow()
                _lead.email_attempts = (_lead.email_attempts or 0) + 1
                _lead.email_status = "sent" if success else _lead.email_status
            await _db.commit()

    background_tasks.add_task(_send)
    return {"email_log_id": log.id, "status": "queued"}


@router.post("/batch-send")
async def batch_send(
    data: BatchSendRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Batch).where(Batch.id == data.batch_id, Batch.company_id == company.id))
    batch = r.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")

    leads_r = await db.execute(
        select(BatchLead, Lead).join(Lead, BatchLead.lead_id == Lead.id)
        .where(
            BatchLead.batch_id == data.batch_id,
            BatchLead.processed == False,
            Lead.email != None,
            Lead.email_opt_out == False,
            Lead.is_active == True,
        )
    )
    rows = leads_r.all()
    queued = 0

    for row in rows:
        lead = row.Lead
        subject = _render(data.subject_template, lead, company, batch)
        body    = _render(data.body_template, lead, company, batch)

        log = EmailLog(
            company_id=company.id, lead_id=lead.id, batch_id=batch.id,
            to_email=lead.email, to_name=lead.name,
            subject=subject, body_text=body,
            from_email=company.email_from_address, from_name=company.email_from_name,
            status="queued",
            email_thread=[{"role": "ai", "body": body, "sent_at": datetime.utcnow().isoformat()}],
        )
        db.add(log)
        queued += 1

    await db.commit()
    background_tasks.add_task(_trigger_batch_send, data.batch_id)
    return {"queued": queued}


@router.post("/approve-reply")
async def approve_reply(
    data: ApproveRequest,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.email.email_service import email_service
    company = await _company(current_user.id, db)
    r = await db.execute(
        select(EmailLog).where(EmailLog.id == data.email_log_id, EmailLog.company_id == company.id)
    )
    log = r.scalar_one_or_none()
    if not log:
        raise HTTPException(404, "Email log not found")

    body = data.edited_body or log.ai_reply_draft
    if not body:
        raise HTTPException(400, "No reply body")

    await email_service.send(
        to_email=log.to_email, to_name=log.to_name or "",
        subject=f"Re: {log.subject}", body_text=body,
        from_email=log.from_email, from_name=log.from_name,
        email_log_id=log.id,
    )
    log.reply_status    = "human_replied"
    log.ai_reply_sent   = True
    log.ai_reply_sent_at = datetime.utcnow()
    thread = log.email_thread or []
    thread.append({"role": "ai", "body": body, "sent_at": datetime.utcnow().isoformat(), "source": "human_approved"})
    log.email_thread = thread
    await db.commit()
    return {"sent": True}


@router.post("/poll-replies")
async def poll_replies(
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_active_user),
):
    from app.tasks.tasks import poll_all_company_replies
    background_tasks.add_task(lambda: poll_all_company_replies.delay())
    return {"message": "IMAP poll triggered"}


def _render(template: str, lead: Lead, company: Company, batch: Optional[Batch] = None) -> str:
    return template.format(
        lead_name=lead.name.split()[0] if lead.name else "ji",
        full_name=lead.name or "",
        company_name=company.name,
        agent_name=company.agent_name or "Aria",
        product_name=getattr(batch, "product_focus", None) or company.active_product or "our services",
    )


async def _trigger_batch_send(batch_id: str):
    from app.tasks.tasks import _dispatch_email_batch
    _dispatch_email_batch.delay(batch_id, "")
