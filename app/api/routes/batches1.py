from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, and_, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.models import Batch, BatchLead, Company, Lead, Schedule

router = APIRouter()
logger = logging.getLogger(__name__)


class FilterCriteria(BaseModel):
    status: Optional[List[str]] = None
    source: Optional[str] = None
    lead_ids: Optional[List[str]] = None
    language: Optional[str] = None
    country_code: Optional[str] = None    # "+91" | "other" — filter leads by phone prefix
    exclude_statuses: Optional[List[str]] = ["do_not_call", "closed_won", "closed_lost"]
    limit: Optional[int] = None


class BatchCreate(BaseModel):
    name: str
    description: Optional[str] = None
    batch_type: str                       # voice | email
    call_mode: str = "sales"             # sales | support
    provider: str = "vobiz"               # vobiz — sole carrier dispatching this batch
    # ai = existing fully-automated flow. human = leads land in the Human
    # Call tab instead of being auto-dialed — see app/api/routes/human_calls.py
    # and Batch.agent_type in models.py. Only meaningful for batch_type="voice".
    agent_type: str = "ai"
    filter_criteria: FilterCriteria
    campaign_name: Optional[str] = None
    product_focus: Optional[str] = None
    email_subject_template: Optional[str] = None
    email_body_template: Optional[str] = None


def _dict(b: Batch) -> dict:
    return {
        "id": b.id, "name": b.name, "description": b.description,
        "batch_type": b.batch_type, "call_mode": b.call_mode, "provider": b.provider,
        "agent_type": b.agent_type,
        "status": b.status, "lead_count": b.lead_count,
        "leads_processed": b.leads_processed, "leads_succeeded": b.leads_succeeded,
        "leads_failed": b.leads_failed, "campaign_name": b.campaign_name,
        "product_focus": b.product_focus, "filter_criteria": b.filter_criteria,
        "email_subject_template": b.email_subject_template,
        "email_body_template": b.email_body_template,
        "started_at": b.started_at, "completed_at": b.completed_at,
        "created_at": b.created_at,
    }


async def _company(user_id: str, db: AsyncSession) -> Company:
    r = await db.execute(select(Company).where(Company.owner_id == user_id))
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Company not found")
    return c


async def _select_leads(company_id: str, f: FilterCriteria, db: AsyncSession) -> List[Lead]:
    conds = [Lead.company_id == company_id, Lead.is_active == True]
    if f.lead_ids:
        conds.append(Lead.id.in_(f.lead_ids))
    else:
        if f.status:
            conds.append(Lead.status.in_(f.status))
        if f.exclude_statuses:
            conds.append(Lead.status.notin_(f.exclude_statuses))
        if f.source:
            conds.append(Lead.source == f.source)
        if f.language:
            conds.append(Lead.language == f.language)
        if f.country_code:
            from app.utils.phone import INDIA_PREFIX
            if f.country_code == INDIA_PREFIX:
                conds.append(Lead.phone.like(f"{INDIA_PREFIX}%"))
            else:
                conds.append(~Lead.phone.like(f"{INDIA_PREFIX}%"))
    q = select(Lead).where(and_(*conds))
    if f.limit:
        q = q.limit(f.limit)
    r = await db.execute(q)
    return r.scalars().all()


