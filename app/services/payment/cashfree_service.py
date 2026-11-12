"""
Cashfree Payment Gateway — Orders API (one-time payments).

Deliberately implemented as plain REST calls (httpx) rather than pulling
in the `cashfree_pg` SDK — the Orders API surface we actually need
(create order, fetch order, verify webhook signature) is three small
calls, and avoiding the SDK keeps one less fast-moving dependency in a
payments path. If you'd rather use the official SDK later, only this
file needs to change — nothing else touches Cashfree directly.

Docs referenced:
  - Create order:  https://docs.cashfree.com/reference/pg-create-order
  - Fetch order:   https://docs.cashfree.com/reference/pg-fetch-order
  - Webhook verify: https://www.cashfree.com/docs/payments/online/webhooks/signature-verification
"""
import base64
import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SANDBOX_BASE_URL = "https://sandbox.cashfree.com/pg"
PRODUCTION_BASE_URL = "https://api.cashfree.com/pg"


def _base_url() -> str:
    return PRODUCTION_BASE_URL if (settings.CASHFREE_ENV or "sandbox").lower() == "production" else SANDBOX_BASE_URL


def _headers(idempotency_key: Optional[str] = None) -> dict:
    if not settings.CASHFREE_CLIENT_ID or not settings.CASHFREE_CLIENT_SECRET:
        raise RuntimeError(
            "CASHFREE_CLIENT_ID / CASHFREE_CLIENT_SECRET not set — add them to .env "
            "(Merchant Dashboard -> Developers -> API Keys)."
        )
    headers = {
        "Content-Type": "application/json",
        "x-api-version": settings.CASHFREE_API_VERSION,
        "x-client-id": settings.CASHFREE_CLIENT_ID,
        "x-client-secret": settings.CASHFREE_CLIENT_SECRET,
    }
    if idempotency_key:
        headers["x-idempotency-key"] = idempotency_key
    return headers


class CashfreeError(Exception):
    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body


async def create_order(
    *,
    order_id: str,
    order_amount: float,
    customer_id: str,
    customer_phone: str,
    customer_email: Optional[str] = None,
    return_url: str,
    notify_url: str,
) -> dict:
    """Creates a Cashfree order and returns the raw response dict — the
    caller needs `payment_session_id` from it to launch checkout on the
    frontend (Cashfree JS SDK's `cashfree.checkout({paymentSessionId})`).

    order_amount is a plain float in rupees (e.g. 2500.0), NOT paise —
    Cashfree's Orders API takes rupee amounts directly, unlike Razorpay.
    """
    payload = {
        "order_id": order_id,
        "order_amount": round(order_amount, 2),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_id,
            "customer_phone": customer_phone,
            **({"customer_email": customer_email} if customer_email else {}),
        },
        "order_meta": {
            "return_url": return_url,
            "notify_url": notify_url,
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_base_url()}/orders",
            json=payload,
            headers=_headers(idempotency_key=order_id),
        )
    if resp.status_code not in (200, 201):
        logger.error(f"Cashfree create_order failed | status={resp.status_code} | body={resp.text[:500]}")
        raise CashfreeError(f"Cashfree create_order failed ({resp.status_code})", response_body=resp.text)
    return resp.json()


async def fetch_order(order_id: str) -> dict:
    """Server-side source of truth for an order's status — call this from
    the webhook handler (or a reconciliation job) rather than trusting the
    webhook payload alone for anything money-affecting, per Cashfree's own
    guidance. order_id here is OUR order_id (the one we generated), not
    Cashfree's internal cf_order_id."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{_base_url()}/orders/{order_id}", headers=_headers())
    if resp.status_code != 200:
        logger.error(f"Cashfree fetch_order failed | status={resp.status_code} | body={resp.text[:500]}")
        raise CashfreeError(f"Cashfree fetch_order failed ({resp.status_code})", response_body=resp.text)
    return resp.json()


def verify_webhook_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """Cashfree's documented scheme:
        signed_payload = timestamp + raw_body
        expected = base64(HMAC_SHA256(signed_payload, CLIENT_SECRET_OR_WEBHOOK_SECRET))
    compared against the `x-webhook-signature` header. `timestamp` is the
    `x-webhook-timestamp` header value.

    Uses CASHFREE_WEBHOOK_SECRET if set (the per-endpoint secret from
    Merchant Dashboard -> Developers -> Webhooks), falling back to
    CASHFREE_CLIENT_SECRET — Cashfree signs with the client secret unless
    you've configured a distinct webhook secret for this endpoint.
    """
    secret = settings.CASHFREE_WEBHOOK_SECRET or settings.CASHFREE_CLIENT_SECRET
    if not secret:
        logger.error("Cashfree webhook verification: no CASHFREE_WEBHOOK_SECRET/CASHFREE_CLIENT_SECRET configured")
        return False
    if not signature or not timestamp:
        return False

    signed_payload = timestamp.encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).digest()
    ).decode("utf-8")

    # Constant-time compare — do NOT use `==` here (timing side-channel).
    return hmac.compare_digest(expected, signature)
