# app/core/security.py
# Firebase Auth (Aug 2026) — the frontend authenticates directly against
# Firebase and sends the resulting ID token as a Bearer token. This module
# verifies that token and resolves it to a local User row.
import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from firebase_admin import exceptions as firebase_exceptions
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import firebase
from app.core.database import get_db
from app.models.models import Company, User

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/firebase-note",
    auto_error=False,
)


async def _get_or_create_user(decoded: dict, db: AsyncSession) -> User:
    """Resolve a Firebase identity to one local User row.

    Lookup order:
      1. Firebase UID — normal case.
      2. Email — handles an existing local account whose Firebase UID is
         missing or stale, so we never try to INSERT a duplicate email.
      3. Create a genuinely new local account.

    The final INSERT is also protected against a race where two requests
    attempt to provision the same email at the same time.
    """
    uid = decoded["uid"]
    email = (decoded.get("email") or "").strip().lower()
    full_name = decoded.get("name") or (email.split("@")[0] if email else "New User")
    email_verified = bool(decoded.get("email_verified"))

    # 1. Normal lookup: Firebase UID already linked to our local user.
    result = await db.execute(select(User).where(User.firebase_uid == uid))
    user = result.scalar_one_or_none()
    if user:
        return user

    # 2. Reconciliation lookup: same email already exists locally.
    # Do NOT require firebase_uid IS NULL here. A stale/different Firebase UID
    # must not cause a second local user to be inserted for the same email.
    if email:
        result = await db.execute(select(User).where(User.email == email))
        existing = result.scalar_one_or_none()
        if existing:
            # If this row already points at another Firebase UID, this request
            # is still an authenticated Firebase identity for the same email.
            # Re-associate it instead of creating a duplicate local account.
            if existing.firebase_uid != uid:
                logger.warning(
                    "Re-linking local user %s (%s) from Firebase UID %s to %s",
                    existing.id,
                    existing.email,
                    existing.firebase_uid,
                    uid,
                )
                existing.firebase_uid = uid

            if email_verified and not existing.email_verified:
                existing.email_verified = True

            try:
                await db.commit()
                await db.refresh(existing)
            except IntegrityError:
                await db.rollback()
                # A concurrent request may have completed the same update.
                retry = await db.execute(select(User).where(User.firebase_uid == uid))
                existing_after_race = retry.scalar_one_or_none()
                if existing_after_race:
                    return existing_after_race
                raise

            return existing

    # 3. Truly new user: create the local profile.
    user = User(
        email=email or f"{uid}@firebase.local",
        full_name=full_name,
        firebase_uid=uid,
        is_active=True,
        email_verified=email_verified,
    )
    db.add(user)

    try:
        await db.commit()
        await db.refresh(user)
    except IntegrityError:
        # A second request can provision the same email between the lookup
        # above and this INSERT. Never let that become an unhandled 500.
        await db.rollback()

        if email:
            retry = await db.execute(select(User).where(User.email == email))
            existing = retry.scalar_one_or_none()
            if existing:
                if existing.firebase_uid != uid:
                    existing.firebase_uid = uid
                if email_verified and not existing.email_verified:
                    existing.email_verified = True
                await db.commit()
                await db.refresh(existing)
                return existing

        # If it was not a duplicate-email race, surface a controlled server
        # error instead of returning a broken SQLAlchemy transaction.
        logger.exception("Failed to provision Firebase user uid=%s email=%s", uid, email)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create your local account. Please try again.",
        )

    # Provision the company only for a genuinely new local user.
    company = Company(
        owner_id=user.id,
        name=f"{full_name}'s Company",
        plan_type="none",
    )
    db.add(company)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # The user itself was successfully created. A company provisioning
        # race should not turn /auth/me into an unhandled database error.
        logger.exception("Failed to provision company for Firebase user %s", user.email)

    logger.info("Provisioned new account via Firebase: %s (uid=%s)", user.email, uid)
    return user


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise cred_exc

    try:
        decoded = firebase.verify_id_token(token)
    except firebase_exceptions.FirebaseError as e:
        logger.info("Firebase token verification failed: %s", e)
        raise cred_exc
    except Exception as e:
        logger.warning("Unexpected error verifying Firebase token: %s", e)
        raise cred_exc

    user = await _get_or_create_user(decoded, db)

    # Keep the local flag synchronized when Firebase reports that the email
    # has been verified. The Firebase token is authoritative here.
    if decoded.get("email_verified") and not user.email_verified:
        user.email_verified = True
        await db.commit()
        await db.refresh(user)

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disabled",
        )
    return current_user