def _clear_batch_redis_lock(batch_id: str):
    """Clear Redis batch lock if the batch is being force-deleted while running.
    Non-fatal — if Redis is down or key doesn't exist, deletion still proceeds."""
    try:
        import redis as redis_sync
        from app.core.config import settings as _s
        _r = redis_sync.from_url(
            getattr(_s, "REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
        _r.delete(f"batch_call_active:{batch_id}")
        _r.delete(f"batch_call_cooldown:{batch_id}")
        logger.info(f"Redis batch locks cleared for batch_id={batch_id}")
    except Exception as e:
        logger.warning(f"Redis lock clear failed (non-fatal): {e}")


@router.get("/preview")
async def preview(
    status: Optional[str] = None,
    country_code: Optional[str] = None,
    limit: int = 200,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    f = FilterCriteria(
        status=[s.strip() for s in status.split(",")] if status else None,
        country_code=country_code,
        limit=limit,
    )
    leads = await _select_leads(company.id, f, db)
    return {
        "total_matching": len(leads),
        "sample": [{"id": l.id, "name": l.name, "phone": l.phone, "status": l.status} for l in leads[:10]],
    }


@router.post("/")
async def create_batch(
    data: BatchCreate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    leads = await _select_leads(company.id, data.filter_criteria, db)
    if not leads:
        raise HTTPException(400, "No leads match the filter criteria.")

    batch = Batch(
        company_id=company.id,
        name=data.name, description=data.description,
        batch_type=data.batch_type, call_mode=data.call_mode, provider=data.provider,
        agent_type=data.agent_type,
        filter_criteria=data.filter_criteria.model_dump(),
        lead_count=len(leads),
        campaign_name=data.campaign_name,
        product_focus=data.product_focus,
        email_subject_template=data.email_subject_template,
        email_body_template=data.email_body_template,
        status="draft",
    )
    db.add(batch)
    await db.flush()

    for lead in leads:
        db.add(BatchLead(batch_id=batch.id, lead_id=lead.id))

    await db.commit()
    return {**_dict(batch), "lead_count": len(leads)}


@router.post("/{batch_id}/reuse")
async def reuse_batch(
    batch_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Clones a completed (or any) batch's settings into a brand-new
    "draft" batch — same name (with a suffix), filter criteria, product
    focus, agent_type, etc. Deliberately RE-RUNS the original
    filter_criteria against CURRENT leads rather than copying the old
    batch's BatchLead rows verbatim: a lead's status may have changed
    since (e.g. now do_not_call, or closed_won), and new leads matching
    the same filter may have been added since — a "reuse" should pick up
    a fresh, currently-accurate audience, not replay a stale snapshot."""
    company = await _company(current_user.id, db)
    r = await db.execute(select(Batch).where(Batch.id == batch_id, Batch.company_id == company.id))
    old = r.scalar_one_or_none()
    if not old:
        raise HTTPException(404, "Batch not found")

    f = FilterCriteria(**(old.filter_criteria or {}))
    leads = await _select_leads(company.id, f, db)
    if not leads:
        raise HTTPException(400, "No leads currently match this batch's filter criteria — nothing to reuse.")

    new_batch = Batch(
        company_id=company.id,
        name=f"{old.name} (reused)",
        description=old.description,
        batch_type=old.batch_type, call_mode=old.call_mode, provider=old.provider,
        agent_type=old.agent_type,
        filter_criteria=old.filter_criteria,
        lead_count=len(leads),
        campaign_name=old.campaign_name,
        product_focus=old.product_focus,
        email_subject_template=old.email_subject_template,
        email_body_template=old.email_body_template,
        status="draft",
    )
    db.add(new_batch)
    await db.flush()

    for lead in leads:
        db.add(BatchLead(batch_id=new_batch.id, lead_id=lead.id))

    await db.commit()
    return {**_dict(new_batch), "lead_count": len(leads)}


@router.get("/")
async def list_batches(
    batch_type: Optional[str] = None,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    conds = [Batch.company_id == company.id]
    if batch_type:
        conds.append(Batch.batch_type == batch_type)
    r = await db.execute(select(Batch).where(and_(*conds)).order_by(Batch.created_at.desc()))
    return [_dict(b) for b in r.scalars().all()]


@router.get("/{batch_id}")
async def get_batch(
    batch_id: str,
    sort_by: Optional[str] = None,    # "status" | "country_code"
    order: str = "asc",
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Batch).where(Batch.id == batch_id, Batch.company_id == company.id))
    batch = r.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")

    leads_r = await db.execute(
        select(BatchLead, Lead).join(Lead, BatchLead.lead_id == Lead.id)
        .where(BatchLead.batch_id == batch_id).limit(500)
    )
    from app.utils.phone import get_country_code
    leads = [
        {"id": row.Lead.id, "name": row.Lead.name, "phone": row.Lead.phone,
         "status": row.Lead.status, "processed": row.BatchLead.processed,
         "country_code": get_country_code(row.Lead.phone)}
        for row in leads_r.all()
    ]
    if sort_by in ("status", "country_code"):
        leads.sort(key=lambda l: l[sort_by], reverse=(order == "desc"))
    return {**_dict(batch), "leads": leads}


@router.patch("/{batch_id}/pause")
async def pause_batch(
    batch_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Pause a running or scheduled batch so it stops dispatching new calls."""
    company = await _company(current_user.id, db)
    r = await db.execute(select(Batch).where(Batch.id == batch_id, Batch.company_id == company.id))
    batch = r.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")
    if batch.status not in ("running", "scheduled"):
        raise HTTPException(400, f"Batch is {batch.status} — only running/scheduled batches can be paused.")

    batch.status = "paused"
    await db.commit()
    _clear_batch_redis_lock(batch_id)
    return _dict(batch)


@router.delete("/{batch_id}")
async def delete_batch(
    batch_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Batch).where(Batch.id == batch_id, Batch.company_id == company.id))
    batch = r.scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Batch not found")

    # Allow deleting any batch regardless of status.
    # If it was running, clear the Redis dispatch lock so the Celery scheduler
    # doesn't try to resume it after deletion.
    if batch.status in ("running", "scheduled"):
        logger.info(f"Force-deleting {batch.status} batch {batch_id} — clearing Redis lock")
        _clear_batch_redis_lock(batch_id)

    # Delete child rows first — SQLite has no ON DELETE CASCADE by default,
    # and neither Batch.schedules nor Batch.batch_leads has an explicit
    # cascade="all, delete-orphan" set, so SQLAlchemy's default behavior
    # on deleting the parent is to try to NULL each child's batch_id
    # instead of deleting the child. That's silently fine for
    # EmailLog.batch_id (nullable=True) but hard-crashes for
    # Schedule.batch_id, which is nullable=False — this is exactly what
    # was happening: deleting any batch that had a schedule attached
    # threw "NOT NULL constraint failed: schedules.batch_id".
    await db.execute(delete(Schedule).where(Schedule.batch_id == batch_id))
    await db.execute(delete(BatchLead).where(BatchLead.batch_id == batch_id))
    await db.delete(batch)
    await db.commit()
    return {"deleted": True}