# """
# Multi-provider streaming TTS — feeds the Vobiz bidirectional media stream
# directly with 8kHz G.711 mulaw audio (Vobiz's wire format — see
# docs.vobiz.ai/concepts/streaming-websockets), so there's no resampling
# step on the outbound path where it can be avoided.

# Providers:
#   - SarvamTTSProvider    : recommended for Hindi/Hinglish. WS streaming,
#                            can output mulaw/8kHz natively (output_audio_codec
#                            param) — matches Vobiz's wire format with zero
#                            conversion.
#   - DeepgramAuraProvider : Aura-2 does NOT support Hindi (confirmed —
#                            developers.deepgram.com/docs/tts-models lists
#                            only en, es, de, fr, nl, it, ja). Only valid for
#                            English-mode calls. resolve_tts_provider() below
#                            refuses to route Hindi/Hinglish text to it.
#   - "vobiz" isn't handled here — it stays on the existing XML <Speak> flow
#     in vobiz_webhook.py, since Vobiz has no standalone synthesize-only
#     endpoint to plug into a custom stream.

# VERIFY BEFORE PRODUCTION: written against each vendor's published API
# reference as of writing, but neither has been exercised against a live
# Vobiz call in this environment. Test one real call per provider and watch
# for audio glitches (wrong sample rate / endianness) before trusting this
# for campaigns — see the "Common developer pitfalls" section of Vobiz's
# streaming docs, most of these bugs are silent (static, no clear error).
# """
# import base64
# import json
# import logging
# from typing import AsyncGenerator, Optional

# import httpx

# logger = logging.getLogger(__name__)

# # Deepgram Aura-2 language coverage as of writing. Anything outside this
# # set (which includes hi/hi-IN/hinglish) must not be routed to Deepgram.
# DEEPGRAM_AURA_SUPPORTED_LANGS = {"en", "es", "de", "fr", "nl", "it", "ja"}

# # A small curated default per gender/provider so Settings can offer a
# # sensible out-of-the-box choice; tts_voice on Company overrides this.
# SARVAM_DEFAULT_VOICE = {"female": "anushka", "male": "shubh"}
# DEEPGRAM_DEFAULT_VOICE_EN = {"female": "aura-2-luna-en", "male": "aura-2-orion-en"}


# def resolve_tts_provider(requested_provider: str, language: str) -> tuple[str, Optional[str]]:
#     """
#     Guards against picking a provider that can't actually speak the
#     requested language. Returns (effective_provider, warning_or_none).

#     language: pass "hi" / "hi-IN" / "hinglish" for your Hindi-Hinglish
#     calls, "en" for English-mode calls.
#     """
#     provider = (requested_provider or "vobiz").lower()
#     lang_short = (language or "hi").lower().split("-")[0]

#     if provider == "deepgram" and lang_short not in DEEPGRAM_AURA_SUPPORTED_LANGS:
#         warning = (
#             f"Deepgram Aura-2 does not support language='{language}' "
#             f"(supports: {sorted(DEEPGRAM_AURA_SUPPORTED_LANGS)}). "
#             f"Falling back to sarvam for this call."
#         )
#         logger.warning(warning)
#         return "sarvam", warning

#     return provider, None


# class SarvamTTSProvider:
#     """
#     WS streaming client for Sarvam's Bulbul TTS. Requests mulaw/8000 output
#     directly — see docs.sarvam.ai/api-reference-docs/text-to-speech/stream.
#     """
#     WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"

#     def __init__(self, api_key: str, model: str = "bulbul:v2"):
#         self.api_key = api_key
#         self.model = model

#     async def synthesize_stream(
#         self,
#         text: str,
#         language: str = "hi-IN",
#         gender: str = "female",
#         voice: Optional[str] = None,
#     ) -> AsyncGenerator[bytes, None]:
#         """Yields raw mulaw/8kHz audio chunks (already base64-decoded)."""
#         import websockets

#         speaker = voice or SARVAM_DEFAULT_VOICE.get(gender.lower(), "anushka")
#         headers = {"api-subscription-key": self.api_key}

