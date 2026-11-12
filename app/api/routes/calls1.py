from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.models import CallLog, Company

router = APIRouter()


def _dict(c: CallLog) -> dict:
    return {
        "id": c.id,
        "lead_id": c.lead_id,
        "direction": c.direction,
        "status": c.status,
        "mode": c.mode,
        "channel": c.channel,
        "dialed_by": c.dialed_by,
        "from_number": c.from_number,
        "to_number": c.to_number,
        "call_control_id": c.call_control_id,
        "started_at": c.started_at,
        "ended_at": c.ended_at,
        "duration_seconds": c.duration_seconds,
        "transcript": c.transcript,
        "summary": c.summary,
        "sentiment": c.sentiment,
        "intent": c.intent,
        "lead_status_after": c.lead_status_after,
        "transferred_to_human": c.transferred_to_human,
        "recording_url": c.recording_url,
        "created_at": c.created_at,
    }


async def _company(user_id: str, db: AsyncSession) -> Company:
    from fastapi import HTTPException
    r = await db.execute(select(Company).where(Company.owner_id == user_id))
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Company not found")
    return c


@router.get("/")
async def list_calls(
    direction: Optional[str] = None,
    status: Optional[str] = None,
    lead_id: Optional[str] = None,
    channel: Optional[str] = None,   # ai | human — see CallLog.channel
    limit: int = Query(50, le=200),
    offset: int = 0,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    conds = [CallLog.company_id == company.id]
    if direction:
        conds.append(CallLog.direction == direction)
    if status:
        conds.append(CallLog.status == status)
    if lead_id:
        conds.append(CallLog.lead_id == lead_id)
    if channel:
        conds.append(CallLog.channel == channel)

    q = select(CallLog).where(and_(*conds)).order_by(CallLog.created_at.desc()).limit(limit).offset(offset)
    total_q = select(func.count()).select_from(CallLog).where(and_(*conds))
    result = await db.execute(q)
    total = (await db.execute(total_q)).scalar()
    return {"total": total, "calls": [_dict(c) for c in result.scalars().all()]}


@router.get("/stats")
async def call_stats(
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    company = await _company(current_user.id, db)
    r = await db.execute(select(CallLog).where(CallLog.company_id == company.id))
    calls = r.scalars().all()
    total = len(calls)
    inbound = sum(1 for c in calls if c.direction == "inbound")
    outbound = sum(1 for c in calls if c.direction == "outbound")
    completed = sum(1 for c in calls if c.status == "completed")
    avg_duration = (
        sum(c.duration_seconds or 0 for c in calls if c.status == "completed") / max(completed, 1)
    )
    return {
        "total": total,
        "inbound": inbound,
        "outbound": outbound,
        "completed": completed,
        "no_answer": sum(1 for c in calls if c.status == "no_answer"),
        "transferred": sum(1 for c in calls if c.transferred_to_human),
        "avg_duration_seconds": round(avg_duration),
        "positive": sum(1 for c in calls if c.sentiment == "positive"),
        "negative": sum(1 for c in calls if c.sentiment == "negative"),
    }


@router.get("/{call_id}")
async def get_call(
    call_id: str,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi import HTTPException
    company = await _company(current_user.id, db)
    r = await db.execute(
        select(CallLog).where(CallLog.id == call_id, CallLog.company_id == company.id)
    )
    call = r.scalar_one_or_none()
    if not call:
        raise HTTPException(404, "Call not found")
    result = _dict(call)
    result["conversation_history"] = call.conversation_history or []
    return result
