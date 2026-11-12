# app/api/routes/admin.py
#
# Merged-in replacement for the standalone "license app/" server. Every
# route here is gated by a single bearer token (settings.ADMIN_TOKEN) —
# there's no per-admin-user concept, this is meant for you (the operator),
# not a multi-admin system. Treat ADMIN_TOKEN like a root password:
#   - change it from the default in production
#   - only expose this panel to a trusted network (VPN / IP allowlist at
#     your reverse proxy / Azure App Service access restrictions) — the
#     token is the ONLY gate, there's no rate limiting or lockout on it
#     the way there is on user login.
#
# Covers what the standalone admin.html used to do (generate/list/revoke
# licenses) plus what it didn't (list users, reset a user's password —
# needed now that there's no "forgot password" email flow: a locked-out
# user contacts you, you reset it here, they change it in Settings).

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis_client import redis_client
from app.core import firebase
from app.models.models import Company, User
from app.services import plan_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ADMIN_TOKEN is the only gate on every route in this file, and unlike
# user login it has no lockout of its own — anyone who can reach this
# path can hammer it with guessed tokens as fast as the network allows.
# This adds the same IP-keyed Redis rate limit login already has,
# counting failed attempts only (a correct token never counts against
# the limit, so normal admin-panel usage — which can easily be dozens of
# calls in a session — never gets throttled).
async def _require_admin(request: Request, authorization: str = Header(...)):
    client_ip = request.client.host if request.client else "unknown"
    rl_key = f"admin_auth_fail:{client_ip}"

    attempts = await redis_client.get(rl_key) or 0
    if int(attempts) >= settings.ADMIN_RATE_LIMIT_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many failed admin auth attempts from this address. Try again later.",
        )

    if authorization != f"Bearer {settings.ADMIN_TOKEN}":
        await redis_client.incr(rl_key, expire=settings.ADMIN_RATE_LIMIT_WINDOW_SECONDS)
        raise HTTPException(401, "Invalid admin token")


# ─────────────────────────────────────────────────────────────────────────────
# Plans — REPLACES the old Licenses section (generate/reset-domain/revoke/
# list against the `licenses` table). That table and app/services/
# license_service.py are left in place for historical data but are no
# longer wired into signup or call-gating anywhere — see
# app/services/plan_service.py instead. This is the admin-side equivalent:
# manually credit a company with minutes (comps, support goodwill,
# refund-as-credit) without them going through Cashfree checkout.
# ─────────────────────────────────────────────────────────────────────────────
class GrantPlanRequest(BaseModel):
    plan_type: str                        # trial | basic | standard | custom
    custom_amount: Optional[float] = None  # required if plan_type == "custom"
    note: Optional[str] = None


@router.post("/companies/{company_id}/grant-plan", dependencies=[Depends(_require_admin)])
async def grant_plan(company_id: str, data: GrantPlanRequest):
    """Admin-side equivalent of a successful Cashfree payment — credits
    minutes onto a company directly, bypassing checkout entirely. Does
    NOT create a PaymentOrder row (there's no real payment), so it won't
    show up in Cashfree reconciliation — only in this company's plan
    fields and your own admin audit trail (server logs)."""
    async with AsyncSessionLocal() as db:
        company = await db.get(Company, company_id)
        if not company:
            raise HTTPException(404, "Company not found")

        try:
            priced = plan_service.price_plan(data.plan_type, data.custom_amount)
            plan_service.assert_purchasable(company, priced["plan_type"])
        except plan_service.PlanError as e:
            raise HTTPException(400, str(e))

        plan_service.apply_paid_plan(company, priced)
        await db.commit()

        logger.info(f"Admin granted plan | company={company_id} | plan={priced['plan_type']} | minutes={priced['minutes']} | note={data.note!r}")
        return {"success": True, **plan_service.balance_summary(company)}