#         async with websockets.connect(self.WS_URL, extra_headers=headers) as ws:
#             await ws.send(json.dumps({
#                 "type": "config",
#                 "data": {
#                     "target_language_code": language,
#                     "speaker": speaker,
#                     "model": self.model,
#                     "output_audio_codec": "mulaw",
#                     "output_audio_bitrate": "8k",
#                 },
#             }))
#             await ws.send(json.dumps({
#                 "type": "text",
#                 "data": {"text": text},
#             }))
#             await ws.send(json.dumps({"type": "flush"}))

#             async for raw_msg in ws:
#                 msg = json.loads(raw_msg)
#                 if msg.get("type") == "audio":
#                     audio_b64 = msg.get("data", {}).get("audio", "")
#                     if audio_b64:
#                         yield base64.b64decode(audio_b64)
#                 elif msg.get("type") in ("completion", "end"):
#                     break


# class DeepgramAuraProvider:
#     """
#     REST client for Deepgram Aura-2. English-only in practice — see module
#     docstring. Requests mulaw/8000 directly via encoding/sample_rate params
#     (developers.deepgram.com/docs/tts-encoding, tts-sample-rate).
#     """
#     BASE_URL = "https://api.deepgram.com/v1/speak"

#     def __init__(self, api_key: str):
#         self.api_key = api_key

#     async def synthesize_stream(
#         self,
#         text: str,
#         gender: str = "female",
#         voice: Optional[str] = None,
#     ) -> AsyncGenerator[bytes, None]:
#         """Yields raw mulaw/8kHz audio chunks. English text only."""
#         model = voice or DEEPGRAM_DEFAULT_VOICE_EN.get(gender.lower(), "aura-2-luna-en")
#         params = {"model": model, "encoding": "mulaw", "sample_rate": "8000"}
#         headers = {
#             "Authorization": f"Token {self.api_key}",
#             "Content-Type": "application/json",
#         }
#         async with httpx.AsyncClient(timeout=30.0) as client:
#             async with client.stream(
#                 "POST", self.BASE_URL, params=params, headers=headers,
#                 json={"text": text},
#             ) as resp:
#                 resp.raise_for_status()
#                 async for chunk in resp.aiter_bytes(chunk_size=1600):  # ~200ms @ 8kHz mulaw
#                     if chunk:
#                         yield chunk


# def get_tts_provider_client(provider: str, company: Optional[object] = None):
#     """Factory — instantiates the right client with the right API key."""
#     from app.core.config import settings

#     if provider == "sarvam":
#         return SarvamTTSProvider(api_key=settings.SARVAM_API_KEY or "")
#     if provider == "deepgram":
#         return DeepgramAuraProvider(api_key=settings.DEEPGRAM_API_KEY or "")
#     raise ValueError(f"{provider} has no standalone streaming client — use the Vobiz XML <Speak> flow instead")
"""
Vobiz Webhook Handler — pure XML-driven conversation loop

Architecture: everything is XML-in/XML-out. RECORD_MODE=True is the
confirmed-working path on this account — <Gather input="speech"> was
empirically confirmed NOT to work on this Vobiz account regardless of
attribute names (times out, never POSTs to the action URL), so this file
no longer tries to make Gather work. Record mode is the real flow:

  /answer     → <Play>greeting audio</Play><Record action=/recording .../>
  /recording  → download+transcribe audio → LLM reply → <Play>reply</Play><Record...>
                                                       or <Play>farewell</Play><Hangup/>
  /hangup     → save transcript, analyze, update lead

TTS PROVIDER: company.tts_provider selects "vobiz" (native <Speak>, kept
as a zero-setup fallback) or "sarvam" (recommended for Hindi — Vobiz's
own <Speak> voices don't cover Hindi well/at all; see chat history this
project). Sarvam audio is synthesized via REST, cached in memory, and
served back to Vobiz via <Play> at a short-lived URL this file exposes.

LATENCY NOTES vs the version this was built from:
  - Shared httpx.AsyncClient (module-level) instead of a new client per
    request — avoids a fresh TCP/TLS handshake every single turn.
  - The LLM reply call and the intent-detection call are independent of
    each other (intent detection only needs transcript+history, not the
    reply text or RAG context) — they now run concurrently via
    asyncio.gather() instead of one after another.
  These are the safe, structural wins available without leaving the
  Record-based (record → upload → download → transcribe) architecture,
  which has an inherent latency floor from that upload/download/silence-
  wait cycle. The only way past that floor is the bidirectional
  WebSocket streaming path discussed earlier in this project — this file
  does NOT do that; it's the lower-risk, already-confirmed-working path
  with TTS quality fixed and unnecessary sequential waits removed.
"""
import asyncio
import base64
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

