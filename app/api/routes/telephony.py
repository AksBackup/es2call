"""
Telephony utility routes — small helper endpoints used by the frontend.

The actual call flow (answer/gather/hangup XML webhooks) lives entirely in
vobiz_webhook.py now that Telnyx has been removed. This file only exposes:
  GET  /telephony/numbers            — the company's configured Vobiz DID
  POST /telephony/calls/{cid}/hangup — force-hangup a live call
  GET  /telephony/calls/{cid}/transcript — last live transcript line
"""
import logging
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.security import get_current_active_user
from app.models.models import Company
from app.services.telephony.call_session import session_manager
from app.services.telephony.vobiz_service import vobiz_service
from app.services.telephony.telnyx_service import telnyx_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/numbers")
async def list_numbers(current_user=Depends(get_current_active_user)) -> List[dict]:
    """Neither carrier is queried — return whatever number(s) are configured
    on the current user's company (Vobiz DID and/or Telnyx number)."""
    async with AsyncSessionLocal() as db:
        owner_id = current_user.get("uid") if isinstance(current_user, dict) else current_user.id
        r = await db.execute(select(Company).where(Company.owner_id == owner_id))
        company = r.scalar_one_or_none()
        if not company:
            return []
        out = []
        if company.vobiz_phone_number:
            out.append({"number": company.vobiz_phone_number, "status": "active", "provider": "vobiz"})
        if company.telnyx_phone_number:
            out.append({"number": company.telnyx_phone_number, "status": "active", "provider": "telnyx"})
        return out


@router.post("/calls/{cid}/hangup")
async def force_hangup(cid: str, current_user=Depends(get_current_active_user)):
    """Manually terminates a live AI call from the Live Calls tab. `cid`
    is Vobiz's call_uuid — that's the only identifier the Live Calls
    frontend actually has (live_ws.py's call_ringing/call_answered/etc
    events all carry call_uuid, never an internal CallLog row id).

    BUG FIX (Aug 2026): this previously called vobiz_service.hangup(cid)
    with NO company argument — vobiz_service._creds(None) returns EMPTY
    auth_id/auth_token, so every hangup attempt was silently failing
    against Vobiz's API for every company, regardless of who called it
    (confirmed by reading _creds()'s fallback logic: it only fills in
    default creds for the shared demo account, not a bare None). This
    button looked like it didn't exist because it was completely
    non-functional, not missing. Also added an ownership check that
    didn't exist before — nothing previously verified `cid` actually
    belonged to the calling user's own company before asking Vobiz to
    hang it up."""
    from fastapi import HTTPException
    from app.models.models import CallLog

    async with AsyncSessionLocal() as db:
        owner_id = current_user.get("uid") if isinstance(current_user, dict) else current_user.id
        r = await db.execute(select(Company).where(Company.owner_id == owner_id))
        company = r.scalar_one_or_none()
        if not company:
            raise HTTPException(404, "Company not found")

        r2 = await db.execute(
            select(CallLog).where(CallLog.call_control_id == cid, CallLog.company_id == company.id)
        )
        call = r2.scalar_one_or_none()
        if not call:
            raise HTTPException(404, "No call on record with that call_uuid for your company.")
        if call.status not in ("ringing", "in_progress"):
            raise HTTPException(400, f"Call is already {call.status} — nothing to hang up.")

        # CallLog.provider records which carrier placed/answered this call.
        carrier = telnyx_service if (call.provider or "").lower() == "telnyx" else vobiz_service
        ok = await carrier.hangup(cid, company)
        if not ok:
            name = "Telnyx" if carrier is telnyx_service else "Vobiz"
            raise HTTPException(502, f"{name} didn't confirm the hangup — check backend logs. The call may still be live.")
        return {"success": True}


@router.get("/calls/{cid}/transcript")
async def live_transcript(cid: str, current_user=Depends(get_current_active_user)):
    return {"transcript": await session_manager.get_live_transcript(cid)}
