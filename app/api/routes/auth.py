# app/api/routes/auth.py
# Firebase Auth (Aug 2026) — login, signup, and password management all
# happen client-side against Firebase directly (see frontend/src/lib/
# firebase.ts + store/index.ts). This backend has NO /login, /register,
# or /change-password endpoints anymore — it never sees a password. The
# only thing left here is /me (read your own profile) and the
# auto-provisioning that used to live in /register now happens the first
# time a new Firebase account hits ANY authenticated endpoint — see
# _get_or_create_user() in app/core/security.py.
#
# Firebase email verification (Aug 2026):
#   1. Person registers with the Firebase Client SDK (store/index.ts).
#   2. The frontend calls Firebase Auth's sendEmailVerification() directly.
#      Firebase sends the email; SendGrid is NOT involved.
#   3. Firebase's hosted action handler verifies the link and redirects to
#      /register?verified=1.
#   4. The frontend polls GET /verification-status, which checks Firebase
#      directly and syncs our local email_verified column.
#   5. Once verified, the frontend navigates to /pricing.
#
# The legacy /send-verification-email endpoint remains below for compatibility
# with any older clients, but the current registration UI does not call it.
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import firebase
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.core.security import get_current_active_user
from app.models.models import User
from app.services.email.email_service import email_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/me")
async def me(current_user: User = Depends(get_current_active_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "email_verified": current_user.email_verified,
        "created_at": current_user.created_at,
    }


def _verification_continue_url() -> str:
    frontend_base = settings.FRONTEND_PUBLIC_URL or settings.PUBLIC_BASE_URL or ""
    return f"{frontend_base.rstrip('/')}/register?verified=1"


@router.post(
    "/send-verification-email",
    dependencies=[Depends(rate_limit("send_verification_email", 3, 600))],
)
async def send_verification_email(current_user: User = Depends(get_current_active_user)):
    """Legacy verification endpoint retained for older clients.
    The current frontend uses Firebase Client SDK sendEmailVerification()
    directly, so the normal registration/resend flow no longer uses SendGrid.
    """
    if current_user.email_verified:
        return {"sent": False, "already_verified": True, "message": "This email is already verified."}

    if not current_user.firebase_uid:
        # Shouldn't happen for any account created through Firebase (every
        # account is, post-migration) — guards against the pre-Firebase
        # migrated-row edge case _get_or_create_user() mentions.
        raise HTTPException(400, "This account has no linked Firebase identity — contact support.")

    try:
        link = firebase.generate_email_verification_link(
            current_user.email, _verification_continue_url()
        )
    except Exception as e:
        logger.error(f"Verification link generation failed for {current_user.email}: {e}")
        raise HTTPException(502, "Could not generate a verification link — please try again shortly.")

    ok, _ = await email_service.send(
        to_email=current_user.email,
        to_name=current_user.full_name,
        subject="Verify your email — Ancentrix Voice",
        body_text=(
            f"Hi {current_user.full_name},\n\n"
            f"Please verify your email to finish setting up your account:\n{link}\n\n"
            f"This link is single-use. If you didn't create this account, you can "
            f"safely ignore this email."
        ),
        body_html=(
            f"<p>Hi {current_user.full_name},</p>"
            f"<p>Please verify your email to finish setting up your account:</p>"
            f"<p><a href=\"{link}\">Verify my email</a></p>"
            f"<p>If the button doesn't work, copy this link into your browser:<br>{link}</p>"
            f"<p>If you didn't create this account, you can safely ignore this email.</p>"
        ),
    )
    if not ok:
        raise HTTPException(502, "Could not send the verification email — please try again shortly.")

    return {"sent": True, "already_verified": False, "message": f"Verification email sent to {current_user.email}"}


@router.get("/verification-status")
async def verification_status(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Checked by the register page while it's waiting for the person to
    click the emailed link. Queries Firebase directly (Admin SDK
    get_user()) rather than relying on the caller's own ID token, since
    that token's `email_verified` claim only updates after the browser
    force-refreshes it — this way "yes, it's verified" is true the moment
    Firebase's servers record the click, not whenever the tab next
    happens to mint a new token.
    """
    if current_user.email_verified:
        return {"email_verified": True}

    if not current_user.firebase_uid:
        return {"email_verified": False}

    try:
        fb_user = firebase.get_user(current_user.firebase_uid)
    except Exception as e:
        logger.warning(f"verification-status: Firebase lookup failed for {current_user.email}: {e}")
        return {"email_verified": current_user.email_verified}

    if fb_user.email_verified and not current_user.email_verified:
        current_user.email_verified = True
        await db.commit()

    return {"email_verified": bool(fb_user.email_verified)}