import httpx
from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.models import CallLog, Company, Lead
from app.services.llm.llm_service import llm_service
from app.services.llm.rag_service import rag_service
from app.services.telephony.call_session import session_manager
from app.services.telephony.vobiz_service import get_vobiz_voice, _get_base_url, vobiz_service
from app.api.routes.live_ws import live_broadcaster

logger = logging.getLogger(__name__)
router = APIRouter()

RECORD_MODE: bool = True
DIAGNOSTIC_MODE: bool = False

_hung_up:    Set[str]        = set()
_responding: Dict[str, bool] = {}

# ── Shared HTTP client ───────────────────────────────────────────────────────
# One client reused for every Deepgram/Sarvam/Vobiz-download call instead of
# opening a fresh connection (TCP+TLS handshake) per request per turn.
_http_client: Optional[httpx.AsyncClient] = None

def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http_client


# ── In-memory TTS audio cache (Sarvam) ──────────────────────────────────────
# Vobiz's <Play> fetches audio from a URL — it can't play raw bytes we hand
# it directly. So Sarvam's synthesized audio gets stashed here under a
# short-lived token, and /tts/{token}.wav serves it back when Vobiz fetches
# it a moment later. In-memory (not disk) because this is a few-hundred-KB
# WAV clip alive for a few seconds — disk I/O would just add latency here.
_TTS_CACHE: Dict[str, bytes] = {}

def _tts_cache_put(audio_bytes: bytes) -> str:
    token = uuid.uuid4().hex
    _TTS_CACHE[token] = audio_bytes
    return token

def _tts_cache_cleanup():
    # Cheap best-effort cap on memory growth — not a real TTL, just
    # prevents unbounded growth if some tokens are never fetched.
    if len(_TTS_CACHE) > 200:
        for k in list(_TTS_CACHE.keys())[:100]:
            _TTS_CACHE.pop(k, None)


@router.get("/tts/{token}.wav")
async def serve_tts_audio(token: str):
    audio = _TTS_CACHE.pop(token, None)  # pop — Vobiz only fetches once
    if not audio:
        return Response(content=b"", media_type="audio/wav", status_code=404)
    return Response(content=audio, media_type="audio/wav")


# ── Sarvam TTS ────────────────────────────────────────────────────────────────
# Sarvam speaker names are lowercase and case-sensitive. anushka is bulbul:v2's
# default female voice; pick your preferred male voice from
# dashboard.sarvam.ai and set it here or via Company.tts_voice.
SARVAM_DEFAULT_VOICE = {"female": "anushka", "male": "abhilash"}

async def _synthesize_sarvam(text: str, company: Company) -> Optional[bytes]:
    """Returns raw WAV bytes at 8kHz (matches Vobiz telephony audio — no
    resampling needed), or None on failure (caller should fall back to
    Vobiz's native <Speak> for that turn rather than fail the whole call)."""
    api_key = getattr(settings, "SARVAM_API_KEY", None)
    if not api_key:
        logger.error("SARVAM_API_KEY not set — cannot use sarvam TTS provider")
        return None

    gender  = (getattr(company, "voice_gender", None) or "female").lower()
    speaker = getattr(company, "tts_voice", None) or SARVAM_DEFAULT_VOICE.get(gender, "anushka")

    try:
        resp = await _client().post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "target_language_code": "hi-IN",
                "speaker": speaker,
                "model": "bulbul:v2",
                "speech_sample_rate": 8000,  # matches telephony — no resample step
            },
        )
        resp.raise_for_status()
        data = resp.json()
        audios = data.get("audios") or []
        if not audios:
            logger.error(f"Sarvam TTS returned no audio | resp={data}")
            return None
        return base64.b64decode(audios[0])
    except Exception as e:
        logger.error(f"Sarvam TTS error: {e}")
        return None


# ── XML prompt builder (Speak OR Play, depending on tts_provider) ───────────

