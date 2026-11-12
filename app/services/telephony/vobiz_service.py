"""
Vobiz REST + XML service — India carrier route, mirrors telnyx_service.py.

Architecture differs from Telnyx on purpose:
- Telnyx: REST call-control actions (POST /calls/{id}/actions/answer|speak|...),
  with call.speak.ended webhooks telling you exactly when TTS finished.
- Vobiz:  XML-driven answer_url response for call setup (Plivo-compatible),
  plus REST "Speak Text" to play TTS into an already-answered call — but
  with NO completion webhook. We estimate playback duration instead and
  sleep that long before unmuting Deepgram. See estimate_speech_seconds().

VERIFY BEFORE PRODUCTION: the exact Speak Text endpoint path/params below
are inferred from Vobiz's documented Plivo-compatible API shape — their
docs describe the feature but didn't expose a literal request schema at
time of writing. Run one real outbound test call and check your Vobiz
console's request logs against this before trusting it for campaigns.
"""
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)
VOBIZ_BASE = "https://api.vobiz.ai/api/v1"

VOBIZ_VOICE_MAP = {
    ("en-US", "female"): {"voice": "WOMAN", "language": "en-US"},
    ("en-US", "male"):   {"voice": "MAN",   "language": "en-US"},
    ("en-IN", "female"): {"voice": "WOMAN", "language": "en-IN"},
    ("en-IN", "male"):   {"voice": "MAN",   "language": "en-IN"},
    ("hi-IN", "female"): {"voice": "WOMAN", "language": "hi-IN"},
    ("hi-IN", "male"):   {"voice": "MAN",   "language": "hi-IN"},
}


def get_vobiz_voice(company: Any) -> Dict[str, str]:
    """Vobiz is the India/Hindi-Hinglish route — language is always hi-IN,
    regardless of company.voice_language (that field governs the Telnyx/
    English route only). Only voice_gender is still configurable here."""
    gender = (getattr(company, "voice_gender", None) or "female").lower()
    return VOBIZ_VOICE_MAP.get(("hi-IN", gender), {"voice": "WOMAN", "language": "hi-IN"})


def _get_base_url() -> str:
    """
    Re-reads PUBLIC_BASE_URL from the .env file directly on every call so
    updating the tunnel URL only requires restarting the one process
    you're working with — not both uvicorn and Celery simultaneously.
    Falls back to the pydantic singleton, then to localhost. Also checks
    the old TELNYX_WEBHOOK_BASE_URL env var name so existing .env files
    from before the Telnyx removal keep working without edits.
    """
    try:
        from dotenv import dotenv_values
        env = {}
        env.update(dotenv_values(".env.local") or {})
        env.update(dotenv_values(".env") or {})
        base = (env.get("PUBLIC_BASE_URL") or env.get("TELNYX_WEBHOOK_BASE_URL") or "").rstrip("/")
    except Exception:
        base = ""

    if not base:
        try:
            from app.core.config import settings
            base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
        except Exception:
            base = ""

    base = base or "http://localhost:8000"

    for suffix in [
        "/api/v1/vobiz/answer", "/api/v1/vobiz",
        "/api/v1/telephony/webhook", "/api/v1/telephony",
        "/api/v1", "/api",
    ]:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def _media_ws_url() -> str:
    base = _get_base_url()
    ws   = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{ws}/api/v1/vobiz/media"


def estimate_speech_seconds(
    text: str,
    words_per_minute: int = 150,
    min_seconds: float = 1.0,
    buffer_seconds: float = 0.5,
) -> float:
    """
    No completion webhook from Vobiz's Speak Text API, so we estimate
    playback time from word count and wait that long before unmuting
    Deepgram. buffer_seconds covers TTS synthesis latency + RTT on top
    of raw speaking time. If real calls show cut-off replies or
    premature unmuting, THIS is the one function to swap for
    checkpoint-event-based timing over a bidirectional stream instead —
    see the conversation notes in vobiz_webhook.py for that fallback path.
    """
    words = max(1, len(text.split()))
    seconds = (words / words_per_minute) * 60
    return max(min_seconds, seconds) + buffer_seconds


