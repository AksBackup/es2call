# app/api/routes/license.py
# One-time activation-key licensing — replaces billing.py (Cashfree/PayPal
# monthly subscriptions). Talks to the external license server defined by
# activationkey.py (see app.services.license_service).

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.security import get_current_active_user
from app.models.models import Company
from app.services import license_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/license", tags=["license"])


class ActivateRequest(BaseModel):
    license_key: str
    domain: Optional[str] = None   # defaults to this server's own host if omitted


async def _get_company_for_user(current_user, db) -> Optional[Company]:
    owner_id = current_user.get("uid") if isinstance(current_user, dict) else current_user.id
    r = await db.execute(select(Company).where(Company.owner_id == owner_id))
    return r.scalar_one_or_none()


async def _get_or_create_company_for_user(current_user, db) -> Company:
    """Single-tenant product — every account gets exactly one Company.
    Normally created at registration (see auth.py), but accounts created
    before that existed (or any other edge case) get one lazily here
    instead of hard-blocking the license activation flow."""
    company = await _get_company_for_user(current_user, db)
    if company:
        return company

    full_name = current_user.get("full_name") if isinstance(current_user, dict) else current_user.full_name
    owner_id  = current_user.get("uid") if isinstance(current_user, dict) else current_user.id
    company = Company(owner_id=owner_id, name=f"{full_name}'s Company" if full_name else "My Company")
    db.add(company)
    await db.commit()
    await db.refresh(company)
    return company


# ─────────────────────────────────────────────────────────────────────────────
# POST /license/activate
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/activate")
async def activate_license(body: ActivateRequest, current_user=Depends(get_current_active_user)):
    async with AsyncSessionLocal() as db:
        company = await _get_or_create_company_for_user(current_user, db)

        from app.core.config import settings
        domain = body.domain or settings.PUBLIC_BASE_URL or "localhost"

        result = await license_service.activate(body.license_key.strip(), domain)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Activation failed"))

        company.license_key        = body.license_key.strip()
        company.license_domain     = domain
        company.license_tier       = result.get("tier")
        company.license_status     = "active"
        company.license_expires_at = license_service._parse_dt(result.get("expires_at"))
        await db.commit()

        return {
            "success": True,
            "tier": result.get("tier"),
            "expires_at": result.get("expires_at"),
            "message": f"License activated — {result.get('tier', 'plan')} tier",
        }


# ─────────────────────────────────────────────────────────────────────────────
# GET /license/status
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/status")
async def license_status(refresh: bool = False, current_user=Depends(get_current_active_user)):
    """
    Reports license status from the LOCAL Company row, not a live call to the
    external license server. The Company row is kept fresh by:
      - the activation call itself (writes license_status="active" immediately)
      - a startup check + a 24h Celery beat task (app.tasks.license_tasks)

    This matters: this endpoint is hit on every page load (see the frontend
    app layout gate). If it called the license server live every time, any
    network blip / license-server restart / DNS hiccup would lock a
    perfectly-valid, already-activated account out of the whole app. Live
    revalidation still happens — just in the background, not on the
    request path — unless `?refresh=true` is passed (used by the manual
    "Refresh" button on the Billing page).
    """
    async with AsyncSessionLocal() as db:
        company = await _get_company_for_user(current_user, db)
        if not company:
            return {"activated": False, "valid": False, "message": "No company profile yet"}

        if not company.license_key:
            return {"activated": False, "valid": False, "message": "No activation key entered yet"}

        if refresh:
            result = await license_service.refresh_status(company)
            if result.get("valid") is None:
                # License server unreachable — don't report a false negative,
                # fall back to whatever's locally stored (same as the fast path).
                not_expired = not company.license_expires_at or company.license_expires_at > datetime.utcnow()
                return {
                    "activated": True,
                    "valid": company.license_status == "active" and not_expired,
                    "tier": company.license_tier,
                    "expires_at": company.license_expires_at.isoformat() if company.license_expires_at else None,
                    "message": result.get("message", "Couldn't reach the license server — showing last known status"),
                }
            return {
                "activated": True,
                "valid": bool(result.get("valid")),
                "tier": result.get("tier") or company.license_tier,
                "expires_at": result.get("expires_at") or (
                    company.license_expires_at.isoformat() if company.license_expires_at else None
                ),
                "max_leads": result.get("max_leads"),
                "max_calls_month": result.get("max_calls_month"),
                "message": result.get("message", "License valid" if result.get("valid") else "License invalid"),
            }

        # Fast path — no external call, just trust the local record.
        not_expired = not company.license_expires_at or company.license_expires_at > datetime.utcnow()
        valid = company.license_status == "active" and not_expired

        cached = await license_service.get_cached_status(company.id)
        return {
            "activated": True,
            "valid": valid,
            "tier": company.license_tier,
            "expires_at": company.license_expires_at.isoformat() if company.license_expires_at else None,
            "max_leads": (cached or {}).get("max_leads"),
            "max_calls_month": (cached or {}).get("max_calls_month"),
            "message": "License valid" if valid else (
                "License expired — please renew" if company.license_status == "active" else "License inactive"
            ),
        }