async def _xml_prompt(text: str, voice_cfg: Dict, company: Optional[Company]) -> str:
    """
    Builds the spoken part of the response — either Vobiz's native
    <Speak> (provider="vobiz") or a <Play> pointing at freshly synthesized
    Sarvam audio (provider="sarvam"). Falls back to <Speak> if Sarvam
    synthesis fails for any reason, so a transient TTS API issue doesn't
    kill the call outright.
    """
    provider = (getattr(company, "tts_provider", None) or "vobiz").lower() if company else "vobiz"

    if provider == "sarvam":
        audio_bytes = await _synthesize_sarvam(text, company)
        if audio_bytes:
            _tts_cache_cleanup()
            token = _tts_cache_put(audio_bytes)
            play_url = f"{_get_base_url()}/api/v1/vobiz/tts/{token}.wav"
            return f'<Play>{play_url}</Play>'
        logger.warning("Sarvam synthesis failed — falling back to Vobiz native Speak for this turn")

    return _xml_escape_speak(text, voice_cfg)


# ── Answer ────────────────────────────────────────────────────────────────────

@router.post("/answer")
async def answer(
    request: Request,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
    mode:       Optional[str] = "support",
):
    form      = await request.form()
    call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
    from_num  = form.get("From", "")
    to_num    = form.get("To", "")

    logger.info(f"Vobiz answer | call_uuid={call_uuid[:12] if call_uuid else '?'} | company={company_id}")

    if DIAGNOSTIC_MODE:
        logger.info("DIAGNOSTIC_MODE active — returning bare Speak XML")
        bare_xml = "<Response><Speak>Hello, this is a test call from Astric AI. The connection is working.</Speak></Response>"
        return Response(content=bare_xml, media_type="text/xml")

    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db) if company_id else None
        if not company:
            logger.error(f"Vobiz answer — no company for company_id={company_id}")
            return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

        lead = await _get_lead(lead_id, db) if lead_id else None

        call_log = CallLog(
            company_id=company.id, lead_id=lead.id if lead else None,
            direction="outbound", status="in_progress", mode=mode or "support",
            provider="vobiz",
            from_number=from_num or company.vobiz_phone_number or "",
            to_number=to_num,
            call_control_id=call_uuid, started_at=datetime.utcnow(),
        )
        db.add(call_log)
        await db.commit()
        await db.refresh(call_log)

        await session_manager.create(
            call_control_id=call_uuid, company_id=company.id,
            lead_id=lead.id if lead else None,
            direction="outbound", mode=mode or "support", call_log_id=call_log.id,
        )

        agent = company.agent_name or "Alex"
        if mode == "sales":
            first = lead.name.split()[0] if lead and lead.name else ""
            greeting = (
                company.greeting_outbound_hi
                or f"Namaste{' ' + first if first else ''} ji! Main {agent} bol raha hoon "
                   f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
            )
        else:
            greeting = (
                company.greeting_inbound_hi
                or f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
                   f"main {agent} hoon. Main aapki kaise madad kar sakta hoon?"
            )

    await session_manager.add_turn(call_uuid, "assistant", greeting)

    phone = lead.phone if lead else (to_num or "Unknown")
    await live_broadcaster.call_start(company_id, call_uuid, phone, mode or "support")
    await live_broadcaster.ai_msg(company_id, call_uuid, greeting)

    voice_cfg  = get_vobiz_voice(company)
    action_url = _make_action_url(company_id, lead_id, mode)
    prompt_xml = await _xml_prompt(greeting, voice_cfg, company)
    xml        = _xml_wrap_with_listen(prompt_xml, action_url)

    logger.info(
        f"Vobiz answer XML | mode={'Record' if RECORD_MODE else 'Gather'} | "
        f"tts={getattr(company, 'tts_provider', 'vobiz')} | "
        f"action={action_url} | call_uuid={call_uuid[:12] if call_uuid else '?'}"
    )
    return Response(content=xml, media_type="text/xml")


# ── Recording callback (Record mode) ─────────────────────────────────────────