# ─────────────────────────────────────────────────────────────────────────────
# Users — needed since there's no self-serve "forgot password" (no email
# system). A locked-out user contacts you; you reset their password here;
# they change it themselves afterward in Settings.
# ─────────────────────────────────────────────────────────────────────────────
class ResetPasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@router.get("/users", dependencies=[Depends(_require_admin)])
async def list_users():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).order_by(User.created_at.desc()))
        users = result.scalars().all()

        out = []
        for u in users:
            r = await db.execute(select(Company).where(Company.owner_id == u.id))
            company = r.scalar_one_or_none()
            out.append({
                "id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "company_id": company.id if company else None,
                "company_name": company.name if company else None,
                "plan_type": company.plan_type if company else None,
                "minutes_remaining": plan_service.minutes_remaining(company) if company else None,
                "plan_expires_at": company.plan_expires_at.isoformat() if company and company.plan_expires_at else None,
                "is_demo_account": bool(company.is_demo_account) if company else False,
                "demo_calls_remaining": company.demo_calls_remaining if company else None,
            })
    return {"total": len(out), "users": out}


@router.post("/users/{user_id}/reset-password", dependencies=[Depends(_require_admin)])
async def reset_user_password(user_id: str, data: ResetPasswordRequest):
    """Sets the password directly in Firebase — there's no local password
    to reset anymore (see app/core/security.py). Requires the user to
    have a firebase_uid, i.e. they've actually signed in at least once
    since the Firebase Auth switch."""
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        if not user.firebase_uid:
            raise HTTPException(400, "This user hasn't signed in via Firebase yet — nothing to reset.")
        try:
            firebase.update_user_password(user.firebase_uid, data.new_password)
        except Exception as e:
            logger.error(f"Firebase password reset failed for user_id={user_id}: {e}")
            raise HTTPException(502, "Could not reset password in Firebase — check FIREBASE_* env vars and try again.")
    logger.info(f"Admin reset password for user_id={user_id}")
    return {"success": True, "message": f"Password reset for {user.email}. Share the new password with them directly."}


@router.post("/users/{user_id}/disable", dependencies=[Depends(_require_admin)])
async def disable_user(user_id: str):
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        user.is_active = False
        await db.commit()
        # Belt-and-suspenders: our own get_current_active_user already
        # blocks a disabled user immediately regardless of Firebase's own
        # state (it checks User.is_active on every request), but revoking
        # here too means their EXISTING token can't be used to hit
        # Firebase-backed things outside this backend either, and closes
        # the gap if is_active is ever bypassed by a future code change.
        if user.firebase_uid:
            try:
                firebase.revoke_refresh_tokens(user.firebase_uid)
            except Exception as e:
                logger.warning(f"Could not revoke Firebase tokens for user_id={user_id}: {e}")
    return {"success": True, "message": f"Disabled {user.email}"}


@router.post("/users/{user_id}/enable", dependencies=[Depends(_require_admin)])
async def enable_user(user_id: str):
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        user.is_active = True
        await db.commit()
    return {"success": True, "message": f"Enabled {user.email}"}


class SetDemoCallsRequest(BaseModel):
    calls_remaining: int = 2
    is_demo_account: Optional[bool] = None   # omit to leave unchanged; set True the first time you turn a company into a demo account


@router.post("/companies/{company_id}/demo-calls", dependencies=[Depends(_require_admin)])
async def set_demo_calls(company_id: str, data: SetDemoCallsRequest):
    """Resets the shared demo account's call counter before handing the
    login to a new prospect (or turns an existing company into a demo
    account for the first time, via is_demo_account). See
    app/tasks/tasks.py's _async_outbound_call for where this is enforced
    — real customer accounts (is_demo_account=False) are never affected
    by anything here regardless of what calls_remaining holds."""
    if data.calls_remaining < 0:
        raise HTTPException(400, "calls_remaining cannot be negative")
    async with AsyncSessionLocal() as db:
        company = await db.get(Company, company_id)
        if not company:
            raise HTTPException(404, "Company not found")
        company.demo_calls_remaining = data.calls_remaining
        if data.is_demo_account is not None:
            company.is_demo_account = data.is_demo_account
        await db.commit()
    logger.info(f"Admin set demo calls for company_id={company_id} → {data.calls_remaining} (is_demo_account={data.is_demo_account})")
    return {
        "success": True,
        "company_id": company_id,
        "demo_calls_remaining": data.calls_remaining,
        "is_demo_account": data.is_demo_account,
    }
