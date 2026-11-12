"""
Minute-based plan logic — replaces the old yearly license system.

Plans (see app/core/config.py for the ₹ figures — all configurable there,
not hardcoded here):
  trial:    ₹100  / 10 minutes   — ONE-TIME per company, ever
  basic:    ₹2500 / 500 minutes  (₹5/min flat)
  standard: ₹2000 / 2000 minutes (₹4/min flat)
  custom:   customer enters any ₹ amount >= ₹1000; minutes = amount / ₹4.3

Every paid plan expires 365 days from the purchase date
(Company.plan_expires_at). Buying a new plan does NOT carry over unused
minutes from the previous one (plan_minutes_used resets to 0) — confirmed
behavior, since "which company" and "how many minutes right now" is
simpler to reason about (and audit) as one active plan at a time rather
than a stack of expiring minute-buckets.

When a company's minutes run out OR the plan expires: ALL new calls
(AI batch dispatch AND human dial) are blocked immediately — see
has_minutes_available() below, called from app/tasks/tasks.py before
dialing and from the (forthcoming) human dial endpoint.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

VALID_PLAN_TYPES = {"trial", "basic", "standard", "custom"}


class PlanError(ValueError):
    """Raised for invalid plan requests (bad plan_type, custom amount too low, trial already used)."""


def price_plan(plan_type: str, custom_amount: Optional[float] = None) -> dict:
    """Returns {plan_type, minutes, rate_per_minute, amount} for a
    requested plan — the single source of truth both the order-creation
    endpoint and the webhook credit step use, so they can never disagree
    on what a given order_id was actually supposed to buy."""
    plan_type = (plan_type or "").lower().strip()
    if plan_type not in VALID_PLAN_TYPES:
        raise PlanError(f"Unknown plan_type '{plan_type}' — must be one of {sorted(VALID_PLAN_TYPES)}")

    if plan_type == "trial":
        return {
            "plan_type": "trial",
            "minutes": settings.PLAN_TRIAL_MINUTES,
            "rate_per_minute": round(settings.PLAN_TRIAL_AMOUNT / settings.PLAN_TRIAL_MINUTES, 4),
            "amount": settings.PLAN_TRIAL_AMOUNT,
        }
    if plan_type == "basic":
        return {
            "plan_type": "basic",
            "minutes": settings.PLAN_BASIC_MINUTES,
            "rate_per_minute": round(settings.PLAN_BASIC_AMOUNT / settings.PLAN_BASIC_MINUTES, 4),
            "amount": settings.PLAN_BASIC_AMOUNT,
        }
    if plan_type == "standard":
        return {
            "plan_type": "standard",
            "minutes": settings.PLAN_STANDARD_MINUTES,
            "rate_per_minute": round(settings.PLAN_STANDARD_AMOUNT / settings.PLAN_STANDARD_MINUTES, 4),
            "amount": settings.PLAN_STANDARD_AMOUNT,
        }
    # custom — customer names the amount, must be >= the configured floor
    if custom_amount is None:
        raise PlanError("custom plan requires an amount")
    custom_amount = float(custom_amount)
    if custom_amount < settings.PLAN_CUSTOM_MIN_AMOUNT:
        raise PlanError(f"Custom plan amount must be at least ₹{settings.PLAN_CUSTOM_MIN_AMOUNT:.0f}")
    minutes = custom_amount / settings.PLAN_CUSTOM_RATE_PER_MINUTE
    return {
        "plan_type": "custom",
        "minutes": round(minutes, 2),
        "rate_per_minute": settings.PLAN_CUSTOM_RATE_PER_MINUTE,
        "amount": round(custom_amount, 2),
    }


def assert_purchasable(company: Any, plan_type: str) -> None:
    """Raises PlanError if this company isn't allowed to buy this plan
    right now — currently only blocks a second trial purchase."""
    if plan_type == "trial" and getattr(company, "trial_used", False):
        raise PlanError("Trial plan has already been used for this company — it's one-time only.")


def apply_paid_plan(company: Any, priced: dict) -> None:
    """Mutates `company` in place to reflect a just-completed payment.
    Caller (the Cashfree webhook handler) is responsible for committing
    the session. Does NOT touch PaymentOrder — that's the caller's job
    too, so this function stays a pure "what does a paid plan do to a
    Company" function, testable without a DB order row at all.

    TOP-UP BEHAVIOR: if the company still has an active (non-expired) plan
    when a new one is paid for, the newly bought minutes are ADDED to
    whatever is left rather than replacing it — this is the "Buy More
    Minutes" flow on the Billing page, and it should never discard minutes
    the company already paid for. Only a company with no plan yet, or
    whose plan has already expired (its old minutes are void anyway per
    has_minutes_available()), gets a fresh plan_minutes_used=0 start."""
    now = datetime.utcnow()
    topping_up = (
        company.plan_type
        and company.plan_type != "none"
        and company.plan_expires_at is not None
        and not is_plan_expired(company)
    )

    if topping_up:
        company.plan_minutes_total = round((company.plan_minutes_total or 0) + priced["minutes"])
        company.plan_amount_paid = round((company.plan_amount_paid or 0.0) + priced["amount"], 2)
        # Extend validity — never shorten it if the existing plan already
        # runs further out than a fresh 365-day window would.
        new_expiry = now + timedelta(days=settings.PLAN_EXPIRY_DAYS)
        if not company.plan_expires_at or new_expiry > company.plan_expires_at:
            company.plan_expires_at = new_expiry
        # plan_minutes_used is intentionally left untouched — that's the
        # whole point of a top-up.
    else:
        company.plan_minutes_total = round(priced["minutes"])
        company.plan_amount_paid = priced["amount"]
        company.plan_expires_at = now + timedelta(days=settings.PLAN_EXPIRY_DAYS)
        company.plan_minutes_used = 0.0   # fresh plan — nothing to carry over

    company.plan_type = priced["plan_type"]
    company.plan_rate_per_minute = priced["rate_per_minute"]
    company.plan_purchased_at = now
    if priced["plan_type"] == "trial":
        company.trial_used = True


def record_minutes_used(company: Any, duration_seconds: int) -> None:
    """Call once per completed call (AI or human), right after
    CallLog.duration_seconds is set — see the call-completion path in
    vobiz_stream_pipeline.py / vobiz_webhook.py and the forthcoming human
    call-end endpoint. Mutates `company` in place; caller commits."""
    if not duration_seconds or duration_seconds <= 0:
        return
    company.plan_minutes_used = (company.plan_minutes_used or 0.0) + (duration_seconds / 60.0)


def minutes_remaining(company: Any) -> float:
    total = company.plan_minutes_total or 0
    used = company.plan_minutes_used or 0.0
    return max(0.0, total - used)


def is_plan_expired(company: Any) -> bool:
    if not company.plan_expires_at:
        return True   # no plan ever purchased
    return datetime.utcnow() >= company.plan_expires_at


def has_minutes_available(company: Any) -> bool:
    """THE call-gating check. When this returns False, block dialing —
    both the AI outbound task (app/tasks/tasks.py) and the human dial
    endpoint must call this before placing a call. Blocks immediately on
    zero minutes OR plan expiry, per confirmed behavior (no grace)."""
    if not company.plan_type or company.plan_type == "none":
        return False
    if is_plan_expired(company):
        return False
    return minutes_remaining(company) > 0


def balance_summary(company: Any) -> dict:
    """Shape returned by GET /api/v1/payments/balance — feeds the Home
    page minutes-remaining widget."""
    return {
        "plan_type": company.plan_type or "none",
        "minutes_total": company.plan_minutes_total or 0,
        "minutes_used": round(company.plan_minutes_used or 0.0, 2),
        "minutes_remaining": round(minutes_remaining(company), 2),
        "rate_per_minute": company.plan_rate_per_minute or 0.0,
        "amount_paid": company.plan_amount_paid or 0.0,
        "purchased_at": company.plan_purchased_at,
        "expires_at": company.plan_expires_at,
        "is_expired": is_plan_expired(company) if company.plan_expires_at else False,
        "trial_used": bool(company.trial_used),
        "can_place_calls": has_minutes_available(company),
    }