@router.post("/recording")
async def recording_callback(
    request: Request,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
    mode:       Optional[str] = "support",
):
    form = await request.form()
    logger.info(f"Vobiz /recording hit | all_fields={dict(form)}")

    call_uuid     = form.get("CallUUID") or form.get("RequestUUID") or ""
    recording_url = (
        form.get("RecordUrl") or form.get("record_url") or
        form.get("recording_url") or form.get("RecordingUrl") or
        form.get("RecordFile") or ""
    )

    if not recording_url:
        logger.warning(f"No recording URL | call_uuid={call_uuid[:12]} — asking to repeat")
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?")

    transcript = await _transcribe_url(recording_url)
    if not transcript:
        logger.warning(f"Empty transcript | call_uuid={call_uuid[:12]} — asking to repeat")
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?")

    return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# ── Gather callback (kept only for manual fallback testing — confirmed NOT
#    reliably working on this Vobiz account; Record mode is the real path) ──

@router.post("/gather")
async def gather_callback(
    request: Request,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
    mode:       Optional[str] = "support",
):
    form = await request.form()
    logger.info(f"Vobiz /gather hit | all_fields={dict(form)}")

    call_uuid  = form.get("CallUUID") or form.get("RequestUUID") or ""
    transcript = (
        form.get("Speech") or form.get("SpeechResult") or
        form.get("speech_result") or form.get("Digits") or ""
    ).strip()

    if not transcript:
        logger.info(f"Vobiz /gather — no speech detected | call_uuid={call_uuid[:12]}")
        session = await session_manager.get(call_uuid)
        company = None
        if session:
            async with AsyncSessionLocal() as db:
                company = await _get_company(session["company_id"], db)
        voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
        action_url = _make_action_url(company_id, lead_id, mode)
        reprompt   = "Kya aap sun pa rahe hain? Kuch kehna chahte hain toh boliye."
        prompt_xml = await _xml_prompt(reprompt, voice_cfg, company)
        return Response(content=_xml_wrap_with_listen(prompt_xml, action_url), media_type="text/xml")

    return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# ── Core reply builder ────────────────────────────────────────────────────────

async def _build_reply_response(
    call_uuid: str,
    transcript: str,
    company_id: Optional[str],
    lead_id: Optional[str],
    mode: str,
) -> Response:

    logger.info(f"Transcript: '{transcript[:120]}' | call_uuid={call_uuid[:12]}")

    if call_uuid in _hung_up:
        return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

    session = await session_manager.get(call_uuid)
    if not session:
        logger.warning(f"No session | call_uuid={call_uuid[:12]}")
        return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

    async with AsyncSessionLocal() as db:
        company = await _get_company(session["company_id"], db)
        if not company:
            return Response(content="<Response><Hangup/></Response>", media_type="text/xml")
        lead = await _get_lead(session.get("lead_id"), db) if session.get("lead_id") else None

    voice_cfg  = get_vobiz_voice(company)
    action_url = _make_action_url(company_id, lead_id, mode)

    # Human transfer check
    human_words = [
        "speak to a human", "talk to a person", "real agent", "manager", "supervisor",
        "insaan se baat", "kisi aur se baat", "manager se baat",
    ]
    if any(w in transcript.lower() for w in human_words) and company.forward_number:
        await session_manager.add_turn(call_uuid, "user", transcript)
        await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)
        msg = "Bilkul, main abhi team se kisi ko connect karta hoon. Ek minute rukiye!"
        await session_manager.add_turn(call_uuid, "assistant", msg)
        await live_broadcaster.ai_msg(session["company_id"], call_uuid, msg)
        async with AsyncSessionLocal() as db:
            await _update_log(session["call_log_id"], {"transferred_to_human": True}, db)
        prompt_xml = await _xml_prompt(msg, voice_cfg, company)
        return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")

    # Build context
    await session_manager.add_turn(call_uuid, "user", transcript)
    await session_manager.set_live_transcript(call_uuid, transcript)
    session = await session_manager.get(call_uuid)

    await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)

    rag_context = ""
    try:
        rag_context = await rag_service.search(session["company_id"], transcript, n_results=3)
    except Exception as e:
        logger.debug(f"RAG error: {e}")

    prompt  = _build_hindi_prompt(company, lead, rag_context, session["mode"])
    now_iso = datetime.now().isoformat()

    # LATENCY: reply generation and intent detection are independent of
    # each other (intent detection only needs transcript+history — not
    # the reply text or RAG context), so run them concurrently instead
    # of one after another.
    async def _get_reply():
        try:
            return await llm_service.generate_response(
                messages=session["history"], system_prompt=prompt,
                max_tokens=65, temperature=0.9,
            )
        except Exception as e:
            logger.error(f"LLM error: {e}")
            return "Ek dum, main check karta hoon."

    async def _get_intent():
        try:
            return await llm_service.detect_callback_intent(
                transcript, session["history"], now_iso
            )
        except Exception:
            return {"wants_callback": False, "wants_to_end": False, "confidence": 0.0}

    reply, intent = await asyncio.gather(_get_reply(), _get_intent())

    logger.info(f"Reply: '{str(reply)[:80]}' | intent={intent} | call_uuid={call_uuid[:12]}")

    await session_manager.add_turn(call_uuid, "assistant", str(reply))
    await live_broadcaster.ai_msg(session["company_id"], call_uuid, str(reply))

    # Callback scheduling
    if intent.get("wants_callback") and intent.get("confidence", 0) >= 0.7:
        cb_dt = _parse_callback_datetime(intent.get("callback_datetime_iso"))
        if cb_dt and session.get("lead_id"):
            async with AsyncSessionLocal() as db:
                lead_obj = await _get_lead(session["lead_id"], db)
                if lead_obj:
                    lead_obj.scheduled_call_at = cb_dt
                    lead_obj.status = "contacted"
                    note = f"Requested callback: {intent.get('callback_time_raw', 'unspecified time')}"
                    lead_obj.notes = f"{lead_obj.notes or ''}\n{note}".strip()
                    await db.commit()
        prompt_xml = await _xml_prompt(str(reply), voice_cfg, company)
        return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")

    # End-of-call
    if intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9:
        prompt_xml = await _xml_prompt(str(reply), voice_cfg, company)
        return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")

    # Normal reply — continue listening
    prompt_xml = await _xml_prompt(str(reply), voice_cfg, company)
    xml = _xml_wrap_with_listen(prompt_xml, action_url)
    return Response(content=xml, media_type="text/xml")


