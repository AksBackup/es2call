import csv
import io
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.models import Company, Lead

router = APIRouter()

VALID_STATUSES = ["new","contacted","called","interested","warm","cold",
                   "closed_won","closed_lost","do_not_call","human_callback_requested"]


class LeadCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    status: str = "new"
    source: str = "manual"
    language: str = "hinglish"
    timezone: str = "Asia/Kolkata"
    notes: Optional[str] = None
    campaign_name: Optional[str] = None


class LeadUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    notes: Optional[str] = None
    interest_level: Optional[float] = None
    email_opt_out: Optional[bool] = None
    campaign_name: Optional[str] = None
    follow_up_question: Optional[str] = None


def _lead_dict(l: Lead) -> dict:
    return {
        "id": l.id, "name": l.name, "phone": l.phone, "email": l.email,
        "status": l.status, "interest_level": l.interest_level,
        "source": l.source, "language": l.language, "timezone": l.timezone,
        "notes": l.notes, "key_info": l.key_info,
        "follow_up_question": l.follow_up_question,
        "call_attempts": l.call_attempts, "last_called_at": l.last_called_at,
        "email_status": l.email_status, "email_attempts": l.email_attempts,
        "last_emailed_at": l.last_emailed_at, "email_opt_out": l.email_opt_out,
        "campaign_name": l.campaign_name, "scheduled_call_at": l.scheduled_call_at,
        "is_active": l.is_active, "created_at": l.created_at, "updated_at": l.updated_at,
    }


async def _company(user_id: str, db: AsyncSession) -> Company:
    r = await db.execute(select(Company).where(Company.owner_id == user_id))
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Company not found. Please set up your company first.")
    return c


@router.get("/")
async def list_leads(
    status: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    conds = [Lead.company_id == company.id, Lead.is_active == True]
    if status:
        statuses = [s.strip() for s in status.split(",")]
        conds.append(Lead.status.in_(statuses))
    if source:
        conds.append(Lead.source == source)
    if language:
        conds.append(Lead.language == language)
    if search:
        like = f"%{search}%"
        from sqlalchemy import or_
        conds.append(or_(Lead.name.ilike(like), Lead.phone.ilike(like), Lead.email.ilike(like)))

    q      = select(Lead).where(and_(*conds)).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    total_q = select(func.count()).select_from(Lead).where(and_(*conds))
    result  = await db.execute(q)
    total   = (await db.execute(total_q)).scalar()
    leads   = result.scalars().all()
    return {"total": total, "leads": [_lead_dict(l) for l in leads]}


@router.post("/")
async def create_lead(
    data: LeadCreate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    lead = Lead(company_id=company.id, **data.model_dump())
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return _lead_dict(lead)


@router.get("/stats")
async def lead_stats(
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Lead).where(Lead.company_id == company.id, Lead.is_active == True))
    leads = r.scalars().all()
    stats = {s: 0 for s in VALID_STATUSES}
    for l in leads:
        if l.status in stats:
            stats[l.status] += 1
    return {"total": len(leads), "by_status": stats}


@router.get("/{lead_id}")
async def get_lead(
    lead_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Lead).where(Lead.id == lead_id, Lead.company_id == company.id))
    lead = r.scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "Lead not found")
    return _lead_dict(lead)


@router.patch("/{lead_id}")
async def update_lead(
    lead_id: str,
    data: LeadUpdate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Lead).where(Lead.id == lead_id, Lead.company_id == company.id))
    lead = r.scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "Lead not found")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(lead, k, v)
    lead.updated_at = datetime.utcnow()
    await db.commit()
    return _lead_dict(lead)


@router.delete("/{lead_id}")
async def delete_lead(
    lead_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(Lead).where(Lead.id == lead_id, Lead.company_id == company.id))
    lead = r.scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "Lead not found")
    lead.is_active = False
    await db.commit()
    return {"deleted": True}


@router.post("/import/csv")
async def import_csv(
    file: UploadFile = File(...),
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Import leads from CSV.
    Required columns: name, phone
    Optional: email, status, source, language, timezone, notes, campaign_name
    """
    company = await _company(current_user.id, db)
    content = await file.read()
    text = content.decode("utf-8-sig")  # handle BOM
    reader = csv.DictReader(io.StringIO(text))

    imported = 0
    skipped  = 0
    errors   = []

    for i, row in enumerate(reader, 1):
        name  = (row.get("name") or row.get("Name") or "").strip()
        phone = (row.get("phone") or row.get("Phone") or row.get("mobile") or "").strip()
        if not name or not phone:
            errors.append(f"Row {i}: missing name or phone")
            skipped += 1
            continue

        # Normalize phone — add +91 if needed
        if phone.startswith("0"):
            phone = "+91" + phone[1:]
        elif phone.isdigit() and len(phone) == 10:
            phone = "+91" + phone
        elif not phone.startswith("+"):
            phone = "+" + phone

        # Check duplicate
        dup = await db.execute(select(Lead).where(Lead.phone == phone, Lead.company_id == company.id))
        if dup.scalar_one_or_none():
            skipped += 1
            continue

        status   = (row.get("status") or "new").strip().lower()
        if status not in VALID_STATUSES:
            status = "new"

        lead = Lead(
            company_id   = company.id,
            name         = name,
            phone        = phone,
            email        = (row.get("email") or "").strip() or None,
            status       = status,
            source       = (row.get("source") or "csv").strip(),
            language     = (row.get("language") or "hinglish").strip(),
            timezone     = (row.get("timezone") or "Asia/Kolkata").strip(),
            notes        = (row.get("notes") or "").strip() or None,
            campaign_name = (row.get("campaign_name") or "").strip() or None,
        )
        db.add(lead)
        imported += 1

    await db.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors[:20]}
