# app/core/firebase.py
#
# Firebase Admin SDK — server-side ID token verification only. This
# backend never touches passwords or issues its own sessions anymore
# (see app/core/security.py); it just verifies the ID token the frontend
# got from the Firebase client SDK on every request.
import json
import logging

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

from app.core.config import settings

logger = logging.getLogger(__name__)

_app = None


def _init() -> firebase_admin.App:
    """Lazy singleton — first call (from get_current_user in security.py,
    or admin.py's password-reset endpoint) initializes the SDK; every
    call after that reuses the same App instance. firebase_admin itself
    would raise ValueError on a second initialize_app() with the same
    name, so this guard matters, not just as an optimization."""
    global _app
    if _app is not None:
        return _app

    if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        cred = credentials.Certificate(json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON))
    elif settings.GOOGLE_APPLICATION_CREDENTIALS:
        cred = credentials.Certificate(settings.GOOGLE_APPLICATION_CREDENTIALS)
    else:
        # Works unattended on Cloud Run / GKE / Compute Engine with an
        # attached service account. Anywhere else (a plain VM, Render,
        # Railway, your laptop) this raises — set one of the two env vars
        # above instead of relying on this fallback outside GCP.
        logger.warning(
            "FIREBASE_SERVICE_ACCOUNT_JSON / GOOGLE_APPLICATION_CREDENTIALS "
            "not set — falling back to Application Default Credentials. "
            "This only works when actually running on GCP infrastructure."
        )
        cred = credentials.ApplicationDefault()

    init_kwargs = {"projectId": settings.FIREBASE_PROJECT_ID} if settings.FIREBASE_PROJECT_ID else {}
    _app = firebase_admin.initialize_app(cred, init_kwargs)
    logger.info("Firebase Admin SDK initialized")
    return _app


def verify_id_token(id_token: str) -> dict:
    """Returns the decoded token claims (uid, email, email_verified, ...)
    or raises — callers (get_current_user below) translate the exception
    into a 401, they don't need to inspect it themselves. check_revoked=True
    means a token from a session the user (or an admin, via disable_user)
    has since revoked is rejected even if it hasn't technically expired
    yet — costs one extra Firebase lookup per request but matters for the
    "admin disables a user" path to take effect immediately rather than
    waiting up to an hour for the token to expire on its own."""
    _init()
    return firebase_auth.verify_id_token(id_token, check_revoked=True)


def get_user(uid: str):
    _init()
    return firebase_auth.get_user(uid)


def update_user_password(uid: str, new_password: str) -> None:
    """Used by the admin password-reset endpoint (app/api/routes/admin.py)
    — there's no local password to reset anymore, so an admin resetting a
    user's password now means setting it directly in Firebase."""
    _init()
    firebase_auth.update_user(uid, password=new_password)


def generate_email_verification_link(email: str, continue_url: str) -> str:
    """Magic-link email verification (Aug 2026). Generates a Firebase
    "verify email" action link server-side — the actual verification
    happens on Google's own hosted action-handler page when the person
    clicks it (no backend endpoint of ours is involved in the click
    itself); `continue_url` is where THAT page redirects to afterward.
    We only generate the link and email it (see
    POST /auth/send-verification-email) — this keeps password/email-
    verification entirely server-driven, matching how the rest of this
    Firebase integration works (frontend never talks to Admin-only APIs
    directly).
    """
    _init()
    action_code_settings = firebase_auth.ActionCodeSettings(
        url=continue_url,
        handle_code_in_app=False,
    )
    return firebase_auth.generate_email_verification_link(
        email, action_code_settings=action_code_settings
    )


def revoke_refresh_tokens(uid: str) -> None:
    """Forces this user's existing sessions to stop working on their next
    token refresh — called when an admin disables an account, so
    'disabled' takes effect promptly instead of only blocking future
    logins while an already-issued token quietly keeps working for up to
    an hour."""
    _init()
    firebase_auth.revoke_refresh_tokens(uid)