async def _error_response(
    call_uuid: str,
    company_id: Optional[str],
    lead_id: Optional[str],
    mode: Optional[str],
    message: str,
) -> Response:
    session = await session_manager.get(call_uuid)
    company = None
    if session:
        async with AsyncSessionLocal() as db:
            company = await _get_company(session["company_id"], db)
    voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
    action_url = _make_action_url(company_id, lead_id, mode)
    prompt_xml = await _xml_prompt(message, voice_cfg, company)
    return Response(content=_xml_wrap_with_listen(prompt_xml, action_url), media_type="text/xml")


# ── Deepgram transcription ────────────────────────────────────────────────────

async def _transcribe_url(audio_url: str) -> str:
    try:
        api_key = settings.DEEPGRAM_API_KEY or ""
        if not api_key:
            logger.error("DEEPGRAM_API_KEY not set")
            return ""

        logger.info(f"Downloading recording: {audio_url}")
        vobiz_auth_id    = "MA_52FARPL9"
        vobiz_auth_token = getattr(settings, "VOBIZ_AUTH_TOKEN", "") or ""
        logger.info(f"Vobiz auth | id={vobiz_auth_id} | token={'SET' if vobiz_auth_token else 'EMPTY — add VOBIZ_AUTH_TOKEN to .env'}")

        audio_resp = await _client().get(
            audio_url,
            headers={"X-Auth-ID": vobiz_auth_id, "X-Auth-Token": vobiz_auth_token},
            auth=(vobiz_auth_id, vobiz_auth_token) if vobiz_auth_token else None,
        )
        logger.info(f"Download status: {audio_resp.status_code}")
        audio_resp.raise_for_status()
        audio_bytes = audio_resp.content

        if not audio_bytes:
            logger.error("Downloaded 0 bytes from Vobiz recording URL")
            return ""

        logger.info(f"Downloaded {len(audio_bytes)} bytes, sending to Deepgram...")

        resp = await _client().post(
            "https://api.deepgram.com/v1/listen",
            headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/mp3"},
            params={"model": "nova-2", "language": "hi", "punctuate": "true", "utterances": "false"},
            content=audio_bytes,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (
            data.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
                .strip()
        )
        logger.info(f"Deepgram transcript: '{text[:120]}'")
        return text

    except Exception as e:
        logger.error(f"Deepgram REST error: {e}")
        return ""


# ── Hangup ────────────────────────────────────────────────────────────────────

@router.post("/hangup")
async def hangup_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
):
    form      = await request.form()
    call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
    _hung_up.add(call_uuid)
    _responding.pop(call_uuid, None)
    logger.info(f"Vobiz hangup | call_uuid={call_uuid[:12]} | all_fields={dict(form)}")
    background_tasks.add_task(_finalize_hangup, call_uuid, company_id, lead_id)
    return {"result": "ok"}


