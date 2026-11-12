"""
Telnyx REST + media-stream service — second telephony provider, sits
alongside vobiz_service.py (Vobiz is untouched).

References (all checked against Telnyx's current docs):
  - Dial:            POST https://api.telnyx.com/v2/calls
                     developers.telnyx.com/api-reference/call-commands
  - Media streaming: developers.telnyx.com/docs/voice/programmable-voice/media-streaming
  - Hangup:          POST /v2/calls/{call_control_id}/actions/hangup
  - Webhooks:        developers.telnyx.com/docs/voice/programmable-voice/voice-api-webhooks
  - Serializer:      pipecat.serializers.telnyx.TelnyxFrameSerializer (built into pipecat)

Architecture (mirrors the Vobiz streaming flow, so the Pipecat pipeline in
vobiz_stream_pipeline.py is reused as-is):

  Celery task -> make_outbound_call()
      POST /v2/calls with stream_url + stream_bidirectional_mode=rtp.
      Telnyx dials the lead; when the lead ANSWERS it opens a WebSocket to
      stream_url  ->  /api/v1/telnyx/media-stream  ->  the Pipecat pipeline.
  Telnyx call events (call.hangup, ...) -> /api/v1/telnyx/webhook, which
      finalizes the call through the SAME _finalize_hangup() Vobiz uses.

Identifier convention: Telnyx's `call_control_id` is the single id used
everywhere — dial response, CallLog.call_control_id, Redis session key,
Live-tab card id, WebSocket `start` event and webhook payloads all carry
it — so unlike Vobiz there is no request_uuid/CallUUID mismatch to patch.

Telnyx account prerequisites (per company):
  1. A *Call Control Application* (Voice -> Programmable Voice -> Applications,
     API version v2). Its ID is `telnyx_connection_id`. Its "Outbound voice
     profile" must be set or dialing fails.
  2. A phone number assigned to that application (`telnyx_phone_number`).
  3. API key (`telnyx_api_key`) and, recommended, the account public key
     (`telnyx_public_key`) for webhook signature verification.
  No static webhook URL is required: webhook_url and stream_url are sent
  with every dial command.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

TELNYX_BASE = "https://api.telnyx.com/v2"

# Codec we ask Telnyx to use for the bidirectional stream. PCMU = G.711
# mu-law @ 8 kHz, the codec PSTN legs natively use, so no transcoding on
# Telnyx's side (their docs warn that a mismatch transcodes and can
# degrade quality). TelnyxFrameSerializer resamples TTS audio to 8 kHz.
STREAM_CODEC = "PCMU"


def _get_base_url() -> str:
    # Provider-agnostic helper (re-reads .env so a changed tunnel URL only
    # needs one process restarted). Reused instead of duplicated.
    from app.services.telephony.vobiz_service import _get_base_url as _b
    return _b()


def _ws_base() -> str:
    return _get_base_url().replace("https://", "wss://").replace("http://", "ws://")


# ── Stream auth token ───────────────────────────────────────────────────────
# The media-stream WebSocket is a public endpoint. Telnyx can sign the
# connection (Ed25519, header telnyx-signature-ed25519), but that needs the
# exact public URL as seen by Telnyx, which is fragile behind tunnels/proxies.
# Instead the stream_url we hand Telnyx carries an HMAC over the call
# parameters; the WebSocket handler rejects anything without a valid one, so
# nobody can open /media-stream by guessing a company_id and burn STT/LLM/TTS.
def make_stream_token(company_id: str, lead_id: Optional[str], mode: str) -> str:
    from app.core.config import settings
    msg = f"{company_id}|{lead_id or ''}|{mode}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def verify_stream_token(token: Optional[str], company_id: str, lead_id: Optional[str], mode: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, make_stream_token(company_id, lead_id, mode))


# ── Webhook signature (Ed25519 over "{timestamp}|{raw body}") ───────────────
def verify_webhook_signature(
    raw_body: bytes,
    signature_b64: Optional[str],
    timestamp: Optional[str],
    public_key_b64: Optional[str],
    tolerance_seconds: int = 300,
) -> bool:
    """True only if the signature is valid for this body+timestamp and the
    timestamp is within tolerance (replay protection)."""
    if not (signature_b64 and timestamp and public_key_b64):
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance_seconds:
            logger.warning("Telnyx webhook rejected — timestamp outside tolerance")
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), timestamp.encode() + b"|" + raw_body)
        return True
    except Exception as e:
        logger.warning(f"Telnyx webhook signature check failed: {type(e).__name__}: {e}")
        return False


def enforce_webhook_signature(raw_body: bytes, headers, company: Any) -> None:
    """Shared by every Telnyx webhook route. Raises HTTP 403 on a bad
    signature (or a missing key when TELNYX_REQUIRE_SIGNATURE is on)."""
    from fastapi import HTTPException
    from app.core.config import settings
    pub = telnyx_service.public_key(company) if company else ""
    if pub:
        if not verify_webhook_signature(
            raw_body, headers.get("telnyx-signature-ed25519"), headers.get("telnyx-timestamp"), pub,
        ):
            raise HTTPException(403, "Invalid Telnyx signature")
    elif settings.TELNYX_REQUIRE_SIGNATURE:
        raise HTTPException(403, "Telnyx signature required but no public key configured")
    else:
        logger.warning(
            "Telnyx webhook accepted UNVERIFIED — set telnyx_public_key "
            "(Settings → Telephony) to enable signature verification"
        )


# ── Media-stream handshake + serializer ─────────────────────────────────────
async def parse_telnyx_start(websocket, max_messages: int = 6) -> Dict[str, Any]:
    """Consume the handshake off the raw socket BEFORE Pipecat's transport
    starts reading. Telnyx sends {"event":"connected"} then
    {"event":"start", "stream_id": ..., "start": {"call_control_id": ...,
    "media_format": {"encoding": "PCMU", "sample_rate": 8000}}}.
    Returns {stream_id, call_control_id, encoding, sample_rate, from, to}."""
    for _ in range(max_messages):
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if msg.get("event") != "start":
            continue
        start = msg.get("start") or {}
        fmt = start.get("media_format") or {}
        parsed = {
            "stream_id": msg.get("stream_id") or "",
            "call_control_id": start.get("call_control_id") or "",
            "encoding": fmt.get("encoding") or STREAM_CODEC,
            "sample_rate": int(fmt.get("sample_rate") or 8000),
            "from": start.get("from") or "",
            "to": start.get("to") or "",
        }
        logger.info(
            f"Telnyx stream start | stream_id={parsed['stream_id'][:12]!r} "
            f"call_control_id={parsed['call_control_id'][:16]!r} "
            f"media_format=({parsed['encoding']!r}, {parsed['sample_rate']})"
        )
        return parsed
    raise RuntimeError("Telnyx WebSocket closed/never sent a 'start' event")


def build_telnyx_serializer(parsed: Dict[str, Any], api_key: str):
    """Pipecat's built-in serializer. Naming is pipecat's (confusing but
    correct): outbound_encoding = what Telnyx sends US (from the start
    event's media_format); inbound_encoding = what we send Telnyx (the
    stream_bidirectional_codec requested at dial time). auto_hang_up
    sends the REST hangup on EndFrame/CancelFrame as a safety net."""
    from pipecat.serializers.telnyx import TelnyxFrameSerializer
    return TelnyxFrameSerializer(
        stream_id=parsed["stream_id"],
        call_control_id=parsed["call_control_id"],
        outbound_encoding=parsed["encoding"],
        inbound_encoding=STREAM_CODEC,
        api_key=api_key,
        params=TelnyxFrameSerializer.InputParams(
            telnyx_sample_rate=parsed["sample_rate"],
            sample_rate=None,   # take pipeline rate from the StartFrame
            auto_hang_up=bool(api_key),
        ),
    )


def telnyx_answer_stream_url(company_id: str, lead_id: Optional[str], mode: str, product_focus: Optional[str]) -> str:
    return (
        f"{_ws_base()}/api/v1/telnyx/media-stream"
        f"?company_id={quote(company_id)}&lead_id={quote(lead_id or '')}"
        f"&mode={quote(mode or 'sales')}&product_focus={quote(product_focus or '')}"
        f"&token={make_stream_token(company_id, lead_id, mode or 'sales')}"
    )


def telnyx_webhook_url(company_id: str, lead_id: Optional[str]) -> str:
    return f"{_get_base_url()}/api/v1/telnyx/webhook?company_id={quote(company_id)}&lead_id={quote(lead_id or '')}"


class TelnyxService:

    def _creds(self, company: Any = None) -> Dict[str, str]:
        """Per-company credentials, no cross-tenant fallback — except the
        shared demo account, which may fall back to .env values for any
        blank field (same rule as VobizService._creds)."""
        api_key = getattr(company, "telnyx_api_key", None) if company else None
        conn_id = getattr(company, "telnyx_connection_id", None) if company else None
        phone = getattr(company, "telnyx_phone_number", None) if company else None

        if company and getattr(company, "is_demo_account", False) and not (api_key and conn_id and phone):
            from app.core.config import settings
            api_key = api_key or settings.TELNYX_API_KEY
            conn_id = conn_id or settings.TELNYX_CONNECTION_ID
            phone = phone or settings.TELNYX_PHONE_NUMBER
        return {"api_key": api_key or "", "connection_id": conn_id or "", "phone": phone or ""}

    def public_key(self, company: Any = None) -> str:
        key = getattr(company, "telnyx_public_key", None) if company else None
        if not key and company and getattr(company, "is_demo_account", False):
            from app.core.config import settings
            key = settings.TELNYX_PUBLIC_KEY
        return key or ""

    def _client(self, api_key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=TELNYX_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=20.0,
        )

    async def make_outbound_call(
        self,
        to_number: str,
        company_id: str,
        lead_id: Optional[str] = None,
        call_mode: str = "sales",
        company: Any = None,
        product_focus: Optional[str] = None,
    ) -> Optional[str]:
        """Dial `to_number`; returns Telnyx's call_control_id, or None on
        failure (same contract as VobizService.make_outbound_call)."""
        creds = self._creds(company)
        if not creds["api_key"]:
            logger.error(f"No Telnyx API key for company={company_id} — set it in Settings → Telephony")
            return None
        if not creds["connection_id"]:
            logger.error(f"No Telnyx connection_id (Call Control Application ID) for company={company_id}")
            return None
        if not creds["phone"]:
            logger.error(f"No Telnyx from-number for company={company_id}")
            return None

        stream_url = telnyx_answer_stream_url(company_id, lead_id, call_mode, product_focus)
        webhook_url = telnyx_webhook_url(company_id, lead_id)
        payload = {
            "connection_id": creds["connection_id"],
            "to": to_number,
            "from": creds["phone"],
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
            "stream_url": stream_url,
            "stream_track": "inbound_track",          # caller's audio only
            "stream_bidirectional_mode": "rtp",       # lets us play audio back
            "stream_bidirectional_codec": STREAM_CODEC,
        }
        logger.info(f"Telnyx outbound | to={to_number} | webhook_url={webhook_url} | ws={_ws_base()}/api/v1/telnyx/media-stream")
        try:
            async with self._client(creds["api_key"]) as client:
                resp = await client.post("/calls", json=payload)
                resp.raise_for_status()
                data = (resp.json() or {}).get("data") or {}
                ccid = data.get("call_control_id")
                logger.info(f"Telnyx outbound call started → {to_number} | call_control_id={ccid}")
                return ccid
        except httpx.HTTPStatusError as e:
            logger.error(f"Telnyx dial HTTP {e.response.status_code}: {e.response.text[:500]}")
            return None
        except Exception as e:
            logger.error(f"Telnyx dial error: {e}")
            return None

    async def dial_agent_leg(
        self, agent_number: str, company: Any, webhook_url: str, timeout_secs: int = 30,
    ) -> Optional[str]:
        """Human click-to-call, step 1: ring the AGENT's own phone from the
        company's Telnyx number (plain call, no media stream). Returns the
        agent leg's call_control_id. Step 2 (bridge_to_lead) runs when the
        agent answers (call.answered webhook)."""
        creds = self._creds(company)
        if not (creds["api_key"] and creds["connection_id"] and creds["phone"]):
            return None
        payload = {
            "connection_id": creds["connection_id"],
            "to": agent_number,
            "from": creds["phone"],
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
            "timeout_secs": timeout_secs,
        }
        try:
            async with self._client(creds["api_key"]) as client:
                resp = await client.post("/calls", json=payload)
                resp.raise_for_status()
                return ((resp.json() or {}).get("data") or {}).get("call_control_id")
        except httpx.HTTPStatusError as e:
            logger.error(f"Telnyx agent-leg dial HTTP {e.response.status_code}: {e.response.text[:500]}")
        except Exception as e:
            logger.error(f"Telnyx agent-leg dial error: {e}")
        return None

    async def bridge_to_lead(
        self, agent_call_control_id: str, lead_number: str, company: Any,
        webhook_url: str, timeout_secs: int = 30,
    ) -> bool:
        """Human click-to-call, step 2: Telnyx `transfer` dials the lead
        (leg B) and bridges it to the answered agent leg. caller id = the
        company's own Telnyx number. webhook_url overrides where leg B's
        events (answered/hangup) are sent. If leg B fails, Telnyx keeps the
        agent leg up and sends call.hangup for leg B."""
        creds = self._creds(company)
        return await self._action(
            agent_call_control_id, "transfer",
            {
                "to": lead_number,
                "from": creds["phone"],
                "webhook_url": webhook_url,
                "webhook_url_method": "POST",
                "timeout_secs": timeout_secs,
            },
            creds,
        )

    async def answer_with_stream(
        self, call_control_id: str, company: Any, stream_url: str, webhook_url: str,
    ) -> bool:
        """Inbound: answer the call and start the bidirectional stream in
        the same command (Telnyx accepts stream_* on /actions/answer)."""
        creds = self._creds(company)
        payload = {
            "stream_url": stream_url,
            "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": STREAM_CODEC,
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
        }
        return await self._action(call_control_id, "answer", payload, creds)

    async def hangup(self, call_control_id: str, company: Any = None) -> bool:
        creds = self._creds(company)
        return await self._action(call_control_id, "hangup", {}, creds)

    async def transfer(self, call_control_id: str, to_number: str, company: Any = None) -> bool:
        """Telnyx transfer action: bridges the live leg to `to_number`.
        NOTE: transferring ends our media stream. Not wired into the AI
        flow yet (Vobiz's transfer isn't either) — available for use."""
        creds = self._creds(company)
        return await self._action(
            call_control_id, "transfer", {"to": to_number, "from": creds["phone"]}, creds
        )

    async def _action(self, call_control_id: str, action: str, payload: Dict, creds: Dict) -> bool:
        if not creds["api_key"]:
            logger.error(f"Telnyx [{action}] — no API key | call_control_id={call_control_id[:16]}")
            return False
        try:
            async with self._client(creds["api_key"]) as client:
                resp = await client.post(f"/calls/{quote(call_control_id, safe='')}/actions/{action}", json=payload)
                if resp.status_code == 422 and "90018" in resp.text:
                    # "Call has already ended" — for hangup that is success.
                    logger.info(f"Telnyx [{action}] — call already ended | call_control_id={call_control_id[:16]}")
                    return action == "hangup"
                resp.raise_for_status()
                logger.info(f"Telnyx [{action}] OK | call_control_id={call_control_id[:16]}")
                return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Telnyx [{action}] HTTP {e.response.status_code}: {e.response.text[:300]} | call_control_id={call_control_id[:16]}")
            return False
        except Exception as e:
            logger.error(f"Telnyx [{action}] error: {e} | call_control_id={call_control_id[:16]}")
            return False


telnyx_service = TelnyxService()
