# app/core/rate_limit.py
#
# Reusable version of the rate-limit pattern already hand-rolled 3x
# (login, register, admin — see auth.py/admin.py) — same underlying
# mechanism (Redis INCR + TTL, consistent across worker processes), just
# not copy-pasted a 4th/5th time for the newer money/resource-sensitive
# endpoints (Cashfree order creation, human-call dialing) that didn't
# have any cap on them at all before this.
from fastapi import Depends, HTTPException, Request

from app.core.redis_client import redis_client
from app.core.security import get_current_active_user
from app.models.models import User


def rate_limit(key_prefix: str, attempts: int, window_seconds: int):
    """Returns a FastAPI dependency that raises 429 once `attempts` calls
    from the same authenticated user land within `window_seconds`.

    Keyed on user id, not IP — appropriate for anything already gated
    behind login (which every use of this in the codebase is), since IP
    alone both over- and under-blocks: it locks out everyone behind one
    NAT/office network together, while doing nothing to stop one
    compromised account hopping IPs.

    key_prefix: namespaces this limiter in Redis — keep it unique per
    endpoint, e.g. "create_order", "human_dial".

    Usage:
        @router.post(
            "/dial",
            dependencies=[Depends(rate_limit("human_dial", 30, 3600))],
        )
    FastAPI caches a dependency's result per request, so declaring both
    this AND `current_user=Depends(get_current_active_user)` on the same
    route does not verify the Firebase token twice.
    """
    async def _dependency(
        request: Request,
        current_user: User = Depends(get_current_active_user),
    ):
        key = f"ratelimit:{key_prefix}:{current_user.id}"
        count = await redis_client.incr(key, expire=window_seconds)
        if count > attempts:
            raise HTTPException(
                status_code=429,
                detail="Too many requests — please slow down and try again shortly.",
            )
    return _dependency