async def _finalize_hangup(call_uuid: str, company_id: Optional[str], lead_id_param: Optional[str]):
    session = await session_manager.end(call_uuid)

    lead_id_for_lock = (session.get("lead_id") if session else None) or lead_id_param
    if lead_id_for_lock:
        try:
            import redis as redis_sync
            _r = redis_sync.from_url(
                getattr(settings, "REDIS_URL", "redis://localhost:6379"), decode_responses=True
            )
            bid = _r.get(f"lead_batch:{lead_id_for_lock}")
            if bid:
                _r.delete(f"batch_call_active:{bid}")
                _r.delete(f"lead_batch:{lead_id_for_lock}")
        except Exception as e:
            logger.debug(f"Batch lock clear error: {e}")

    if not session:
        return

    history     = session.get("history", [])
    call_log_id = session.get("call_log_id")
    lead_id     = session.get("lead_id")
    company_id  = session["company_id"]

    transcript = "\n".join([
        f"{'Agent' if m['role'] == 'assistant' else 'Caller'}: {m['content']}"
        for m in history
    ])

    analysis = {}
    if transcript:
        async with AsyncSessionLocal() as db:
            company = await _get_company(company_id, db)
        if company:
            try:
                analysis = await llm_service.analyze_call(
                    transcript, f"{company.name} — {company.description or ''}"
                )
            except Exception as e:
                logger.error(f"Analysis error: {e}")

    duration = 0
    if session.get("started_at"):
        try:
            started  = datetime.fromisoformat(session["started_at"])
            duration = int((datetime.utcnow() - started).total_seconds())
        except Exception:
            pass

    await live_broadcaster.call_end(company_id, call_uuid, duration)

    async with AsyncSessionLocal() as db:
        await _update_log(call_log_id, {
            "status": "completed", "ended_at": datetime.utcnow(),
            "duration_seconds": duration, "conversation_history": history,
            "transcript": transcript, "summary": analysis.get("summary", ""),
            "sentiment": analysis.get("sentiment", ""), "intent": analysis.get("intent", ""),
            "lead_status_after": analysis.get("lead_status", ""),
            "transferred_to_human": analysis.get("transferred_to_human", False),
        }, db)

        if lead_id:
            lead = await _get_lead(lead_id, db)
            if lead:
                valid = ["new","contacted","interested","warm","cold",
                         "closed_won","closed_lost","do_not_call"]
                ns = analysis.get("lead_status")
                if ns and ns in valid:
                    lead.status = ns
                iv = analysis.get("interest_level")
                if iv is not None:
                    lead.interest_level = float(iv)
                ki = analysis.get("key_info", {})
                if ki:
                    lead.key_info = {**(lead.key_info or {}), **{k: v for k, v in ki.items() if v}}
                lead.updated_at = datetime.utcnow()
                await db.commit()

    # NOTE: minutes/billing deduction removed here — the old
    # app.services.minutes_service + Firebase Firestore lookup this block
    # used to call doesn't exist anywhere in this project (confirmed —
    # there's no minutes_service.py or firebase_admin_init.py). It was
    # silently failing every call (see the repeated "Minutes deduction
    # error" warnings in your logs) and doing nothing. If you have a
    # minutes/billing system in this project's SQL models instead of
    # Firebase, wire it in here — otherwise leave this removed rather
    # than keep a permanently-failing no-op.

    await asyncio.sleep(30)
    _hung_up.discard(call_uuid)


# ── XML helpers ───────────────────────────────────────────────────────────────

def _xml_escape(text: str) -> str:
    return (text.replace("&","&amp;").replace("<","&lt;")
                .replace(">","&gt;").replace('"',"&quot;"))


def _xml_escape_speak(text: str, voice_cfg: Dict) -> str:
    return (
        f'<Speak voice="{voice_cfg["voice"]}" '
        f'language="{voice_cfg["language"]}">{_xml_escape(text)}</Speak>'
    )