class VobizService:

    def _creds(self, company: Any = None) -> Dict[str, str]:
        """Per-company credentials for every real customer — no fallback
        to a shared/global credential from .env for them. Every normal
        company must set its own vobiz_auth_id/vobiz_auth_token/
        vobiz_phone_number in Settings before it can make or receive
        calls, so a company with no credentials of its own can't
        silently place calls (and rack up charges) on someone else's
        account — wrong for a genuinely multi-tenant deployment where
        each customer brings their own Vobiz account.

        ONE exception: the shared demo account (company.is_demo_account
        is True). Prospects handed the demo login shouldn't need to set
        up their own Vobiz account first, so for that account only,
        any of the three fields left blank fall back to
        settings.VOBIZ_AUTH_ID/VOBIZ_AUTH_TOKEN/VOBIZ_PHONE_NUMBER from
        .env. Real customer accounts (is_demo_account=False, the
        default) are completely unaffected by this fallback."""
        auth_id = getattr(company, "vobiz_auth_id", None) if company else None
        auth_token = getattr(company, "vobiz_auth_token", None) if company else None
        phone = getattr(company, "vobiz_phone_number", None) if company else None

        is_demo = bool(getattr(company, "is_demo_account", False)) if company else False
        if is_demo and (not auth_id or not auth_token or not phone):
            from app.core.config import settings
            auth_id = auth_id or settings.VOBIZ_AUTH_ID
            auth_token = auth_token or settings.VOBIZ_AUTH_TOKEN
            phone = phone or settings.VOBIZ_PHONE_NUMBER

        return {"auth_id": auth_id or "", "auth_token": auth_token or "", "phone": phone or ""}

    def _make_client(self, auth_id: str, auth_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=VOBIZ_BASE,
            headers={
                "X-Auth-ID":    auth_id,
                "X-Auth-Token": auth_token,
                "Content-Type": "application/json",
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
        creds = self._creds(company)
        if not creds["auth_id"] or not creds["auth_token"]:
            logger.error(f"No Vobiz credentials for company={company_id} — set vobiz_auth_id/vobiz_auth_token")
            return None
        if not creds["phone"]:
            logger.error(f"No Vobiz from-number for company={company_id} — set vobiz_phone_number")
            return None

        from app.core.config import settings
        from urllib.parse import quote

        # USE_STREAMING_CALLS gates which flow handles the call:
        #   True  (default) -> /vobiz-stream/answer-stream -> the Pipecat
        #                       bidirectional pipeline (fast, no per-turn
        #                       webhook round trip, no max_tokens hacks).
        #   False            -> /vobiz/answer -> the old Record+Gather XML
        #                       flow, kept only as a rollback switch.
        # This was previously hardcoded to the old flow — the streaming
        # route existed and was reachable, but nothing ever pointed a real
        # call at it, so every test kept exercising the old path no matter
        # what was fixed inside the streaming pipeline itself.
        use_streaming = getattr(settings, "USE_STREAMING_CALLS", True)
        answer_path = "/api/v1/vobiz-stream/answer-stream" if use_streaming else "/api/v1/vobiz/answer"
        answer_url = (
            f"{_get_base_url()}{answer_path}"
            f"?company_id={company_id}&lead_id={lead_id or ''}&mode={call_mode}"
            f"&product_focus={quote(product_focus or '')}"
        )
        hangup_url = (
            f"{_get_base_url()}/api/v1/vobiz/hangup"
            f"?company_id={company_id}&lead_id={lead_id or ''}"
        )
        logger.info(f"Vobiz outbound | answer_url={answer_url} | hangup_url={hangup_url}")

        payload = {
            "from": creds["phone"],
            "to": to_number,
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": hangup_url,
            "hangup_method": "POST",
        }
        try:
            async with self._make_client(creds["auth_id"], creds["auth_token"]) as client:
                resp = await client.post(f"/Account/{creds['auth_id']}/Call/", json=payload)
                resp.raise_for_status()
                data = resp.json()
                call_uuid = data.get("request_uuid") or data.get("call_uuid") or data.get("api_id")
                logger.info(f"Vobiz outbound call started → {to_number} | call_uuid={call_uuid}")
                return call_uuid
        except Exception as e:
            logger.error(f"Vobiz outbound error: {e}")
            return None

    async def speak_text(
        self,
        call_uuid: str,
        text: str,
        company: Any = None,
        wait_for_completion: bool = True,
    ) -> bool:
        creds = self._creds(company)
        voice_cfg = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "en-IN"}
        payload = {"text": text, "voice": voice_cfg["voice"], "language": voice_cfg["language"]}
        ok = await self._call_action(call_uuid, "Speak", payload, creds)
        if ok and wait_for_completion:
            import asyncio
            await asyncio.sleep(estimate_speech_seconds(text))
        return ok

    async def hangup(self, call_uuid: str, company: Any = None) -> bool:
        creds = self._creds(company)
        try:
            async with self._make_client(creds["auth_id"], creds["auth_token"]) as client:
                resp = await client.delete(f"/Account/{creds['auth_id']}/Call/{call_uuid}/")
                resp.raise_for_status()
                logger.info(f"Vobiz hangup OK | call_uuid={call_uuid}")
                return True
        except Exception as e:
            logger.error(f"Vobiz hangup error: {e}")
            return False

    async def transfer(self, call_uuid: str, to_number: str, company: Any = None) -> bool:
        """
        TENTATIVE — Vobiz's docs list a 'Transfer a call' endpoint but I
        couldn't confirm its exact param shape. This follows Plivo's
        documented transfer pattern (redirect the live A-leg to new XML
        via aleg_url). Test this specifically before relying on the
        'speak to a human' handoff path in production.
        """
        creds = self._creds(company)
        redirect_xml_url = f"{_get_base_url()}/api/v1/vobiz/transfer-xml?to={to_number}"
        return await self._call_action(call_uuid, "", {"aleg_url": redirect_xml_url, "aleg_method": "POST"}, creds)

    async def _call_action(self, call_uuid: str, action: str, payload: Dict, creds: Dict) -> bool:
        path = f"/Account/{creds['auth_id']}/Call/{call_uuid}/{action}/" if action else f"/Account/{creds['auth_id']}/Call/{call_uuid}/"
        try:
            async with self._make_client(creds["auth_id"], creds["auth_token"]) as client:
                resp = await client.post(path, json=payload)
                resp.raise_for_status()
                logger.info(f"Vobiz [{action or 'update'}] OK | call_uuid={call_uuid}")
                return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Vobiz [{action}] HTTP {e.response.status_code}: {e.response.text[:300]} | call_uuid={call_uuid}")
            return False
        except Exception as e:
            logger.error(f"Vobiz [{action}] error: {e} | call_uuid={call_uuid}")
            return False


vobiz_service = VobizService()