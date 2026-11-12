"""
PayPal Checkout — Orders v2 API (one-time payments), plain REST via httpx
(same approach as cashfree_service.py: no SDK dependency in a payments path).

Flow used by this app (redirect flow, NO webhook required):
  1. create_order()   POST /v2/checkout/orders  (intent=CAPTURE)
                      -> returns an approval URL; the browser is redirected there.
  2. Buyer approves on paypal.com and PayPal redirects the browser to our
     return_url with ?token=<paypal_order_id>&PayerID=...
  3. capture_order()  POST /v2/checkout/orders/{id}/capture  — the money only
                      moves HERE, server-side. If the buyer abandons the
                      page, nothing is charged.
  The caller (api/routes/payments.py) verifies status == COMPLETED and that
  the captured amount/currency match what we asked for before crediting.

Docs: https://developer.paypal.com/docs/api/orders/v2/
Auth: https://developer.paypal.com/api/rest/authentication/  (OAuth2 client-credentials)

Currency: PayPal does not offer INR for most accounts. Plans are priced in
₹ in this app, so by default the INR amount is converted to USD with
PAYPAL_INR_PER_USD. Set PAYPAL_CURRENCY=INR only if your PayPal business
account is enabled for INR.
"""
import logging
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
LIVE_BASE_URL = "https://api-m.paypal.com"

_token_cache: Dict[str, Any] = {"token": None, "exp": 0.0, "env": None}


class PayPalError(Exception):
    def __init__(self, message: str, response_body: Any = None):
        super().__init__(message)
        self.response_body = response_body


def _base_url() -> str:
    return LIVE_BASE_URL if (settings.PAYPAL_ENV or "sandbox").lower() in ("live", "production") else SANDBOX_BASE_URL


def charge_amount(amount_inr: float) -> Tuple[str, str]:
    """(currency_code, 'value' string) PayPal should charge for a plan priced
    in rupees. PayPal wants a string with exactly the currency's decimals."""
    cur = (settings.PAYPAL_CURRENCY or "USD").upper()
    if cur == "INR":
        value = Decimal(str(amount_inr))
    else:
        rate = Decimal(str(settings.PAYPAL_INR_PER_USD or 90))
        value = Decimal(str(amount_inr)) / rate
    return cur, str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def _access_token() -> str:
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set — add them to .env")
    env = _base_url()
    if _token_cache["token"] and _token_cache["env"] == env and time.time() < _token_cache["exp"] - 60:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{env}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.PAYPAL_CLIENT_ID.strip().strip("'\""), settings.PAYPAL_CLIENT_SECRET.strip().strip("'\"")),
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.error(f"PayPal token failed | status={resp.status_code} | body={resp.text[:300]}")
        if resp.status_code == 401:
            cid = settings.PAYPAL_CLIENT_ID or ""
            logger.error(
                f"PayPal 401 hint | PAYPAL_ENV={settings.PAYPAL_ENV!r} -> {env} | "
                f"client_id={cid[:6]}...{cid[-4:]} (len={len(cid)}) | secret_len={len(settings.PAYPAL_CLIENT_SECRET or '')}. "
                "invalid_client = the ID/secret pair does not belong to this environment. Use the Sandbox app's "
                "Client ID + Secret with PAYPAL_ENV=sandbox (or the Live pair with PAYPAL_ENV=live); "
                "check for swapped values, a regenerated secret, or a stale PAYPAL_* variable in the shell/.env."
            )
        raise PayPalError(f"PayPal auth failed ({resp.status_code})", resp.text)
    data = resp.json()
    _token_cache.update(token=data["access_token"], exp=time.time() + int(data.get("expires_in", 300)), env=env)
    return data["access_token"]


async def create_order(
    *, order_id: str, amount_inr: float, description: str, return_url: str, cancel_url: str,
) -> Dict[str, Any]:
    """Returns {paypal_order_id, approval_url, currency, value, raw}."""
    currency, value = charge_amount(amount_inr)
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": order_id,
            "custom_id": order_id,
            "invoice_id": order_id,       # PayPal rejects a re-used invoice_id => no double-charge on retries
            "description": description[:120],
            "amount": {"currency_code": currency, "value": value},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "landing_page": "LOGIN",
        }}},
    }
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_base_url()}/v2/checkout/orders", json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "PayPal-Request-Id": order_id},
        )
    if resp.status_code not in (200, 201):
        logger.error(f"PayPal create_order failed | status={resp.status_code} | body={resp.text[:600]}")
        raise PayPalError(f"PayPal create_order failed ({resp.status_code})", resp.text)
    data = resp.json()
    links = {l.get("rel"): l.get("href") for l in data.get("links", [])}
    approval = links.get("payer-action") or links.get("approve")
    if not approval:
        raise PayPalError("PayPal response had no approval link", data)
    return {"paypal_order_id": data["id"], "approval_url": approval,
            "currency": currency, "value": value, "raw": data}


async def capture_order(paypal_order_id: str) -> Dict[str, Any]:
    """Captures an APPROVED order. Idempotent on PayPal's side per request id:
    capturing an already-captured order returns 422 ORDER_ALREADY_CAPTURED,
    which we resolve by fetching the order instead."""
    token = await _access_token()
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(
            f"{_base_url()}/v2/checkout/orders/{paypal_order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "PayPal-Request-Id": f"cap_{paypal_order_id}"},
            content=b"{}",
        )
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 422 and "ORDER_ALREADY_CAPTURED" in resp.text:
        return await fetch_order(paypal_order_id)
    logger.error(f"PayPal capture failed | status={resp.status_code} | body={resp.text[:600]}")
    raise PayPalError(f"PayPal capture failed ({resp.status_code})", resp.text)


async def fetch_order(paypal_order_id: str) -> Dict[str, Any]:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_base_url()}/v2/checkout/orders/{paypal_order_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise PayPalError(f"PayPal fetch_order failed ({resp.status_code})", resp.text)
    return resp.json()


def extract_capture(order: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Pull the first capture out of an order/capture response:
    {id, status, currency, value, custom_id} or None."""
    try:
        pu = order["purchase_units"][0]
        cap = pu["payments"]["captures"][0]
        return {
            "id": cap["id"], "status": cap["status"],
            "currency": cap["amount"]["currency_code"], "value": cap["amount"]["value"],
            "custom_id": cap.get("custom_id") or pu.get("custom_id") or pu.get("reference_id") or "",
        }
    except (KeyError, IndexError, TypeError):
        return None