def _xml_wrap_with_listen(prompt_xml: str, action_url: str) -> str:
    """
    prompt_xml is either a <Speak>...</Speak> or <Play>...</Play> block,
    already built by _xml_prompt(). This just wraps it with the
    <Record> verb that actually listens for the caller's next turn.
    """
    if RECORD_MODE:
        listen = (
            f'<Record action="{action_url}" method="POST" '
            f'maxLength="8" silence="2" finishOnKey="" />'
        )
    else:
        listen = (
            f'<Gather inputType="speech" action="{action_url}" method="POST" '
            f'language="hi-IN" executionTimeout="15" speechEndTimeout="auto">'
            f'</Gather>'
        )
    return f'<Response>{prompt_xml}{listen}</Response>'


def _make_action_url(company_id: Optional[str], lead_id: Optional[str], mode: Optional[str]) -> str:
    endpoint = "recording" if RECORD_MODE else "gather"
    return (
        f"{_get_base_url()}/api/v1/vobiz/{endpoint}"
        f"?company_id={company_id}&amp;lead_id={lead_id or ''}&amp;mode={mode or 'support'}"
    )


# ── Hindi prompt builder ──────────────────────────────────────────────────────

def _build_hindi_prompt(company: Any, lead: Any, rag_context: str, mode: str) -> str:
    agent = company.agent_name or "Aria"
    desc  = company.description_hi or company.description or ""
    serv  = company.services_hi or company.services or ""
    faqs  = company.faqs_hi or company.faqs or ""

    products_txt = ""
    for p in (company.products or []):
        name  = p.get("name_hi")  or p.get("name", "")
        pdesc = p.get("description_hi") or p.get("description", "")
        price = p.get("price", "")
        feats = p.get("features_hi") or p.get("features") or []
        products_txt += f"\n- {name} ({price}): {pdesc}"
        if feats:
            products_txt += f" | Features: {', '.join(feats)}"

    base = (
        f"Aap {agent} hain, {company.name} ke liye ek AI phone agent. "
        f"HAMESHA natural Hindi-English mix (Hinglish) mein baat karein.\n\n"
        f"Company: {company.name}\nVivaran: {desc}\nSevayein: {serv}\n"
    )
    if products_txt:
        base += f"\nProducts:{products_txt}\n"
    if faqs:
        base += f"\nFAQs:\n{faqs}\n"
    if rag_context:
        base += f"\nAdditional context:\n{rag_context}\n"

    if mode == "sales":
        ln = getattr(lead, "name", None) or ""
        base += (
            f"\nOutbound sales call. Lead: {ln or 'pata nahi'}. "
            f"Product pitch karein, interest judge karein. "
            f"Jawab CHHOTE rakhein — jaise real phone call."
        )
    else:
        base += f"\nInbound support call. Sawaal ka seedha jawab dein. CHHOTA rakhein."
    return base


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_company(company_id: str, db) -> Optional[Company]:
    r = await db.execute(select(Company).where(Company.id == company_id))
    return r.scalar_one_or_none()

async def _get_lead(lead_id: Optional[str], db) -> Optional[Lead]:
    if not lead_id:
        return None
    r = await db.execute(select(Lead).where(Lead.id == lead_id))
    return r.scalar_one_or_none()

async def _update_log(call_log_id: Optional[str], updates: dict, db):
    if not call_log_id:
        return
    r = await db.execute(select(CallLog).where(CallLog.id == call_log_id))
    log = r.scalar_one_or_none()
    if log:
        for k, v in updates.items():
            setattr(log, k, v)
        log.updated_at = datetime.utcnow()
        await db.commit()

def _parse_callback_datetime(iso_str: Optional[str]):
    if not iso_str:
        return None
    try:
        from datetime import time as dtime
        import pytz
        tz = pytz.timezone("Asia/Kolkata")
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(tz).replace(tzinfo=None)
        t = dt.time()
        if t < dtime(9, 0):
            dt = dt.replace(hour=9, minute=0, second=0)
        elif t > dtime(18, 0):
            from datetime import timedelta
            dt = (dt + timedelta(days=1)).replace(hour=9, minute=0, second=0)
        return dt
    except Exception:
        return None