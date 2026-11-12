from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.models import Appointment, Company, Lead

router = APIRouter()


async def _company(user_id: str, db: AsyncSession) -> Company:
    r = await db.execute(select(Company).where(Company.owner_id == user_id))
    company = r.scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")
    return company


def _dict(a: Appointment) -> dict:
    return {
        "id": a.id,
        "company_id": a.company_id,
        "lead_id": a.lead_id,
        "appointment_type": a.appointment_type,
        "status": a.status,
        "product": a.product,
        "location": a.location,
        "scheduled_at": a.scheduled_at,
        "duration_minutes": a.duration_minutes,
        "notes": a.notes,
        "created_by": a.created_by,
        "source_call_id": a.source_call_id,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "lead_name": a.lead.name if a.lead else None,
        "lead_phone": a.lead.phone if a.lead else None,
    }


class AppointmentCreate(BaseModel):
    lead_id: str
    appointment_type: str = "site_visit"
    product: Optional[str] = None
    location: Optional[str] = None
    scheduled_at: datetime
    duration_minutes: int = 30
    notes: Optional[str] = None
    status: str = "confirmed"


class AppointmentUpdate(BaseModel):
    status: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    notes: Optional[str] = None
    appointment_type: Optional[str] = None
    product: Optional[str] = None
    location: Optional[str] = None
    duration_minutes: Optional[int] = None


async def _load_appointment(appointment_id: str, company_id: str, db: AsyncSession) -> Appointment:
    r = await db.execute(
        select(Appointment)
        .options(selectinload(Appointment.lead))
        .where(Appointment.id == appointment_id, Appointment.company_id == company_id)
    )
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "Appointment not found")
    return a


@router.get("/")
async def list_appointments(
    status: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    conds = [Appointment.company_id == company.id]
    if status:
        conds.append(Appointment.status == status)
    if from_date:
        conds.append(Appointment.scheduled_at >= from_date)
    if to_date:
        conds.append(Appointment.scheduled_at <= to_date)
    r = await db.execute(
        select(Appointment)
        .options(selectinload(Appointment.lead))
        .where(and_(*conds))
        .order_by(Appointment.scheduled_at.asc())
    )
    return {"appointments": [_dict(a) for a in r.scalars().all()]}


@router.post("/")
async def create_appointment(
    data: AppointmentCreate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    lead_result = await db.execute(
        select(Lead).where(Lead.id == data.lead_id, Lead.company_id == company.id)
    )
    lead = lead_result.scalar_one_or_none()
    if not lead:
        raise HTTPException(404, "Lead not found")
    if data.duration_minutes < 5 or data.duration_minutes > 480:
        raise HTTPException(422, "Duration must be between 5 and 480 minutes")
    allowed_types = {"site_visit", "office_meeting", "product_demo", "callback", "other"}
    if data.appointment_type not in allowed_types:
        raise HTTPException(422, "Invalid appointment type")

    appointment = Appointment(
        company_id=company.id,
        lead_id=lead.id,
        appointment_type=data.appointment_type,
        status="confirmed",
        product=data.product,
        location=data.location,
        scheduled_at=data.scheduled_at,
        duration_minutes=data.duration_minutes,
        notes=data.notes,
        created_by="admin",
    )
    db.add(appointment)

    # Keep the existing Lead Notes as the simple, persistent hand-off point.
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    note = (
        f"[{stamp}] MANUAL APPOINTMENT SCHEDULED: {data.appointment_type.replace('_', ' ')}"
        f" | {data.scheduled_at.isoformat()}"
        f" | Product: {data.product or 'not specified'}"
        f" | Location: {data.location or 'not specified'}"
        f" | Admin note: {data.notes or 'none'}"
    )
    lead.notes = f"{lead.notes}\n{note}".strip() if lead.notes else note
    lead.updated_at = datetime.utcnow()

    await db.commit()
    a = await _load_appointment(appointment.id, company.id, db)
    return _dict(a)


@router.patch("/{appointment_id}")
async def update_appointment(
    appointment_id: str,
    data: AppointmentUpdate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    a = await _load_appointment(appointment_id, company.id, db)
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(a, k, v)
    a.updated_at = datetime.utcnow()
    await db.commit()
    a = await _load_appointment(appointment_id, company.id, db)
    return _dict(a)


@router.post("/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    a = await _load_appointment(appointment_id, company.id, db)
    a.status = "cancelled"
    a.updated_at = datetime.utcnow()
    await db.commit()
    a = await _load_appointment(appointment_id, company.id, db)
    return _dict(a)
