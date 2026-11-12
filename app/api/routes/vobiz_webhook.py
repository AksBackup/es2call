# # """
# # Vobiz Webhook Handler — pure XML-driven conversation loop

# # Architecture: everything is XML-in/XML-out. No REST Speak in the main
# # conversation loop. Vobiz calls us → we return XML → Vobiz executes it.

# # Two verb strategies tried in order:
# #   Primary:  <Record>  → audio URL POSTed to /recording → Deepgram REST STT
# #   Fallback: <Gather input="speech"> → transcript POSTed to /gather directly

# # If you see "greeting then cut" with no /recording hit in logs, switch the
# # RECORD_MODE flag to False below to use <Gather> instead.

# # Flow (Record mode):
# #   /answer     → <Speak>greeting</Speak><Record action=/recording .../>
# #   /recording  → transcribe audio → LLM reply → <Speak>reply</Speak><Record...>
# #                                              or <Speak>farewell</Speak><Hangup/>
# #   /hangup     → save transcript, analyze, update lead, deduct minutes

# # Flow (Gather mode):
# #   /answer     → <Speak>greeting</Speak><Gather action=/gather ...>
# #   /gather     → SpeechResult already transcribed by Vobiz → LLM reply → same XML
# #   /hangup     → same

# # NOTE on duplicate /answer hits:
# #   Vobiz (like most telephony providers) can re-POST /answer for the same
# #   CallUUID if the first response was slow or ambiguous. Because
# #   CallLog.call_control_id is UNIQUE, blindly inserting a new row on every
# #   /answer call causes:
# #       sqlite3.IntegrityError: UNIQUE constraint failed: call_logs.call_control_id
# #   which bubbles up as a 500 and kills the call ("greeting then cut").
# #   The /answer handler below is now idempotent: it looks up any existing
# #   CallLog/session for the CallUUID first and reuses it instead of inserting
# #   a duplicate, and additionally guards the insert with try/except in case
# #   two requests race each other concurrently.
# # """
# # import logging
# # from datetime import datetime
# # from typing import Any, Dict, Optional, Set

# # import httpx
# # from fastapi import APIRouter, BackgroundTasks, Request, Response
# # from sqlalchemy import select
# # from sqlalchemy.exc import IntegrityError

# # from app.core.database import AsyncSessionLocal
# # from app.models.models import CallLog, Company, Lead
# # from app.services.llm.llm_service import llm_service
# # from app.services.llm.rag_service import rag_service
# # from app.services.telephony.call_session import session_manager
# # from app.services.telephony.vobiz_service import get_vobiz_voice, _get_base_url, vobiz_service
# # from app.api.routes.live_ws import live_broadcaster

# # logger = logging.getLogger(__name__)
# # router = APIRouter()

# # # Switching back to Gather mode — <Gather input="speech"> IS supported.
# # # Root cause of earlier failures: wrong attribute names.
# # # Vobiz uses inputType="speech", executionTimeout, speechEndTimeout —
# # # NOT input="speech", timeout, speechTimeout (those are Plivo/Twilio names).
# # RECORD_MODE: bool = True

# # DIAGNOSTIC_MODE: bool = False

# # _hung_up:    Set[str]        = set()
# # _responding: Dict[str, bool] = {}


# # # ── Answer ────────────────────────────────────────────────────────────────────

# # @router.post("/answer")
# # async def answer(
# #     request: Request,
# #     company_id: Optional[str] = None,
# #     lead_id:    Optional[str] = None,
# #     mode:       Optional[str] = "support",
# # ):
# #     form      = await request.form()
# #     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
# #     from_num  = form.get("From", "")
# #     to_num    = form.get("To", "")

# #     logger.info(f"Vobiz answer | call_uuid={call_uuid[:12] if call_uuid else '?'} | company={company_id}")

# #     if DIAGNOSTIC_MODE:
# #         logger.info("DIAGNOSTIC_MODE active — returning bare Speak XML")
# #         bare_xml = "<Response><Speak>Hello, this is a test call from Astric AI. The connection is working.</Speak></Response>"
# #         return Response(content=bare_xml, media_type="text/xml")

# #     async with AsyncSessionLocal() as db:
# #         company = await _get_company(company_id, db) if company_id else None
# #         if not company:
# #             logger.error(f"Vobiz answer — no company for company_id={company_id}")
# #             return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

# #         lead = await _get_lead(lead_id, db) if lead_id else None

# #         # ── Idempotency guard ──────────────────────────────────────────────
# #         # Vobiz can re-POST /answer for the same CallUUID (slow first
# #         # response, retry policy, etc). call_control_id is UNIQUE, so we
# #         # must NOT blindly insert again — look up any existing row first.
# #         existing_result = await db.execute(
# #             select(CallLog).where(CallLog.call_control_id == call_uuid)
# #         )
# #         call_log = existing_result.scalar_one_or_none()
# #         is_duplicate_answer = call_log is not None

# #         if is_duplicate_answer:
# #             logger.warning(
# #                 f"Duplicate /answer for call_uuid={call_uuid[:12] if call_uuid else '?'} "
# #                 f"— reusing existing call_log (id={call_log.id}) instead of inserting a new row"
# #             )
# #         else:
# #             call_log = CallLog(
# #                 company_id=company.id, lead_id=lead.id if lead else None,
# #                 direction="outbound", status="in_progress", mode=mode or "support",
# #                 provider="vobiz",
# #                 from_number=from_num or company.vobiz_phone_number or "",
# #                 to_number=to_num,
# #                 call_control_id=call_uuid, started_at=datetime.utcnow(),
# #             )
# #             db.add(call_log)
# #             try:
# #                 await db.commit()
# #                 await db.refresh(call_log)
# #             except IntegrityError:
# #                 # Race: another concurrent /answer request for the same
# #                 # CallUUID inserted first. Roll back and fetch that row
# #                 # instead of crashing the request.
# #                 await db.rollback()
# #                 logger.warning(
# #                     f"IntegrityError on call_control_id={call_uuid[:12] if call_uuid else '?'} "
# #                     f"— concurrent /answer race, fetching existing row"
# #                 )
# #                 existing_result = await db.execute(
# #                     select(CallLog).where(CallLog.call_control_id == call_uuid)
# #                 )
# #                 call_log = existing_result.scalar_one_or_none()
# #                 is_duplicate_answer = True
# #                 if call_log is None:
# #                     # Shouldn't happen, but fail safe instead of 500ing
# #                     logger.error(f"Could not recover CallLog after IntegrityError | call_uuid={call_uuid[:12] if call_uuid else '?'}")
# #                     return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

# #         # ── Session guard ────────────────────────────────────────────────
# #         # If a session already exists for this call (because /answer already
# #         # ran once successfully), don't recreate it or re-broadcast
# #         # "call started" — just reuse what's there.
# #         existing_session = await session_manager.get(call_uuid)

# #         if existing_session:
# #             greeting = None
# #             for turn in existing_session.get("history", []):
# #                 if turn.get("role") == "assistant":
# #                     greeting = turn.get("content")
# #                     break
# #             if not greeting:
# #                 greeting = _build_greeting(company, lead, mode)
# #         else:
# #             await session_manager.create(
# #                 call_control_id=call_uuid, company_id=company.id,
# #                 lead_id=lead.id if lead else None,
# #                 direction="outbound", mode=mode or "support", call_log_id=call_log.id,
# #             )
# #             greeting = _build_greeting(company, lead, mode)
# #             await session_manager.add_turn(call_uuid, "assistant", greeting)

# #     if not existing_session:
# #         # Only fire "call started" / first greeting broadcast once per call
# #         phone = lead.phone if lead else (to_num or "Unknown")
# #         await live_broadcaster.call_start(company_id, call_uuid, phone, mode or "support")
# #         await live_broadcaster.ai_msg(company_id, call_uuid, greeting)

# #     voice_cfg  = get_vobiz_voice(company)
# #     action_url = _make_action_url(company_id, lead_id, mode)
# #     xml        = _xml_speak_then_listen(greeting, voice_cfg, action_url)

# #     logger.info(
# #         f"Vobiz answer XML | mode={'Record' if RECORD_MODE else 'Gather'} | "
# #         f"action={action_url} | call_uuid={call_uuid[:12] if call_uuid else '?'} | "
# #         f"duplicate={is_duplicate_answer}"
# #     )
# #     return Response(content=xml, media_type="text/xml")


# # def _build_greeting(company: Any, lead: Any, mode: Optional[str]) -> str:
# #     agent = company.agent_name or "Alex"
# #     if mode == "sales":
# #         first = lead.name.split()[0] if lead and lead.name else ""
# #         return (
# #             company.greeting_outbound_hi
# #             or f"Namaste{' ' + first if first else ''} ji! Main {agent} bol raha hoon "
# #                f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
# #         )
# #     return (
# #         company.greeting_inbound_hi
# #         or f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
# #            f"main {agent} hoon. Main aapki kaise madad kar sakta hoon?"
# #     )


# # # ── Recording callback (Record mode) ─────────────────────────────────────────

# # @router.post("/recording")
# # async def recording_callback(
# #     request: Request,
# #     company_id: Optional[str] = None,
# #     lead_id:    Optional[str] = None,
# #     mode:       Optional[str] = "support",
# # ):
# #     form = await request.form()
# #     logger.info(f"Vobiz /recording hit | all_fields={dict(form)}")

# #     call_uuid     = form.get("CallUUID") or form.get("RequestUUID") or ""
# #     recording_url = (
# #         form.get("RecordUrl") or form.get("record_url") or
# #         form.get("recording_url") or form.get("RecordingUrl") or
# #         form.get("RecordFile") or ""
# #     )

# #     if not recording_url:
# #         logger.warning(f"No recording URL | call_uuid={call_uuid[:12]} — asking to repeat")
# #         return await _error_response(call_uuid, company_id, lead_id, mode,
# #                                      "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?")

# #     transcript = await _transcribe_url(recording_url)
# #     if not transcript:
# #         logger.warning(f"Empty transcript | call_uuid={call_uuid[:12]} — asking to repeat")
# #         return await _error_response(call_uuid, company_id, lead_id, mode,
# #                                      "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?")

# #     return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# # # ── Gather callback (Gather mode) ─────────────────────────────────────────────

# # @router.post("/gather")
# # async def gather_callback(
# #     request: Request,
# #     company_id: Optional[str] = None,
# #     lead_id:    Optional[str] = None,
# #     mode:       Optional[str] = "support",
# # ):
# #     form = await request.form()
# #     logger.info(f"Vobiz /gather hit | all_fields={dict(form)}")

# #     call_uuid  = form.get("CallUUID") or form.get("RequestUUID") or ""
# #     # Vobiz field names: Speech (not SpeechResult), Digits for DTMF
# #     transcript = (
# #         form.get("Speech") or form.get("SpeechResult") or
# #         form.get("speech_result") or form.get("Digits") or ""
# #     ).strip()

# #     if not transcript:
# #         logger.info(f"Vobiz /gather — no speech detected | call_uuid={call_uuid[:12]}")
# #         session = await session_manager.get(call_uuid)
# #         company = None
# #         if session:
# #             async with AsyncSessionLocal() as db:
# #                 company = await _get_company(session["company_id"], db)
# #         voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
# #         action_url = _make_action_url(company_id, lead_id, mode)
# #         reprompt   = "Kya aap sun pa rahe hain? Kuch kehna chahte hain toh boliye."
# #         return Response(
# #             content=_xml_speak_then_listen(reprompt, voice_cfg, action_url),
# #             media_type="text/xml",
# #         )

# #     return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# # # ── Core reply builder ────────────────────────────────────────────────────────

# # async def _build_reply_response(
# #     call_uuid: str,
# #     transcript: str,
# #     company_id: Optional[str],
# #     lead_id: Optional[str],
# #     mode: str,
# # ) -> Response:

# #     logger.info(f"Transcript: '{transcript[:120]}' | call_uuid={call_uuid[:12]}")

# #     if call_uuid in _hung_up:
# #         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

# #     session = await session_manager.get(call_uuid)
# #     if not session:
# #         logger.warning(f"No session | call_uuid={call_uuid[:12]}")
# #         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

# #     async with AsyncSessionLocal() as db:
# #         company = await _get_company(session["company_id"], db)
# #         if not company:
# #             return Response(content="<Response><Hangup/></Response>", media_type="text/xml")
# #         lead = await _get_lead(session.get("lead_id"), db) if session.get("lead_id") else None

# #     voice_cfg  = get_vobiz_voice(company)
# #     action_url = _make_action_url(company_id, lead_id, mode)

# #     # Human transfer check
# #     human_words = [
# #         "speak to a human", "talk to a person", "real agent", "manager", "supervisor",
# #         "insaan se baat", "kisi aur se baat", "manager se baat",
# #     ]
# #     if any(w in transcript.lower() for w in human_words) and company.forward_number:
# #         await session_manager.add_turn(call_uuid, "user", transcript)
# #         await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)
# #         msg = "Bilkul, main abhi team se kisi ko connect karta hoon. Ek minute rukiye!"
# #         await session_manager.add_turn(call_uuid, "assistant", msg)
# #         await live_broadcaster.ai_msg(session["company_id"], call_uuid, msg)
# #         async with AsyncSessionLocal() as db:
# #             await _update_log(session["call_log_id"], {"transferred_to_human": True}, db)
# #         xml = _xml_escape_speak(msg, voice_cfg) + "<Hangup/>"
# #         return Response(content=f"<Response>{xml}</Response>", media_type="text/xml")

# #     # Build context
# #     await session_manager.add_turn(call_uuid, "user", transcript)
# #     await session_manager.set_live_transcript(call_uuid, transcript)
# #     session = await session_manager.get(call_uuid)

# #     # Live — user spoke
# #     await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)

# #     rag_context = ""
# #     try:
# #         rag_context = await rag_service.search(session["company_id"], transcript, n_results=3)
# #     except Exception as e:
# #         logger.debug(f"RAG error: {e}")

# #     prompt = _build_hindi_prompt(company, lead, rag_context, session["mode"])
# #     try:
# #         reply = await llm_service.generate_response(
# #             messages=session["history"], system_prompt=prompt,
# #             max_tokens=65, temperature=0.9,
# #         )
# #     except Exception as e:
# #         logger.error(f"LLM error: {e}")
# #         reply = "Ek dum, main check karta hoon."

# #     now_iso = datetime.now().isoformat()
# #     try:
# #         intent = await llm_service.detect_callback_intent(
# #             transcript, session["history"], now_iso
# #         )
# #     except Exception:
# #         intent = {"wants_callback": False, "wants_to_end": False, "confidence": 0.0}

# #     logger.info(f"Reply: '{str(reply)[:80]}' | intent={intent} | call_uuid={call_uuid[:12]}")

# #     await session_manager.add_turn(call_uuid, "assistant", str(reply))

# #     # Live — AI replied
# #     await live_broadcaster.ai_msg(session["company_id"], call_uuid, str(reply))

# #     # Callback scheduling
# #     if intent.get("wants_callback") and intent.get("confidence", 0) >= 0.7:
# #         cb_dt = _parse_callback_datetime(intent.get("callback_datetime_iso"))
# #         if cb_dt and session.get("lead_id"):
# #             async with AsyncSessionLocal() as db:
# #                 lead_obj = await _get_lead(session["lead_id"], db)
# #                 if lead_obj:
# #                     lead_obj.scheduled_call_at = cb_dt
# #                     lead_obj.status = "contacted"
# #                     note = f"Requested callback: {intent.get('callback_time_raw', 'unspecified time')}"
# #                     lead_obj.notes = f"{lead_obj.notes or ''}\n{note}".strip()
# #                     await db.commit()
# #         xml = _xml_escape_speak(str(reply), voice_cfg) + "<Hangup/>"
# #         return Response(content=f"<Response>{xml}</Response>", media_type="text/xml")

# #     # End-of-call
# #     if intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9:
# #         xml = _xml_escape_speak(str(reply), voice_cfg) + "<Hangup/>"
# #         return Response(content=f"<Response>{xml}</Response>", media_type="text/xml")

# #     # Normal reply — continue listening
# #     xml = _xml_speak_then_listen(str(reply), voice_cfg, action_url)
# #     return Response(content=xml, media_type="text/xml")


# # async def _error_response(
# #     call_uuid: str,
# #     company_id: Optional[str],
# #     lead_id: Optional[str],
# #     mode: Optional[str],
# #     message: str,
# # ) -> Response:
# #     session = await session_manager.get(call_uuid)
# #     company = None
# #     if session:
# #         async with AsyncSessionLocal() as db:
# #             company = await _get_company(session["company_id"], db)
# #     voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
# #     action_url = _make_action_url(company_id, lead_id, mode)
# #     return Response(
# #         content=_xml_speak_then_listen(message, voice_cfg, action_url),
# #         media_type="text/xml",
# #     )


# # # ── Deepgram transcription ────────────────────────────────────────────────────

# # async def _transcribe_url(audio_url: str) -> str:
# #     try:
# #         from app.core.config import settings
# #         api_key = settings.DEEPGRAM_API_KEY or ""
# #         if not api_key:
# #             logger.error("DEEPGRAM_API_KEY not set")
# #             return ""

# #         # Step 1 — download audio from Vobiz in its own client
# #         # media.vobiz.ai requires Basic Auth (auth_id:auth_token)
# #         logger.info(f"Downloading recording: {audio_url}")
# #         # VOBIZ_AUTH_ID is always MA_OBGHKHK4 — visible in every webhook log.
# #         # VOBIZ_AUTH_TOKEN must be in .env. Get it from Vobiz dashboard → API Keys.
# #         vobiz_auth_id    = "MA_OBGHKHK4"
# #         vobiz_auth_token = getattr(settings, "VOBIZ_AUTH_TOKEN", "") or ""
# #         logger.info(f"Vobiz auth | id={vobiz_auth_id} | token={'SET' if vobiz_auth_token else 'EMPTY — add VOBIZ_AUTH_TOKEN to .env'}")
# #         async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
# #             audio_resp = await dl.get(
# #                 audio_url,
# #                 headers={
# #                     "X-Auth-ID":    vobiz_auth_id,
# #                     "X-Auth-Token": vobiz_auth_token,
# #                 },
# #                 auth=(vobiz_auth_id, vobiz_auth_token) if vobiz_auth_token else None,
# #             )
# #             logger.info(f"Download status: {audio_resp.status_code}")
# #             audio_resp.raise_for_status()
# #             audio_bytes = audio_resp.content

# #         if not audio_bytes:
# #             logger.error("Downloaded 0 bytes from Vobiz recording URL")
# #             return ""

# #         logger.info(f"Downloaded {len(audio_bytes)} bytes, sending to Deepgram...")

# #         # Step 2 — send raw bytes to Deepgram in a separate client
# #         async with httpx.AsyncClient(timeout=30.0) as dg:
# #             resp = await dg.post(
# #                 "https://api.deepgram.com/v1/listen",
# #                 headers={
# #                     "Authorization": f"Token {api_key}",
# #                     "Content-Type":  "audio/mp3",
# #                 },
# #                 params={
# #                     "model":      "nova-2",
# #                     "language":   "hi",
# #                     "punctuate":  "true",
# #                     "utterances": "false",
# #                 },
# #                 content=audio_bytes,
# #             )
# #             resp.raise_for_status()
# #             data = resp.json()
# #             text = (
# #                 data.get("results", {})
# #                     .get("channels", [{}])[0]
# #                     .get("alternatives", [{}])[0]
# #                     .get("transcript", "")
# #                     .strip()
# #             )
# #             logger.info(f"Deepgram transcript: '{text[:120]}'")
# #             return text

# #     except Exception as e:
# #         logger.error(f"Deepgram REST error: {e}")
# #         return ""


# # # ── Hangup ────────────────────────────────────────────────────────────────────

# # @router.post("/hangup")
# # async def hangup_webhook(
# #     request: Request,
# #     background_tasks: BackgroundTasks,
# #     company_id: Optional[str] = None,
# #     lead_id:    Optional[str] = None,
# # ):
# #     form      = await request.form()
# #     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
# #     _hung_up.add(call_uuid)
# #     _responding.pop(call_uuid, None)
# #     logger.info(f"Vobiz hangup | call_uuid={call_uuid[:12]}")
# #     background_tasks.add_task(_finalize_hangup, call_uuid, company_id, lead_id)
# #     return {"result": "ok"}


# # async def _finalize_hangup(call_uuid: str, company_id: Optional[str], lead_id_param: Optional[str]):
# #     session = await session_manager.end(call_uuid)

# #     lead_id_for_lock = (session.get("lead_id") if session else None) or lead_id_param
# #     if lead_id_for_lock:
# #         try:
# #             import redis as redis_sync
# #             from app.core.config import settings as _s
# #             _r = redis_sync.from_url(
# #                 getattr(_s, "REDIS_URL", "redis://localhost:6379"), decode_responses=True
# #             )
# #             bid = _r.get(f"lead_batch:{lead_id_for_lock}")
# #             if bid:
# #                 _r.delete(f"batch_call_active:{bid}")
# #                 _r.delete(f"lead_batch:{lead_id_for_lock}")
# #         except Exception as e:
# #             logger.debug(f"Batch lock clear error: {e}")

# #     if not session:
# #         return

# #     history     = session.get("history", [])
# #     call_log_id = session.get("call_log_id")
# #     lead_id     = session.get("lead_id")
# #     company_id  = session["company_id"]

# #     transcript = "\n".join([
# #         f"{'Agent' if m['role'] == 'assistant' else 'Caller'}: {m['content']}"
# #         for m in history
# #     ])

# #     analysis = {}
# #     if transcript:
# #         async with AsyncSessionLocal() as db:
# #             company = await _get_company(company_id, db)
# #         if company:
# #             try:
# #                 analysis = await llm_service.analyze_call(
# #                     transcript, f"{company.name} — {company.description or ''}"
# #                 )
# #             except Exception as e:
# #                 logger.error(f"Analysis error: {e}")

# #     duration = 0
# #     if session.get("started_at"):
# #         try:
# #             started  = datetime.fromisoformat(session["started_at"])
# #             duration = int((datetime.utcnow() - started).total_seconds())
# #         except Exception:
# #             pass

# #     # Live — call ended
# #     await live_broadcaster.call_end(company_id, call_uuid, duration)

# #     async with AsyncSessionLocal() as db:
# #         await _update_log(call_log_id, {
# #             "status": "completed", "ended_at": datetime.utcnow(),
# #             "duration_seconds": duration, "conversation_history": history,
# #             "transcript": transcript, "summary": analysis.get("summary", ""),
# #             "sentiment": analysis.get("sentiment", ""), "intent": analysis.get("intent", ""),
# #             "lead_status_after": analysis.get("lead_status", ""),
# #             "transferred_to_human": analysis.get("transferred_to_human", False),
# #         }, db)

# #         if lead_id:
# #             lead = await _get_lead(lead_id, db)
# #             if lead:
# #                 valid = ["new","contacted","interested","warm","cold",
# #                          "closed_won","closed_lost","do_not_call"]
# #                 ns = analysis.get("lead_status")
# #                 if ns and ns in valid:
# #                     lead.status = ns
# #                 iv = analysis.get("interest_level")
# #                 if iv is not None:
# #                     lead.interest_level = float(iv)
# #                 ki = analysis.get("key_info", {})
# #                 if ki:
# #                     lead.key_info = {**(lead.key_info or {}), **{k: v for k, v in ki.items() if v}}
# #                 lead.updated_at = datetime.utcnow()
# #                 await db.commit()

# #     if duration > 0:
# #         try:
# #             from app.services.minutes_service import deduct_minutes as _deduct
# #             from firebase_admin_init import get_db as _get_firestore
# #             fs  = _get_firestore()
# #             uid = None
# #             for doc in fs.collection("users").where("company_id","==",company_id).limit(1).stream():
# #                 uid = doc.id
# #                 break
# #             if uid:
# #                 _deduct(uid=uid, duration_seconds=duration)
# #         except Exception as e:
# #             logger.warning(f"Minutes deduction error: {e}")

# #     import asyncio
# #     await asyncio.sleep(30)
# #     _hung_up.discard(call_uuid)


# # # ── XML helpers ───────────────────────────────────────────────────────────────

# # def _xml_escape(text: str) -> str:
# #     return (text.replace("&","&amp;").replace("<","&lt;")
# #                 .replace(">","&gt;").replace('"',"&quot;"))


# # def _xml_escape_speak(text: str, voice_cfg: Dict) -> str:
# #     return (
# #         f'<Speak voice="{voice_cfg["voice"]}" '
# #         f'language="{voice_cfg["language"]}">{_xml_escape(text)}</Speak>'
# #     )


# # def _xml_speak_then_listen(text: str, voice_cfg: Dict, action_url: str) -> str:
# #     speak = _xml_escape_speak(text, voice_cfg)
# #     if RECORD_MODE:
# #         listen = (
# #             f'<Record action="{action_url}" method="POST" '
# #             f'maxLength="8" silence="2" finishOnKey="" />'
# #         )
# #     else:
# #         # Correct Vobiz attribute names (NOT Plivo/Twilio names):
# #         # - inputType="speech"       (not input="speech")
# #         # - executionTimeout="15"    (not timeout — valid range 5-60, default 15)
# #         # - speechEndTimeout="auto"  (not speechTimeout — valid 2-10 or auto)
# #         # Vobiz POSTs to action URL on speech OR on timeout (empty fields).
# #         listen = (
# #             f'<Gather inputType="speech" action="{action_url}" method="POST" '
# #             f'language="{voice_cfg["language"]}" executionTimeout="15" speechEndTimeout="auto">'
# #             f'</Gather>'
# #         )
# #     return f'<Response>{speak}{listen}</Response>'


# # def _make_action_url(company_id: Optional[str], lead_id: Optional[str], mode: Optional[str]) -> str:
# #     endpoint = "recording" if RECORD_MODE else "gather"
# #     return (
# #         f"{_get_base_url()}/api/v1/vobiz/{endpoint}"
# #         f"?company_id={company_id}&amp;lead_id={lead_id or ''}&amp;mode={mode or 'support'}"
# #     )


# # # ── Hindi prompt builder ──────────────────────────────────────────────────────

# # def _build_hindi_prompt(company: Any, lead: Any, rag_context: str, mode: str) -> str:
# #     agent = company.agent_name or "Aria"
# #     desc  = company.description_hi or company.description or ""
# #     serv  = company.services_hi or company.services or ""
# #     faqs  = company.faqs_hi or company.faqs or ""

# #     products_txt = ""
# #     for p in (company.products or []):
# #         name  = p.get("name_hi")  or p.get("name", "")
# #         pdesc = p.get("description_hi") or p.get("description", "")
# #         price = p.get("price", "")
# #         feats = p.get("features_hi") or p.get("features") or []
# #         products_txt += f"\n- {name} ({price}): {pdesc}"
# #         if feats:
# #             products_txt += f" | Features: {', '.join(feats)}"

# #     base = (
# #         f"Aap {agent} hain, {company.name} ke liye ek AI phone agent. "
# #         f"HAMESHA natural Hindi-English mix (Hinglish) mein baat karein.\n\n"
# #         f"Company: {company.name}\nVivaran: {desc}\nSevayein: {serv}\n"
# #     )
# #     if products_txt:
# #         base += f"\nProducts:{products_txt}\n"
# #     if faqs:
# #         base += f"\nFAQs:\n{faqs}\n"
# #     if rag_context:
# #         base += f"\nAdditional context:\n{rag_context}\n"

# #     if mode == "sales":
# #         ln = getattr(lead, "name", None) or ""
# #         base += (
# #             f"\nOutbound sales call. Lead: {ln or 'pata nahi'}. "
# #             f"Product pitch karein, interest judge karein. "
# #             f"Jawab CHHOTE rakhein — jaise real phone call."
# #         )
# #     else:
# #         base += f"\nInbound support call. Sawaal ka seedha jawab dein. CHHOTA rakhein."
# #     return base


# # # ── DB helpers ────────────────────────────────────────────────────────────────

# # async def _get_company(company_id: str, db) -> Optional[Company]:
# #     r = await db.execute(select(Company).where(Company.id == company_id))
# #     return r.scalar_one_or_none()

# # async def _get_lead(lead_id: Optional[str], db) -> Optional[Lead]:
# #     if not lead_id:
# #         return None
# #     r = await db.execute(select(Lead).where(Lead.id == lead_id))
# #     return r.scalar_one_or_none()

# # async def _update_log(call_log_id: Optional[str], updates: dict, db):
# #     if not call_log_id:
# #         return
# #     r = await db.execute(select(CallLog).where(CallLog.id == call_log_id))
# #     log = r.scalar_one_or_none()
# #     if log:
# #         for k, v in updates.items():
# #             setattr(log, k, v)
# #         log.updated_at = datetime.utcnow()
# #         await db.commit()

# # def _parse_callback_datetime(iso_str: Optional[str]):
# #     if not iso_str:
# #         return None
# #     try:
# #         from datetime import time as dtime
# #         import pytz
# #         tz = pytz.timezone("Asia/Kolkata")
# #         dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
# #         if dt.tzinfo:
# #             dt = dt.astimezone(tz).replace(tzinfo=None)
# #         t = dt.time()
# #         if t < dtime(9, 0):
# #             dt = dt.replace(hour=9, minute=0, second=0)
# #         elif t > dtime(18, 0):
# #             from datetime import timedelta
# #             dt = (dt + timedelta(days=1)).replace(hour=9, minute=0, second=0)
# #         return dt
# #     except Exception:
# #         return None
# """
# Vobiz Webhook Handler — pure XML-driven conversation loop

# Architecture: everything is XML-in/XML-out. RECORD_MODE=True is the
# confirmed-working path on this account — <Gather input="speech"> was
# empirically confirmed NOT to work on this Vobiz account regardless of
# attribute names (times out, never POSTs to the action URL), so this file
# no longer tries to make Gather work. Record mode is the real flow:

#   /answer     → <Play>greeting audio</Play><Record action=/recording .../>
#   /recording  → download+transcribe audio → LLM reply → <Play>reply</Play><Record...>
#                                                        or <Play>farewell</Play><Hangup/>
#   /hangup     → save transcript, analyze, update lead

# TTS PROVIDER: company.tts_provider selects "vobiz" (native <Speak>, kept
# as a zero-setup fallback) or "sarvam" (recommended for Hindi — Vobiz's
# own <Speak> voices don't cover Hindi well/at all; see chat history this
# project). Sarvam audio is synthesized via REST, cached in memory, and
# served back to Vobiz via <Play> at a short-lived URL this file exposes.

# SARVAM FIXES (running history):
#   1. Sarvam's TTS REST API expects the text under an `inputs` LIST field,
#      not a `text` string field. Sending `"text": "..."` got a silent 400
#      Bad Request with no useful detail unless you log the response body —
#      which is what was happening (calls fell back to Vobiz's native Speak
#      on every single turn). Fixed to send `"inputs": [text]`.
#   2. _synthesize_sarvam now logs `e.response.text` on HTTPStatusError so
#      any future Sarvam-side rejection (bad speaker name, model mismatch,
#      etc.) is visible in the logs instead of just "400 Bad Request".
#   3. MODEL UPGRADE: switched bulbul:v2 → bulbul:v3. v3 is Sarvam's newer
#      model — noticeably more natural on code-mixed Hinglish specifically
#      (handles number normalization, mixed-language prosody, etc. with
#      less preprocessing needed), which is what was behind "Sarvam sounds
#      worse than Vobiz". v2's voices sound flatter/more robotic on natural
#      Hinglish by comparison. v3 has a COMPLETELY DIFFERENT speaker
#      catalog from v2 — v2 names (anushka, hitesh, abhilash, etc.) do not
#      exist on v3 and will 400 if sent with model="bulbul:v3". v3 also
#      drops pitch/loudness controls (v2-only) in favor of a `temperature`
#      param for expressiveness — not currently set here, defaults to 0.6.

# LATENCY NOTES vs the version this was built from:
#   - Shared httpx.AsyncClient (module-level) instead of a new client per
#     request — avoids a fresh TCP/TLS handshake every single turn.
#   - The LLM reply call and the intent-detection call are independent of
#     each other (intent detection only needs transcript+history, not the
#     reply text or RAG context) — they now run concurrently via
#     asyncio.gather() instead of one after another.
#   These are the safe, structural wins available without leaving the
#   Record-based (record → upload → download → transcribe) architecture,
#   which has an inherent latency floor from that upload/download/silence-
#   wait cycle. The only way past that floor is the bidirectional
#   WebSocket streaming path discussed earlier in this project — this file
#   does NOT do that; it's the lower-risk, already-confirmed-working path
#   with TTS quality fixed and unnecessary sequential waits removed.

# KNOWN OPEN ISSUE (not fixed here — flagging so it doesn't surprise you):
#   _transcribe_url() still has the Vobiz auth ID hardcoded as a literal
#   ("MA_52FARPL9" as of this revision) instead of read from company/
#   settings. It works today because it matches the current sub-account,
#   but will silently 401 again (same failure mode as before) the moment
#   a call routes through a different Vobiz sub-account. Worth wiring to
#   company.vobiz_auth_id when you get a chance.
# """
# import asyncio
# import base64
# import logging
# import uuid
# from datetime import datetime
# from typing import Any, Dict, Optional, Set

# import httpx
# from fastapi import APIRouter, BackgroundTasks, Request, Response
# from sqlalchemy import select

# from app.core.config import settings
# from app.core.database import AsyncSessionLocal
# from app.models.models import CallLog, Company, Lead
# from app.services.llm.llm_service import llm_service
# from app.services.llm.rag_service import rag_service
# from app.services.telephony.call_session import session_manager
# from app.services.telephony.vobiz_service import get_vobiz_voice, _get_base_url, vobiz_service
# from app.api.routes.live_ws import live_broadcaster

# logger = logging.getLogger(__name__)
# router = APIRouter()

# RECORD_MODE: bool = True
# DIAGNOSTIC_MODE: bool = False

# # How long Vobiz waits for silence after the caller stops talking before it
# # finalizes the recording and POSTs to /recording. This sits directly on
# # the critical path of every turn, so it's worth tuning down from a generic
# # default — but too low risks cutting callers off mid-sentence (especially
# # with Hinglish, where speakers pause between code-switches). 0.6s was the
# # original default; 0.45-0.5s is usually still safe. TEST ON A HANDFUL OF
# # REAL CALLS before shipping a lower value — this is exactly the kind of
# # thing that looks fine on a quiet test line and clips people on a noisy
# # mobile connection.
# RECORD_SILENCE_SECONDS: float = 0.5
# RECORD_MAX_LENGTH_SECONDS: int = 8

# # ── Filler + redirect (perceived-latency trick) ─────────────────────────────
# # Record mode has a hard floor: Vobiz has to finalize the recording, POST
# # it to us, and then we still have to download + transcribe + RAG + LLM +
# # TTS before we can say anything back. Instead of the caller sitting in
# # dead air the whole time, play a short pre-cached filler clip immediately
# # and use Vobiz's <Redirect> verb to hop to a second endpoint once the
# # filler finishes — by which point the real reply is usually ready (or
# # close to it), because we kick off the actual processing in the
# # background the instant /recording is hit, not after the filler plays.
# # This overlaps real processing time with filler playback instead of just
# # masking it, so it also shaves real wall-clock, not just perceived time.
# #
# # CAVEAT: this file already has a documented case of a Vobiz verb
# # (<Gather>) not behaving reliably on this account. <Redirect> is a
# # standard verb on Plivo-style telephony XML, but VERIFY IT AGAINST A REAL
# # CALL before trusting this in production — check the logs for "/continue
# # hit" actually firing. If it doesn't work on your account, flip this to
# # False and everything falls back to the original fully-synchronous
# # behavior with zero other changes needed.
# ENABLE_FILLER_REDIRECT: bool = True
# # A pool instead of one fixed phrase — playing the exact same "Hmm, ek
# # second..." on every single turn of a multi-minute call sounds robotic
# # fast. First-bounce fillers are the ones played most often (almost every
# # turn, per production logs), so that pool is bigger; second-bounce is
# # rarer so a smaller pool is fine. _pick_filler() also avoids repeating
# # whatever was just used on THIS call.
# FILLER_POOL_1 = [
#     "Hmm, ek second...",
#     "Achha, ek min...",
#     "Theek hai, dekhta hoon...",
#     "Ji, ek second...",
# ]
# FILLER_POOL_2 = [
#     "Bas ek second aur...",
#     "Bas thoda sa aur...",
#     "Ho gaya, bas...",
# ]
# FILLER_BOUNCE_TIMEOUT = 2.5   # seconds to wait before playing another filler
# MAX_FILLER_BOUNCES = 2        # after this many, wait out the remaining budget silently

# # call_uuid -> asyncio.Task running _process_recording_turn() in the
# # background while the filler clip plays. Consumed (popped) by /continue.
# _PENDING_TURNS: Dict[str, "asyncio.Task"] = {}
# # call_uuid -> how many filler bounces have already played for this turn.
# _CONTINUE_BOUNCES: Dict[str, int] = {}
# # call_uuid -> last filler text played, so back-to-back turns don't reuse it.
# _LAST_FILLER: Dict[str, str] = {}


# def _pick_filler(call_uuid: str, pool: list) -> str:
#     import random
#     last = _LAST_FILLER.get(call_uuid)
#     choices = [p for p in pool if p != last] or pool
#     choice = random.choice(choices)
#     _LAST_FILLER[call_uuid] = choice
#     return choice

# _hung_up:    Set[str]        = set()
# _responding: Dict[str, bool] = {}

# # ── Shared HTTP client ───────────────────────────────────────────────────────
# # One client reused for every Deepgram/Sarvam/Vobiz-download call instead of
# # opening a fresh connection (TCP+TLS handshake) per request per turn.
# _http_client: Optional[httpx.AsyncClient] = None

# def _client() -> httpx.AsyncClient:
#     global _http_client
#     if _http_client is None or _http_client.is_closed:
#         _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
#     return _http_client


# # ── In-memory TTS audio cache (Sarvam) ──────────────────────────────────────
# # Vobiz's <Play> fetches audio from a URL — it can't play raw bytes we hand
# # it directly. So Sarvam's synthesized audio gets stashed here under a
# # short-lived token, and /tts/{token}.wav serves it back when Vobiz fetches
# # it a moment later. In-memory (not disk) because this is a few-hundred-KB
# # WAV clip alive for a few seconds — disk I/O would just add latency here.
# _TTS_CACHE: Dict[str, bytes] = {}

# def _tts_cache_put(audio_bytes: bytes) -> str:
#     token = uuid.uuid4().hex
#     _TTS_CACHE[token] = audio_bytes
#     return token

# def _tts_cache_cleanup():
#     # Cheap best-effort cap on memory growth — not a real TTL, just
#     # prevents unbounded growth if some tokens are never fetched.
#     if len(_TTS_CACHE) > 200:
#         for k in list(_TTS_CACHE.keys())[:100]:
#             _TTS_CACHE.pop(k, None)


# @router.get("/tts/{token}.wav")
# async def serve_tts_audio(token: str):
#     audio = _TTS_CACHE.pop(token, None)  # pop — Vobiz only fetches once
#     if not audio:
#         return Response(content=b"", media_type="audio/wav", status_code=404)
#     return Response(content=audio, media_type="audio/wav")


# # ── Sarvam TTS ────────────────────────────────────────────────────────────────
# # MODEL: bulbul:v3 (see module docstring for why this replaced bulbul:v2).
# # v3 speaker names are NOT shared with v2 — do not mix them.
# SARVAM_DEFAULT_VOICE = {"female": "sophia", "male": "shubh"}
# # other female v3 speakers worth trying: "priya", "kavya", "amelia"
# # other male v3 speakers worth trying: "aditya", "advait", "ashutosh"

# async def _synthesize_sarvam(text: str, company: Company) -> Optional[bytes]:
#     """Returns raw WAV bytes at 8kHz (matches Vobiz telephony audio — no
#     resampling needed), or None on failure (caller should fall back to
#     Vobiz's native <Speak> for that turn rather than fail the whole call)."""
#     api_key = getattr(settings, "SARVAM_API_KEY", None)
#     if not api_key:
#         logger.error("SARVAM_API_KEY not set — cannot use sarvam TTS provider")
#         return None

#     gender  = (getattr(company, "voice_gender", None) or "female").lower()
#     speaker = getattr(company, "tts_voice", None) or SARVAM_DEFAULT_VOICE.get(gender, "sophia")

#     try:
#         resp = await _client().post(
#             "https://api.sarvam.ai/text-to-speech",
#             headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
#             json={
#                 # Sarvam's TTS endpoint expects the text under an `inputs`
#                 # LIST field, not a `text` string field — sending "text"
#                 # silently 400s with no useful detail unless you log the
#                 # response body.
#                 "inputs": [text],
#                 "target_language_code": "hi-IN",
#                 "speaker": speaker,
#                 "model": "bulbul:v3",
#                 "pace": 1.0,                    # 0.5–2.0 on v3; lower = slower/calmer
#                 "enable_preprocessing": True,   # better Hinglish/number normalization
#                 "speech_sample_rate": 8000,     # matches telephony — no resample step
#                 # NOTE: pitch/loudness are v2-only params, intentionally
#                 # omitted — v3 ignores them if sent.
#             },
#         )
#         resp.raise_for_status()
#         data = resp.json()
#         audios = data.get("audios") or []
#         if not audios:
#             logger.error(f"Sarvam TTS returned no audio | resp={data}")
#             return None
#         return base64.b64decode(audios[0])
#     except httpx.HTTPStatusError as e:
#         # Log the actual response body — this is what tells you WHY
#         # Sarvam rejected the request (bad speaker name, model mismatch,
#         # auth issue, malformed payload, etc.) instead of just "400 Bad
#         # Request" with zero context.
#         body = None
#         try:
#             body = e.response.text
#         except Exception:
#             pass
#         logger.error(f"Sarvam TTS error: {e} | response_body={body}")
#         return None
#     except Exception as e:
#         logger.error(f"Sarvam TTS error: {e}")
#         return None


# # ── XML prompt builder (Speak OR Play, depending on tts_provider) ───────────

# # Fixed phrases (reprompts, error messages, human-transfer message, and
# # each company's configured greetings) get synthesized through Sarvam over
# # and over — same text, same voice, every time — even though the audio
# # never changes. Cache the resulting WAV bytes keyed by (company, voice,
# # text) so repeats are served instantly with zero TTS API round trip.
# # Small in-memory cache (short clips, capped) — not persisted across
# # restarts, but warmup_static_tts() below re-primes it on boot.
# _STATIC_TTS_CACHE: Dict[str, bytes] = {}
# _STATIC_TTS_CACHE_MAX = 500

# FIXED_PHRASES_HI = [
#     "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?",
#     "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?",
#     "Kya aap sun pa rahe hain? Kuch kehna chahte hain toh boliye.",
#     "Bilkul, main abhi team se kisi ko connect karta hoon. Ek minute rukiye!",
# ] + FILLER_POOL_1 + FILLER_POOL_2


# def _static_tts_key(company: Any, text: str) -> str:
#     gender  = (getattr(company, "voice_gender", None) or "female").lower()
#     speaker = getattr(company, "tts_voice", None) or f"default-{gender}"
#     cid     = getattr(company, "id", "") or "anon"
#     return f"{cid}|{speaker}|{text}"


# async def warmup_static_tts(companies: list) -> int:
#     """Pre-synthesize the fixed reprompt/error/transfer phrases, plus each
#     company's own greeting text, for every Sarvam-using company. Call once
#     at app startup so the first live call of the day doesn't pay a live
#     Sarvam round trip on these — they're already sitting in the cache.
#     Safe to call repeatedly; already-cached entries are skipped."""
#     n = 0
#     for company in companies:
#         if (getattr(company, "tts_provider", None) or "").lower() != "sarvam":
#             continue
#         phrases = list(FIXED_PHRASES_HI)
#         if getattr(company, "greeting_inbound_hi", None):
#             phrases.append(company.greeting_inbound_hi)
#         if getattr(company, "greeting_outbound_hi", None):
#             phrases.append(company.greeting_outbound_hi)
#         for text in phrases:
#             key = _static_tts_key(company, text)
#             if key in _STATIC_TTS_CACHE:
#                 continue
#             try:
#                 audio = await _synthesize_sarvam(text, company)
#             except Exception as e:
#                 logger.warning(f"TTS warmup error (non-fatal) | company={getattr(company, 'id', '?')}: {e}")
#                 continue
#             if audio:
#                 _STATIC_TTS_CACHE[key] = audio
#                 n += 1
#     logger.info(f"TTS warmup complete — {n} phrases pre-synthesized and cached")
#     return n


# async def _xml_prompt(text: str, voice_cfg: Dict, company: Optional[Company]) -> str:
#     """
#     Builds the spoken part of the response — either Vobiz's native
#     <Speak> (provider="vobiz") or a <Play> pointing at freshly synthesized
#     Sarvam audio (provider="sarvam"). Falls back to <Speak> if Sarvam
#     synthesis fails for any reason, so a transient TTS API issue doesn't
#     kill the call outright.
#     """
#     provider = (getattr(company, "tts_provider", None) or "vobiz").lower() if company else "vobiz"

#     if provider == "sarvam":
#         cache_key   = _static_tts_key(company, text) if company else None
#         audio_bytes = _STATIC_TTS_CACHE.get(cache_key) if cache_key else None
#         if audio_bytes:
#             logger.debug(f"Static TTS cache hit | company={getattr(company, 'id', '?')}")
#         else:
#             audio_bytes = await _synthesize_sarvam(text, company)
#             if audio_bytes and cache_key:
#                 if len(_STATIC_TTS_CACHE) >= _STATIC_TTS_CACHE_MAX:
#                     _STATIC_TTS_CACHE.pop(next(iter(_STATIC_TTS_CACHE)), None)
#                 _STATIC_TTS_CACHE[cache_key] = audio_bytes
#         if audio_bytes:
#             _tts_cache_cleanup()
#             token = _tts_cache_put(audio_bytes)
#             play_url = f"{_get_base_url()}/api/v1/vobiz/tts/{token}.wav"
#             return f'<Play>{play_url}</Play>'
#         logger.warning("Sarvam synthesis failed — falling back to Vobiz native Speak for this turn")

#     return _xml_escape_speak(text, voice_cfg)


# # ── Answer ────────────────────────────────────────────────────────────────────

# @router.post("/answer")
# async def answer(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     form      = await request.form()
#     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
#     from_num  = form.get("From", "")
#     to_num    = form.get("To", "")

#     logger.info(f"Vobiz answer | call_uuid={call_uuid[:12] if call_uuid else '?'} | company={company_id}")

#     if DIAGNOSTIC_MODE:
#         logger.info("DIAGNOSTIC_MODE active — returning bare Speak XML")
#         bare_xml = "<Response><Speak>Hello, this is a test call from Astric AI. The connection is working.</Speak></Response>"
#         return Response(content=bare_xml, media_type="text/xml")

#     async with AsyncSessionLocal() as db:
#         company = await _get_company(company_id, db) if company_id else None
#         if not company:
#             logger.error(f"Vobiz answer — no company for company_id={company_id}")
#             return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#         lead = await _get_lead(lead_id, db) if lead_id else None

#         # IDEMPOTENCY: Vobiz (like most telephony providers) can and does
#         # retry a webhook if the first response is slow or a connection
#         # blips — same CallUUID, sent again. The old code assumed /answer
#         # only ever fires once per call and unconditionally INSERTed a
#         # CallLog row, which crashes with a UNIQUE constraint violation on
#         # the retry (call_control_id already exists) → 500 → Vobiz gets no
#         # valid XML on that attempt → "Error Reaching Answer URL" (7011)
#         # and hangs up, even though the FIRST /answer attempt had already
#         # succeeded fine. Check for an existing row first instead of
#         # assuming this is always the first delivery.
#         existing = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
#         call_log = existing.scalar_one_or_none()
#         if call_log:
#             logger.warning(
#                 f"Vobiz /answer — duplicate webhook delivery for call_uuid={call_uuid[:12]} "
#                 f"(retry), reusing existing CallLog instead of inserting again"
#             )
#         else:
#             call_log = CallLog(
#                 company_id=company.id, lead_id=lead.id if lead else None,
#                 direction="outbound", status="in_progress", mode=mode or "support",
#                 provider="vobiz",
#                 from_number=from_num or company.vobiz_phone_number or "",
#                 to_number=to_num,
#                 call_control_id=call_uuid, started_at=datetime.utcnow(),
#             )
#             db.add(call_log)
#             try:
#                 await db.commit()
#                 await db.refresh(call_log)
#             except Exception:
#                 # Backstop for a true race (two near-simultaneous /answer
#                 # deliveries both passed the existence check above before
#                 # either had committed) — roll back and reuse whichever
#                 # row actually made it in, instead of 500ing.
#                 await db.rollback()
#                 existing2 = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
#                 call_log = existing2.scalar_one_or_none()
#                 if not call_log:
#                     raise  # genuinely not a duplicate-insert issue — surface the real error

#         await session_manager.create(
#             call_control_id=call_uuid, company_id=company.id,
#             lead_id=lead.id if lead else None,
#             direction="outbound", mode=mode or "support", call_log_id=call_log.id,
#             company_snapshot=_company_snapshot(company),
#         )

#         agent = company.agent_name or "Alex"
#         if mode == "sales":
#             first = lead.name.split()[0] if lead and lead.name else ""
#             greeting = (
#                 company.greeting_outbound_hi
#                 or f"Namaste{' ' + first if first else ''} ji! Main {agent} bol raha hoon "
#                    f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
#             )
#         else:
#             greeting = (
#                 company.greeting_inbound_hi
#                 or f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
#                    f"main {agent} hoon. Main aapki kaise madad kar sakta hoon?"
#             )

#     await session_manager.add_turn(call_uuid, "assistant", greeting)

#     phone = lead.phone if lead else (to_num or "Unknown")
#     await live_broadcaster.call_start(company_id, call_uuid, phone, mode or "support")
#     # FIX: nothing was ever emitting call_answered, so the Live tab stayed
#     # stuck on the "ringing / waiting for pickup" screen for the entire
#     # call and never showed the chat panel — even though messages were
#     # arriving fine underneath it. Vobiz only hits /answer once the callee
#     # has actually picked up (there's no separate pre-answer webhook in
#     # this flow), so it's correct to flip straight to answered here.
#     await live_broadcaster.call_answered(company_id, call_uuid)
#     await live_broadcaster.ai_msg(company_id, call_uuid, greeting)

#     voice_cfg  = get_vobiz_voice(company)
#     action_url = _make_action_url(company_id, lead_id, mode)
#     prompt_xml = await _xml_prompt(greeting, voice_cfg, company)
#     xml        = _xml_wrap_with_listen(prompt_xml, action_url)

#     logger.info(
#         f"Vobiz answer XML | mode={'Record' if RECORD_MODE else 'Gather'} | "
#         f"tts={getattr(company, 'tts_provider', 'vobiz')} | "
#         f"action={action_url} | call_uuid={call_uuid[:12] if call_uuid else '?'}"
#     )
#     return Response(content=xml, media_type="text/xml")


# # ── Recording callback (Record mode) ─────────────────────────────────────────

# @router.post("/recording")
# async def recording_callback(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     form = await request.form()
#     logger.info(f"Vobiz /recording hit | all_fields={dict(form)}")

#     call_uuid     = form.get("CallUUID") or form.get("RequestUUID") or ""
#     recording_url = (
#         form.get("RecordUrl") or form.get("record_url") or
#         form.get("recording_url") or form.get("RecordingUrl") or
#         form.get("RecordFile") or ""
#     )

#     if not recording_url:
#         logger.warning(f"No recording URL | call_uuid={call_uuid[:12]} — asking to repeat")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?")

#     if ENABLE_FILLER_REDIRECT and RECORD_MODE:
#         # Kick off the real work (download + transcribe + RAG + LLM + TTS)
#         # in the background RIGHT NOW, then immediately answer with a short
#         # cached filler clip + <Redirect>. By the time Vobiz finishes
#         # playing the filler and hits /continue, the background task is
#         # usually done (or nearly so) — its time overlaps with filler
#         # playback instead of stacking after it.
#         task = asyncio.create_task(
#             _process_recording_turn(call_uuid, recording_url, company_id, lead_id, mode)
#         )
#         _PENDING_TURNS[call_uuid] = task

#         session = await session_manager.get(call_uuid)
#         company = await _company_for_session(session) if session else None
#         voice_cfg = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}

#         filler_xml   = await _xml_prompt(_pick_filler(call_uuid, FILLER_POOL_1), voice_cfg, company)
#         continue_url = _make_continue_url(company_id, lead_id, mode)
#         xml = f'<Response>{filler_xml}<Redirect method="POST">{continue_url}</Redirect></Response>'
#         logger.info(f"Vobiz /recording — dispatched background turn, replying with filler | call_uuid={call_uuid[:12]}")
#         return Response(content=xml, media_type="text/xml")

#     # Filler/redirect disabled (or not Record mode) — original fully
#     # synchronous behavior, unchanged.
#     return await _process_recording_turn(call_uuid, recording_url, company_id, lead_id, mode)


# async def _process_recording_turn(
#     call_uuid: str, recording_url: str,
#     company_id: Optional[str], lead_id: Optional[str], mode: Optional[str],
# ) -> Response:
#     """The actual per-turn work: transcribe the recording, then build the
#     reply. Runs either inline (filler/redirect disabled) or as a background
#     asyncio.Task consumed by /continue (filler/redirect enabled)."""
#     transcript = await _transcribe_url(recording_url)
#     if not transcript:
#         logger.warning(f"Empty transcript | call_uuid={call_uuid[:12]} — asking to repeat")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?")

#     return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# @router.post("/continue")
# async def continue_callback(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     """Hit by Vobiz's <Redirect> once a filler clip finishes playing.
#     Picks up the background task dispatched from /recording. If it's not
#     done yet, plays ANOTHER short filler and redirects back here instead
#     of leaving the caller in dead air for the rest of the wait — up to
#     MAX_FILLER_BOUNCES times, after which it waits out the remaining
#     budget in one go."""
#     form = await request.form()
#     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
#     logger.info(f"Vobiz /continue hit | call_uuid={call_uuid[:12] if call_uuid else '?'}")

#     task = _PENDING_TURNS.get(call_uuid)
#     if task is None:
#         _CONTINUE_BOUNCES.pop(call_uuid, None)
#         logger.warning(f"Vobiz /continue — no pending turn for call_uuid={call_uuid[:12]}")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Ek second, phir se boliye.")

#     bounces = _CONTINUE_BOUNCES.get(call_uuid, 0)
#     # shield() keeps the timeout below from cancelling the background task
#     # itself — it just gives up on THIS wait and lets a later /continue
#     # hit (or the final hard-budget wait) pick the same task back up.
#     remaining_budget = 10.0 if bounces >= MAX_FILLER_BOUNCES else FILLER_BOUNCE_TIMEOUT
#     try:
#         result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining_budget)
#         _PENDING_TURNS.pop(call_uuid, None)
#         _CONTINUE_BOUNCES.pop(call_uuid, None)
#         return result
#     except asyncio.TimeoutError:
#         if bounces < MAX_FILLER_BOUNCES:
#             _CONTINUE_BOUNCES[call_uuid] = bounces + 1
#             session = await session_manager.get(call_uuid)
#             company = await _company_for_session(session) if session else None
#             voice_cfg = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
#             filler_xml   = await _xml_prompt(_pick_filler(call_uuid, FILLER_POOL_2), voice_cfg, company)
#             continue_url = _make_continue_url(company_id, lead_id, mode)
#             xml = f'<Response>{filler_xml}<Redirect method="POST">{continue_url}</Redirect></Response>'
#             logger.info(f"Vobiz /continue — still processing, bounce filler #{bounces + 1} | call_uuid={call_uuid[:12]}")
#             return Response(content=xml, media_type="text/xml")

#         _PENDING_TURNS.pop(call_uuid, None)
#         _CONTINUE_BOUNCES.pop(call_uuid, None)
#         logger.error(f"Vobiz /continue — background turn exceeded total budget | call_uuid={call_uuid[:12]}")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Maafi chahta hoon, thoda time lag raha hai. Kya aap dobara bol sakte hain?")
#     except Exception as e:
#         _PENDING_TURNS.pop(call_uuid, None)
#         _CONTINUE_BOUNCES.pop(call_uuid, None)
#         logger.error(f"Vobiz /continue — background turn failed: {e} | call_uuid={call_uuid[:12]}")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Kuch problem hua. Kya aap dobara bol sakte hain?")


# # ── Gather callback (kept only for manual fallback testing — confirmed NOT
# #    reliably working on this Vobiz account; Record mode is the real path) ──

# @router.post("/gather")
# async def gather_callback(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     form = await request.form()
#     logger.info(f"Vobiz /gather hit | all_fields={dict(form)}")

#     call_uuid  = form.get("CallUUID") or form.get("RequestUUID") or ""
#     transcript = (
#         form.get("Speech") or form.get("SpeechResult") or
#         form.get("speech_result") or form.get("Digits") or ""
#     ).strip()

#     if not transcript:
#         logger.info(f"Vobiz /gather — no speech detected | call_uuid={call_uuid[:12]}")
#         session = await session_manager.get(call_uuid)
#         company = await _company_for_session(session) if session else None
#         voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
#         action_url = _make_action_url(company_id, lead_id, mode)
#         reprompt   = "Kya aap sun pa rahe hain? Kuch kehna chahte hain toh boliye."
#         prompt_xml = await _xml_prompt(reprompt, voice_cfg, company)
#         return Response(content=_xml_wrap_with_listen(prompt_xml, action_url), media_type="text/xml")

#     return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# # ── Core reply builder ────────────────────────────────────────────────────────

# async def _build_reply_response(
#     call_uuid: str,
#     transcript: str,
#     company_id: Optional[str],
#     lead_id: Optional[str],
#     mode: str,
# ) -> Response:

#     logger.info(f"Transcript: '{transcript[:120]}' | call_uuid={call_uuid[:12]}")

#     if call_uuid in _hung_up:
#         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#     session = await session_manager.get(call_uuid)
#     if not session:
#         logger.warning(f"No session | call_uuid={call_uuid[:12]}")
#         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#     company = await _company_for_session(session)
#     if not company:
#         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")
#     async with AsyncSessionLocal() as db:
#         lead = await _get_lead(session.get("lead_id"), db) if session.get("lead_id") else None

#     voice_cfg  = get_vobiz_voice(company)
#     action_url = _make_action_url(company_id, lead_id, mode)

#     # Human transfer check
#     human_words = [
#         "speak to a human", "talk to a person", "real agent", "manager", "supervisor",
#         "insaan se baat", "kisi aur se baat", "manager se baat",
#     ]
#     if any(w in transcript.lower() for w in human_words) and company.forward_number:
#         await session_manager.add_turn(call_uuid, "user", transcript)
#         await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)
#         msg = "Bilkul, main abhi team se kisi ko connect karta hoon. Ek minute rukiye!"
#         await session_manager.add_turn(call_uuid, "assistant", msg)
#         await live_broadcaster.ai_msg(session["company_id"], call_uuid, msg)
#         async with AsyncSessionLocal() as db:
#             await _update_log(session["call_log_id"], {"transferred_to_human": True}, db)
#         prompt_xml = await _xml_prompt(msg, voice_cfg, company)
#         return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")

#     # Build context
#     await session_manager.add_turn(call_uuid, "user", transcript)
#     await session_manager.set_live_transcript(call_uuid, transcript)
#     session = await session_manager.get(call_uuid)

#     await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)

#     rag_context = ""
#     try:
#         rag_context = await rag_service.search(session["company_id"], transcript, n_results=3)
#     except Exception as e:
#         logger.debug(f"RAG error: {e}")

#     prompt  = _build_hindi_prompt(company, lead, rag_context, session["mode"])
#     now_iso = datetime.now().isoformat()

#     # LATENCY: reply generation and intent detection are independent of
#     # each other (intent detection only needs transcript+history — not
#     # the reply text or RAG context), so run them concurrently instead
#     # of one after another.
#     async def _get_reply():
#         try:
#             # LATENCY: production logs show Sarvam TTS taking 5-8s on
#             # replies generated at max_tokens=65 (~200-260 chars) — TTS
#             # synthesis time scales with text length, so this was directly
#             # adding to every turn's wall-clock time on top of network
#             # latency. Cutting the cap forces shorter, punchier replies
#             # (which also suit a real phone call better than a paragraph),
#             # and should measurably shrink the TTS step. Watch a few real
#             # calls after this change — if replies start feeling too
#             # clipped, raise it back up gradually rather than jumping to 65.
#             return await llm_service.generate_response(
#                 messages=session["history"], system_prompt=prompt,
#                 max_tokens=40, temperature=0.9,
#             )
#         except Exception as e:
#             logger.error(f"LLM error: {e}")
#             return "Ek dum, main check karta hoon."

#     async def _get_intent():
#         try:
#             return await llm_service.detect_callback_intent(
#                 transcript, session["history"], now_iso
#             )
#         except Exception:
#             return {"wants_callback": False, "wants_to_end": False, "confidence": 0.0}

#     reply, intent = await asyncio.gather(_get_reply(), _get_intent())

#     logger.info(f"Reply: '{str(reply)[:80]}' | intent={intent} | call_uuid={call_uuid[:12]}")

#     await session_manager.add_turn(call_uuid, "assistant", str(reply))
#     await live_broadcaster.ai_msg(session["company_id"], call_uuid, str(reply))

#     # Callback scheduling — note it, but do NOT hang up on this alone.
#     #
#     # BUG FIX: this used to hang up immediately whenever wants_callback
#     # fired with confidence >= 0.7. But the classifier's own prompt treats
#     # casual words like "later" / "after that" as callback signals, and a
#     # caller can just as easily use those mid-sentence to mean "later in
#     # THIS conversation" rather than "call me back another day". Caught in
#     # production: caller said "...उसके बाद बताता हूं मैं" (roughly "I'll
#     # tell you after that [once you've explained more]") and the call was
#     # hung up mid-conversation on a false positive.
#     #
#     # A genuine callback request should only end the call when the caller
#     # has ALSO actually signaled they're done (wants_to_end, high
#     # confidence) — that's a much stronger, harder-to-misfire-on signal.
#     # Otherwise: save the callback note and keep the conversation going.
#     if intent.get("wants_callback") and intent.get("confidence", 0) >= 0.7:
#         cb_dt = _parse_callback_datetime(intent.get("callback_datetime_iso"))
#         if cb_dt and session.get("lead_id"):
#             async with AsyncSessionLocal() as db:
#                 lead_obj = await _get_lead(session["lead_id"], db)
#                 if lead_obj:
#                     lead_obj.scheduled_call_at = cb_dt
#                     lead_obj.status = "contacted"
#                     note = f"Requested callback: {intent.get('callback_time_raw', 'unspecified time')}"
#                     lead_obj.notes = f"{lead_obj.notes or ''}\n{note}".strip()
#                     await db.commit()
#         if not (intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9):
#             prompt_xml = await _xml_prompt(str(reply), voice_cfg, company)
#             xml = _xml_wrap_with_listen(prompt_xml, action_url)
#             return Response(content=xml, media_type="text/xml")

#     # End-of-call — the caller has actually indicated they're done (bye,
#     # not interested, stop calling, etc.), independent of any callback
#     # note above.
#     if intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9:
#         prompt_xml = await _xml_prompt(str(reply), voice_cfg, company)
#         return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")

#     # Normal reply — continue listening
#     prompt_xml = await _xml_prompt(str(reply), voice_cfg, company)
#     xml = _xml_wrap_with_listen(prompt_xml, action_url)
#     return Response(content=xml, media_type="text/xml")


# async def _error_response(
#     call_uuid: str,
#     company_id: Optional[str],
#     lead_id: Optional[str],
#     mode: Optional[str],
#     message: str,
# ) -> Response:
#     session = await session_manager.get(call_uuid)
#     company = await _company_for_session(session) if session else None
#     voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
#     action_url = _make_action_url(company_id, lead_id, mode)
#     prompt_xml = await _xml_prompt(message, voice_cfg, company)
#     return Response(content=_xml_wrap_with_listen(prompt_xml, action_url), media_type="text/xml")


# # ── Deepgram transcription ────────────────────────────────────────────────────

# async def _transcribe_url(audio_url: str) -> str:
#     try:
#         api_key = settings.DEEPGRAM_API_KEY or ""
#         if not api_key:
#             logger.error("DEEPGRAM_API_KEY not set")
#             return ""

#         logger.info(f"Downloading recording: {audio_url}")
#         # FIX: was hardcoded as a literal (previously "MA_OBGHKHK4", then
#         # "MA_52FARPL9") — broke every time the account/sub-account this
#         # was hardcoded for wasn't the one Vobiz actually routed the call
#         # through, since Vobiz then 401s the recording download. Now read
#         # from settings (backed by .env: VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN).
#         vobiz_auth_id    = getattr(settings, "VOBIZ_AUTH_ID", "") or ""
#         vobiz_auth_token = getattr(settings, "VOBIZ_AUTH_TOKEN", "") or ""
#         if not vobiz_auth_id:
#             logger.error("VOBIZ_AUTH_ID not set — add it to .env, recording download will 401")
#         logger.info(f"Vobiz auth | id={vobiz_auth_id or 'EMPTY — add VOBIZ_AUTH_ID to .env'} | token={'SET' if vobiz_auth_token else 'EMPTY — add VOBIZ_AUTH_TOKEN to .env'}")

#         audio_resp = await _client().get(
#             audio_url,
#             headers={"X-Auth-ID": vobiz_auth_id, "X-Auth-Token": vobiz_auth_token},
#             auth=(vobiz_auth_id, vobiz_auth_token) if vobiz_auth_token else None,
#         )
#         logger.info(f"Download status: {audio_resp.status_code}")
#         audio_resp.raise_for_status()
#         audio_bytes = audio_resp.content

#         if not audio_bytes:
#             logger.error("Downloaded 0 bytes from Vobiz recording URL")
#             return ""

#         logger.info(f"Downloaded {len(audio_bytes)} bytes, sending to Deepgram...")

#         resp = await _client().post(
#             "https://api.deepgram.com/v1/listen",
#             headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/mp3"},
#             params={"model": "nova-2", "language": "hi", "punctuate": "true", "utterances": "false"},
#             content=audio_bytes,
#         )
#         resp.raise_for_status()
#         data = resp.json()
#         text = (
#             data.get("results", {})
#                 .get("channels", [{}])[0]
#                 .get("alternatives", [{}])[0]
#                 .get("transcript", "")
#                 .strip()
#         )
#         logger.info(f"Deepgram transcript: '{text[:120]}'")
#         return text

#     except Exception as e:
#         logger.error(f"Deepgram REST error: {e}")
#         return ""


# # ── Hangup ────────────────────────────────────────────────────────────────────

# @router.post("/hangup")
# async def hangup_webhook(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
# ):
#     form      = await request.form()
#     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
#     _hung_up.add(call_uuid)
#     _responding.pop(call_uuid, None)
#     # Clean up the per-call tracking dicts used by the filler/redirect
#     # flow — the background task (if any) either already finished or its
#     # result is now moot since the caller hung up.
#     _PENDING_TURNS.pop(call_uuid, None)
#     _CONTINUE_BOUNCES.pop(call_uuid, None)
#     _LAST_FILLER.pop(call_uuid, None)
#     logger.info(f"Vobiz hangup | call_uuid={call_uuid[:12]} | all_fields={dict(form)}")
#     background_tasks.add_task(_finalize_hangup, call_uuid, company_id, lead_id)
#     return {"result": "ok"}


# async def _finalize_hangup(call_uuid: str, company_id: Optional[str], lead_id_param: Optional[str]):
#     session = await session_manager.end(call_uuid)

#     lead_id_for_lock = (session.get("lead_id") if session else None) or lead_id_param
#     if lead_id_for_lock:
#         try:
#             import redis as redis_sync
#             _r = redis_sync.from_url(
#                 getattr(settings, "REDIS_URL", "redis://localhost:6379"), decode_responses=True
#             )
#             bid = _r.get(f"lead_batch:{lead_id_for_lock}")
#             if bid:
#                 _r.delete(f"batch_call_active:{bid}")
#                 _r.delete(f"lead_batch:{lead_id_for_lock}")
#         except Exception as e:
#             logger.debug(f"Batch lock clear error: {e}")

#     if not session:
#         return

#     history     = session.get("history", [])
#     call_log_id = session.get("call_log_id")
#     lead_id     = session.get("lead_id")
#     company_id  = session["company_id"]

#     transcript = "\n".join([
#         f"{'Agent' if m['role'] == 'assistant' else 'Caller'}: {m['content']}"
#         for m in history
#     ])

#     analysis = {}
#     if transcript:
#         async with AsyncSessionLocal() as db:
#             company = await _get_company(company_id, db)
#         if company:
#             try:
#                 analysis = await llm_service.analyze_call(
#                     transcript, f"{company.name} — {company.description or ''}"
#                 )
#             except Exception as e:
#                 logger.error(f"Analysis error: {e}")

#     duration = 0
#     if session.get("started_at"):
#         try:
#             started  = datetime.fromisoformat(session["started_at"])
#             duration = int((datetime.utcnow() - started).total_seconds())
#         except Exception:
#             pass

#     await live_broadcaster.call_end(company_id, call_uuid, duration)

#     async with AsyncSessionLocal() as db:
#         await _update_log(call_log_id, {
#             "status": "completed", "ended_at": datetime.utcnow(),
#             "duration_seconds": duration, "conversation_history": history,
#             "transcript": transcript, "summary": analysis.get("summary", ""),
#             "sentiment": analysis.get("sentiment", ""), "intent": analysis.get("intent", ""),
#             "lead_status_after": analysis.get("lead_status", ""),
#             "transferred_to_human": analysis.get("transferred_to_human", False),
#         }, db)

#         if lead_id:
#             lead = await _get_lead(lead_id, db)
#             if lead:
#                 valid = ["new","contacted","interested","warm","cold",
#                          "closed_won","closed_lost","do_not_call"]
#                 ns = analysis.get("lead_status")
#                 if ns and ns in valid:
#                     lead.status = ns
#                 iv = analysis.get("interest_level")
#                 if iv is not None:
#                     lead.interest_level = float(iv)
#                 ki = analysis.get("key_info", {})
#                 if ki:
#                     lead.key_info = {**(lead.key_info or {}), **{k: v for k, v in ki.items() if v}}
#                 lead.updated_at = datetime.utcnow()
#                 await db.commit()

#     # NOTE: minutes/billing deduction removed here — the old
#     # app.services.minutes_service + Firebase Firestore lookup this block
#     # used to call doesn't exist anywhere in this project (confirmed —
#     # there's no minutes_service.py or firebase_admin_init.py). It was
#     # silently failing every call (see the repeated "Minutes deduction
#     # error" warnings in your logs) and doing nothing. If you have a
#     # minutes/billing system in this project's SQL models instead of
#     # Firebase, wire it in here — otherwise leave this removed rather
#     # than keep a permanently-failing no-op.

#     await asyncio.sleep(30)
#     _hung_up.discard(call_uuid)


# # ── XML helpers ───────────────────────────────────────────────────────────────

# def _xml_escape(text: str) -> str:
#     return (text.replace("&","&amp;").replace("<","&lt;")
#                 .replace(">","&gt;").replace('"',"&quot;"))


# def _xml_escape_speak(text: str, voice_cfg: Dict) -> str:
#     return (
#         f'<Speak voice="{voice_cfg["voice"]}" '
#         f'language="{voice_cfg["language"]}">{_xml_escape(text)}</Speak>'
#     )


# def _xml_wrap_with_listen(prompt_xml: str, action_url: str) -> str:
#     """
#     prompt_xml is either a <Speak>...</Speak> or <Play>...</Play> block,
#     already built by _xml_prompt(). This just wraps it with the
#     <Record> verb that actually listens for the caller's next turn.
#     """
#     if RECORD_MODE:
#         # REVERTED: previously tried adding timeout="..." alongside
#         # silence="..." to fix recordings running to the full maxLength
#         # cap (see below). That guess was wrong — Vobiz's XML validator
#         # rejected the extra attribute outright and hung up the call
#         # immediately with HangupCauseName="Invalid Answer XML" before the
#         # caller heard anything. Back to the single known-working
#         # attribute. The maxLength-cap issue is still real (see prior
#         # logs) but needs the actual attribute name confirmed from
#         # Vobiz's docs/support before touching this again — guessing
#         # unknown attribute names on this provider is unsafe, not just
#         # ineffective; it can invalidate the whole response.
#         listen = (
#             f'<Record action="{action_url}" method="POST" '
#             f'maxLength="{RECORD_MAX_LENGTH_SECONDS}" silence="{RECORD_SILENCE_SECONDS}" '
#             f'finishOnKey="" />'
#         )
#     else:
#         listen = (
#             f'<Gather inputType="speech" action="{action_url}" method="POST" '
#             f'language="hi-IN" executionTimeout="15" speechEndTimeout="auto">'
#             f'</Gather>'
#         )
#     return f'<Response>{prompt_xml}{listen}</Response>'


# def _make_action_url(company_id: Optional[str], lead_id: Optional[str], mode: Optional[str]) -> str:
#     endpoint = "recording" if RECORD_MODE else "gather"
#     return (
#         f"{_get_base_url()}/api/v1/vobiz/{endpoint}"
#         f"?company_id={company_id}&amp;lead_id={lead_id or ''}&amp;mode={mode or 'support'}"
#     )


# def _make_continue_url(company_id: Optional[str], lead_id: Optional[str], mode: Optional[str]) -> str:
#     return (
#         f"{_get_base_url()}/api/v1/vobiz/continue"
#         f"?company_id={company_id}&amp;lead_id={lead_id or ''}&amp;mode={mode or 'support'}"
#     )


# # ── Hindi prompt builder ──────────────────────────────────────────────────────

# def _build_hindi_prompt(company: Any, lead: Any, rag_context: str, mode: str) -> str:
#     agent = company.agent_name or "Aria"
#     desc  = company.description_hi or company.description or ""
#     serv  = company.services_hi or company.services or ""
#     faqs  = company.faqs_hi or company.faqs or ""

#     products_txt = ""
#     for p in (company.products or []):
#         name  = p.get("name_hi")  or p.get("name", "")
#         pdesc = p.get("description_hi") or p.get("description", "")
#         price = p.get("price", "")
#         feats = p.get("features_hi") or p.get("features") or []
#         products_txt += f"\n- {name} ({price}): {pdesc}"
#         if feats:
#             products_txt += f" | Features: {', '.join(feats)}"

#     base = (
#         f"Aap {agent} hain, {company.name} ke liye ek AI phone agent. "
#         f"HAMESHA natural Hindi-English mix (Hinglish) mein baat karein.\n\n"
#         f"Company: {company.name}\nVivaran: {desc}\nSevayein: {serv}\n"
#     )
#     if products_txt:
#         base += f"\nProducts:{products_txt}\n"
#     if faqs:
#         base += f"\nFAQs:\n{faqs}\n"
#     if rag_context:
#         base += f"\nAdditional context:\n{rag_context}\n"

#     if mode == "sales":
#         ln = getattr(lead, "name", None) or ""
#         base += (
#             f"\nOutbound sales call. Lead: {ln or 'pata nahi'}. "
#             f"Product pitch karein, interest judge karein. "
#             f"Jawab BAHUT CHHOTE rakhein — 1 chhota sentence, kabhi kabhi 2. "
#             f"Jaise real phone call pe log bolte hain, lecture nahi."
#         )
#     else:
#         base += (
#             f"\nInbound support call. Sawaal ka seedha jawab dein. "
#             f"BAHUT CHHOTA rakhein — 1 chhota sentence, kabhi kabhi 2."
#         )
#     return base


# # ── DB helpers ────────────────────────────────────────────────────────────────

# async def _get_company(company_id: str, db) -> Optional[Company]:
#     r = await db.execute(select(Company).where(Company.id == company_id))
#     return r.scalar_one_or_none()

# async def _get_lead(lead_id: Optional[str], db) -> Optional[Lead]:
#     if not lead_id:
#         return None
#     r = await db.execute(select(Lead).where(Lead.id == lead_id))
#     return r.scalar_one_or_none()

# # Fields the per-turn reply/TTS path actually reads off `company` (voice
# # config, prompts, business info). Snapshotted into the Redis session at
# # /answer so _build_reply_response / _error_response / gather_callback
# # don't each re-hit Postgres for the same row every single turn — company
# # config doesn't change mid-call.
# _COMPANY_SNAPSHOT_FIELDS = [
#     "id", "name", "agent_name", "description", "description_hi",
#     "services", "services_hi", "faqs", "faqs_hi", "products", "active_product",
#     "voice_gender", "tts_provider", "tts_voice", "forward_number",
#     "greeting_inbound_hi", "greeting_outbound_hi", "vobiz_phone_number",
# ]

# def _company_snapshot(company: Company) -> Dict:
#     return {f: getattr(company, f, None) for f in _COMPANY_SNAPSHOT_FIELDS}

# def _company_from_snapshot(snapshot: Optional[Dict]) -> Optional[Any]:
#     """Rebuilds a lightweight attribute-access view of Company from the
#     cached snapshot dict — every place that reads `company.X` downstream
#     (get_vobiz_voice, _xml_prompt, _synthesize_sarvam, _build_hindi_prompt)
#     only ever does getattr(), so a SimpleNamespace works as a drop-in."""
#     if not snapshot:
#         return None
#     from types import SimpleNamespace
#     return SimpleNamespace(**snapshot)

# async def _company_for_session(session: Dict, db_fallback=True) -> Optional[Any]:
#     """Preferred way to get a company-like object for a turn: read the
#     cached snapshot from the session (no DB hit) and only fall back to a
#     real Postgres query if the session predates this cache or the
#     snapshot is missing for some other reason."""
#     company = _company_from_snapshot(session.get("company_snapshot"))
#     if company is not None:
#         return company
#     if not db_fallback:
#         return None
#     async with AsyncSessionLocal() as db:
#         return await _get_company(session["company_id"], db)

# async def _update_log(call_log_id: Optional[str], updates: dict, db):
#     if not call_log_id:
#         return
#     r = await db.execute(select(CallLog).where(CallLog.id == call_log_id))
#     log = r.scalar_one_or_none()
#     if log:
#         for k, v in updates.items():
#             setattr(log, k, v)
#         log.updated_at = datetime.utcnow()
#         await db.commit()

# def _parse_callback_datetime(iso_str: Optional[str]):
#     if not iso_str:
#         return None
#     try:
#         from datetime import time as dtime
#         import pytz
#         tz = pytz.timezone("Asia/Kolkata")
#         dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
#         if dt.tzinfo:
#             dt = dt.astimezone(tz).replace(tzinfo=None)
#         t = dt.time()
#         if t < dtime(9, 0):
#             dt = dt.replace(hour=9, minute=0, second=0)
#         elif t > dtime(18, 0):
#             from datetime import timedelta
#             dt = (dt + timedelta(days=1)).replace(hour=9, minute=0, second=0)
#         return dt
#     except Exception:
#         return None
# """
# Vobiz Webhook Handler — pure XML-driven conversation loop

# Architecture: everything is XML-in/XML-out. No REST Speak in the main
# conversation loop. Vobiz calls us → we return XML → Vobiz executes it.

# Two verb strategies tried in order:
#   Primary:  <Record>  → audio URL POSTed to /recording → Deepgram REST STT
#   Fallback: <Gather input="speech"> → transcript POSTed to /gather directly

# If you see "greeting then cut" with no /recording hit in logs, switch the
# RECORD_MODE flag to False below to use <Gather> instead.

# Flow (Record mode):
#   /answer     → <Speak>greeting</Speak><Record action=/recording .../>
#   /recording  → transcribe audio → LLM reply → <Speak>reply</Speak><Record...>
#                                              or <Speak>farewell</Speak><Hangup/>
#   /hangup     → save transcript, analyze, update lead, deduct minutes

# Flow (Gather mode):
#   /answer     → <Speak>greeting</Speak><Gather action=/gather ...>
#   /gather     → SpeechResult already transcribed by Vobiz → LLM reply → same XML
#   /hangup     → same

# NOTE on duplicate /answer hits:
#   Vobiz (like most telephony providers) can re-POST /answer for the same
#   CallUUID if the first response was slow or ambiguous. Because
#   CallLog.call_control_id is UNIQUE, blindly inserting a new row on every
#   /answer call causes:
#       sqlite3.IntegrityError: UNIQUE constraint failed: call_logs.call_control_id
#   which bubbles up as a 500 and kills the call ("greeting then cut").
#   The /answer handler below is now idempotent: it looks up any existing
#   CallLog/session for the CallUUID first and reuses it instead of inserting
#   a duplicate, and additionally guards the insert with try/except in case
#   two requests race each other concurrently.
# """
# import logging
# from datetime import datetime
# from typing import Any, Dict, Optional, Set

# import httpx
# from fastapi import APIRouter, BackgroundTasks, Request, Response
# from sqlalchemy import select
# from sqlalchemy.exc import IntegrityError

# from app.core.database import AsyncSessionLocal
# from app.models.models import CallLog, Company, Lead
# from app.services.llm.llm_service import llm_service
# from app.services.llm.rag_service import rag_service
# from app.services.telephony.call_session import session_manager
# from app.services.telephony.vobiz_service import get_vobiz_voice, _get_base_url, vobiz_service
# from app.api.routes.live_ws import live_broadcaster

# logger = logging.getLogger(__name__)
# router = APIRouter()

# # Switching back to Gather mode — <Gather input="speech"> IS supported.
# # Root cause of earlier failures: wrong attribute names.
# # Vobiz uses inputType="speech", executionTimeout, speechEndTimeout —
# # NOT input="speech", timeout, speechTimeout (those are Plivo/Twilio names).
# RECORD_MODE: bool = True

# DIAGNOSTIC_MODE: bool = False

# _hung_up:    Set[str]        = set()
# _responding: Dict[str, bool] = {}


# # ── Answer ────────────────────────────────────────────────────────────────────

# @router.post("/answer")
# async def answer(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     form      = await request.form()
#     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
#     from_num  = form.get("From", "")
#     to_num    = form.get("To", "")

#     logger.info(f"Vobiz answer | call_uuid={call_uuid[:12] if call_uuid else '?'} | company={company_id}")

#     if DIAGNOSTIC_MODE:
#         logger.info("DIAGNOSTIC_MODE active — returning bare Speak XML")
#         bare_xml = "<Response><Speak>Hello, this is a test call from Astric AI. The connection is working.</Speak></Response>"
#         return Response(content=bare_xml, media_type="text/xml")

#     async with AsyncSessionLocal() as db:
#         company = await _get_company(company_id, db) if company_id else None
#         if not company:
#             logger.error(f"Vobiz answer — no company for company_id={company_id}")
#             return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#         lead = await _get_lead(lead_id, db) if lead_id else None

#         # ── Idempotency guard ──────────────────────────────────────────────
#         # Vobiz can re-POST /answer for the same CallUUID (slow first
#         # response, retry policy, etc). call_control_id is UNIQUE, so we
#         # must NOT blindly insert again — look up any existing row first.
#         existing_result = await db.execute(
#             select(CallLog).where(CallLog.call_control_id == call_uuid)
#         )
#         call_log = existing_result.scalar_one_or_none()
#         is_duplicate_answer = call_log is not None

#         if is_duplicate_answer:
#             logger.warning(
#                 f"Duplicate /answer for call_uuid={call_uuid[:12] if call_uuid else '?'} "
#                 f"— reusing existing call_log (id={call_log.id}) instead of inserting a new row"
#             )
#         else:
#             call_log = CallLog(
#                 company_id=company.id, lead_id=lead.id if lead else None,
#                 direction="outbound", status="in_progress", mode=mode or "support",
#                 provider="vobiz",
#                 from_number=from_num or company.vobiz_phone_number or "",
#                 to_number=to_num,
#                 call_control_id=call_uuid, started_at=datetime.utcnow(),
#             )
#             db.add(call_log)
#             try:
#                 await db.commit()
#                 await db.refresh(call_log)
#             except IntegrityError:
#                 # Race: another concurrent /answer request for the same
#                 # CallUUID inserted first. Roll back and fetch that row
#                 # instead of crashing the request.
#                 await db.rollback()
#                 logger.warning(
#                     f"IntegrityError on call_control_id={call_uuid[:12] if call_uuid else '?'} "
#                     f"— concurrent /answer race, fetching existing row"
#                 )
#                 existing_result = await db.execute(
#                     select(CallLog).where(CallLog.call_control_id == call_uuid)
#                 )
#                 call_log = existing_result.scalar_one_or_none()
#                 is_duplicate_answer = True
#                 if call_log is None:
#                     # Shouldn't happen, but fail safe instead of 500ing
#                     logger.error(f"Could not recover CallLog after IntegrityError | call_uuid={call_uuid[:12] if call_uuid else '?'}")
#                     return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#         # ── Session guard ────────────────────────────────────────────────
#         # If a session already exists for this call (because /answer already
#         # ran once successfully), don't recreate it or re-broadcast
#         # "call started" — just reuse what's there.
#         existing_session = await session_manager.get(call_uuid)

#         if existing_session:
#             greeting = None
#             for turn in existing_session.get("history", []):
#                 if turn.get("role") == "assistant":
#                     greeting = turn.get("content")
#                     break
#             if not greeting:
#                 greeting = _build_greeting(company, lead, mode)
#         else:
#             await session_manager.create(
#                 call_control_id=call_uuid, company_id=company.id,
#                 lead_id=lead.id if lead else None,
#                 direction="outbound", mode=mode or "support", call_log_id=call_log.id,
#             )
#             greeting = _build_greeting(company, lead, mode)
#             await session_manager.add_turn(call_uuid, "assistant", greeting)

#     if not existing_session:
#         # Only fire "call started" / first greeting broadcast once per call
#         phone = lead.phone if lead else (to_num or "Unknown")
#         await live_broadcaster.call_start(company_id, call_uuid, phone, mode or "support")
#         await live_broadcaster.ai_msg(company_id, call_uuid, greeting)

#     voice_cfg  = get_vobiz_voice(company)
#     action_url = _make_action_url(company_id, lead_id, mode)
#     xml        = _xml_speak_then_listen(greeting, voice_cfg, action_url)

#     logger.info(
#         f"Vobiz answer XML | mode={'Record' if RECORD_MODE else 'Gather'} | "
#         f"action={action_url} | call_uuid={call_uuid[:12] if call_uuid else '?'} | "
#         f"duplicate={is_duplicate_answer}"
#     )
#     return Response(content=xml, media_type="text/xml")


# def _build_greeting(company: Any, lead: Any, mode: Optional[str]) -> str:
#     agent = company.agent_name or "Alex"
#     if mode == "sales":
#         first = lead.name.split()[0] if lead and lead.name else ""
#         return (
#             company.greeting_outbound_hi
#             or f"Namaste{' ' + first if first else ''} ji! Main {agent} bol raha hoon "
#                f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
#         )
#     return (
#         company.greeting_inbound_hi
#         or f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
#            f"main {agent} hoon. Main aapki kaise madad kar sakta hoon?"
#     )


# # ── Recording callback (Record mode) ─────────────────────────────────────────

# @router.post("/recording")
# async def recording_callback(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     form = await request.form()
#     logger.info(f"Vobiz /recording hit | all_fields={dict(form)}")

#     call_uuid     = form.get("CallUUID") or form.get("RequestUUID") or ""
#     recording_url = (
#         form.get("RecordUrl") or form.get("record_url") or
#         form.get("recording_url") or form.get("RecordingUrl") or
#         form.get("RecordFile") or ""
#     )

#     if not recording_url:
#         logger.warning(f"No recording URL | call_uuid={call_uuid[:12]} — asking to repeat")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?")

#     transcript = await _transcribe_url(recording_url)
#     if not transcript:
#         logger.warning(f"Empty transcript | call_uuid={call_uuid[:12]} — asking to repeat")
#         return await _error_response(call_uuid, company_id, lead_id, mode,
#                                      "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?")

#     return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# # ── Gather callback (Gather mode) ─────────────────────────────────────────────

# @router.post("/gather")
# async def gather_callback(
#     request: Request,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
#     mode:       Optional[str] = "support",
# ):
#     form = await request.form()
#     logger.info(f"Vobiz /gather hit | all_fields={dict(form)}")

#     call_uuid  = form.get("CallUUID") or form.get("RequestUUID") or ""
#     # Vobiz field names: Speech (not SpeechResult), Digits for DTMF
#     transcript = (
#         form.get("Speech") or form.get("SpeechResult") or
#         form.get("speech_result") or form.get("Digits") or ""
#     ).strip()

#     if not transcript:
#         logger.info(f"Vobiz /gather — no speech detected | call_uuid={call_uuid[:12]}")
#         session = await session_manager.get(call_uuid)
#         company = None
#         if session:
#             async with AsyncSessionLocal() as db:
#                 company = await _get_company(session["company_id"], db)
#         voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
#         action_url = _make_action_url(company_id, lead_id, mode)
#         reprompt   = "Kya aap sun pa rahe hain? Kuch kehna chahte hain toh boliye."
#         return Response(
#             content=_xml_speak_then_listen(reprompt, voice_cfg, action_url),
#             media_type="text/xml",
#         )

#     return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


# # ── Core reply builder ────────────────────────────────────────────────────────

# async def _build_reply_response(
#     call_uuid: str,
#     transcript: str,
#     company_id: Optional[str],
#     lead_id: Optional[str],
#     mode: str,
# ) -> Response:

#     logger.info(f"Transcript: '{transcript[:120]}' | call_uuid={call_uuid[:12]}")

#     if call_uuid in _hung_up:
#         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#     session = await session_manager.get(call_uuid)
#     if not session:
#         logger.warning(f"No session | call_uuid={call_uuid[:12]}")
#         return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

#     async with AsyncSessionLocal() as db:
#         company = await _get_company(session["company_id"], db)
#         if not company:
#             return Response(content="<Response><Hangup/></Response>", media_type="text/xml")
#         lead = await _get_lead(session.get("lead_id"), db) if session.get("lead_id") else None

#     voice_cfg  = get_vobiz_voice(company)
#     action_url = _make_action_url(company_id, lead_id, mode)

#     # Human transfer check
#     human_words = [
#         "speak to a human", "talk to a person", "real agent", "manager", "supervisor",
#         "insaan se baat", "kisi aur se baat", "manager se baat",
#     ]
#     if any(w in transcript.lower() for w in human_words) and company.forward_number:
#         await session_manager.add_turn(call_uuid, "user", transcript)
#         await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)
#         msg = "Bilkul, main abhi team se kisi ko connect karta hoon. Ek minute rukiye!"
#         await session_manager.add_turn(call_uuid, "assistant", msg)
#         await live_broadcaster.ai_msg(session["company_id"], call_uuid, msg)
#         async with AsyncSessionLocal() as db:
#             await _update_log(session["call_log_id"], {"transferred_to_human": True}, db)
#         xml = _xml_escape_speak(msg, voice_cfg) + "<Hangup/>"
#         return Response(content=f"<Response>{xml}</Response>", media_type="text/xml")

#     # Build context
#     await session_manager.add_turn(call_uuid, "user", transcript)
#     await session_manager.set_live_transcript(call_uuid, transcript)
#     session = await session_manager.get(call_uuid)

#     # Live — user spoke
#     await live_broadcaster.user_msg(session["company_id"], call_uuid, transcript)

#     rag_context = ""
#     try:
#         rag_context = await rag_service.search(session["company_id"], transcript, n_results=3)
#     except Exception as e:
#         logger.debug(f"RAG error: {e}")

#     prompt = _build_hindi_prompt(company, lead, rag_context, session["mode"])
#     try:
#         reply = await llm_service.generate_response(
#             messages=session["history"], system_prompt=prompt,
#             max_tokens=65, temperature=0.9,
#         )
#     except Exception as e:
#         logger.error(f"LLM error: {e}")
#         reply = "Ek dum, main check karta hoon."

#     now_iso = datetime.now().isoformat()
#     try:
#         intent = await llm_service.detect_callback_intent(
#             transcript, session["history"], now_iso
#         )
#     except Exception:
#         intent = {"wants_callback": False, "wants_to_end": False, "confidence": 0.0}

#     logger.info(f"Reply: '{str(reply)[:80]}' | intent={intent} | call_uuid={call_uuid[:12]}")

#     await session_manager.add_turn(call_uuid, "assistant", str(reply))

#     # Live — AI replied
#     await live_broadcaster.ai_msg(session["company_id"], call_uuid, str(reply))

#     # Callback scheduling
#     if intent.get("wants_callback") and intent.get("confidence", 0) >= 0.7:
#         cb_dt = _parse_callback_datetime(intent.get("callback_datetime_iso"))
#         if cb_dt and session.get("lead_id"):
#             async with AsyncSessionLocal() as db:
#                 lead_obj = await _get_lead(session["lead_id"], db)
#                 if lead_obj:
#                     lead_obj.scheduled_call_at = cb_dt
#                     lead_obj.status = "contacted"
#                     note = f"Requested callback: {intent.get('callback_time_raw', 'unspecified time')}"
#                     lead_obj.notes = f"{lead_obj.notes or ''}\n{note}".strip()
#                     await db.commit()
#         xml = _xml_escape_speak(str(reply), voice_cfg) + "<Hangup/>"
#         return Response(content=f"<Response>{xml}</Response>", media_type="text/xml")

#     # End-of-call
#     if intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9:
#         xml = _xml_escape_speak(str(reply), voice_cfg) + "<Hangup/>"
#         return Response(content=f"<Response>{xml}</Response>", media_type="text/xml")

#     # Normal reply — continue listening
#     xml = _xml_speak_then_listen(str(reply), voice_cfg, action_url)
#     return Response(content=xml, media_type="text/xml")


# async def _error_response(
#     call_uuid: str,
#     company_id: Optional[str],
#     lead_id: Optional[str],
#     mode: Optional[str],
#     message: str,
# ) -> Response:
#     session = await session_manager.get(call_uuid)
#     company = None
#     if session:
#         async with AsyncSessionLocal() as db:
#             company = await _get_company(session["company_id"], db)
#     voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
#     action_url = _make_action_url(company_id, lead_id, mode)
#     return Response(
#         content=_xml_speak_then_listen(message, voice_cfg, action_url),
#         media_type="text/xml",
#     )


# # ── Deepgram transcription ────────────────────────────────────────────────────

# async def _transcribe_url(audio_url: str) -> str:
#     try:
#         from app.core.config import settings
#         api_key = settings.DEEPGRAM_API_KEY or ""
#         if not api_key:
#             logger.error("DEEPGRAM_API_KEY not set")
#             return ""

#         # Step 1 — download audio from Vobiz in its own client
#         # media.vobiz.ai requires Basic Auth (auth_id:auth_token)
#         logger.info(f"Downloading recording: {audio_url}")
#         # VOBIZ_AUTH_ID is always MA_OBGHKHK4 — visible in every webhook log.
#         # VOBIZ_AUTH_TOKEN must be in .env. Get it from Vobiz dashboard → API Keys.
#         vobiz_auth_id    = "MA_OBGHKHK4"
#         vobiz_auth_token = getattr(settings, "VOBIZ_AUTH_TOKEN", "") or ""
#         logger.info(f"Vobiz auth | id={vobiz_auth_id} | token={'SET' if vobiz_auth_token else 'EMPTY — add VOBIZ_AUTH_TOKEN to .env'}")
#         async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
#             audio_resp = await dl.get(
#                 audio_url,
#                 headers={
#                     "X-Auth-ID":    vobiz_auth_id,
#                     "X-Auth-Token": vobiz_auth_token,
#                 },
#                 auth=(vobiz_auth_id, vobiz_auth_token) if vobiz_auth_token else None,
#             )
#             logger.info(f"Download status: {audio_resp.status_code}")
#             audio_resp.raise_for_status()
#             audio_bytes = audio_resp.content

#         if not audio_bytes:
#             logger.error("Downloaded 0 bytes from Vobiz recording URL")
#             return ""

#         logger.info(f"Downloaded {len(audio_bytes)} bytes, sending to Deepgram...")

#         # Step 2 — send raw bytes to Deepgram in a separate client
#         async with httpx.AsyncClient(timeout=30.0) as dg:
#             resp = await dg.post(
#                 "https://api.deepgram.com/v1/listen",
#                 headers={
#                     "Authorization": f"Token {api_key}",
#                     "Content-Type":  "audio/mp3",
#                 },
#                 params={
#                     "model":      "nova-2",
#                     "language":   "hi",
#                     "punctuate":  "true",
#                     "utterances": "false",
#                 },
#                 content=audio_bytes,
#             )
#             resp.raise_for_status()
#             data = resp.json()
#             text = (
#                 data.get("results", {})
#                     .get("channels", [{}])[0]
#                     .get("alternatives", [{}])[0]
#                     .get("transcript", "")
#                     .strip()
#             )
#             logger.info(f"Deepgram transcript: '{text[:120]}'")
#             return text

#     except Exception as e:
#         logger.error(f"Deepgram REST error: {e}")
#         return ""


# # ── Hangup ────────────────────────────────────────────────────────────────────

# @router.post("/hangup")
# async def hangup_webhook(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     company_id: Optional[str] = None,
#     lead_id:    Optional[str] = None,
# ):
#     form      = await request.form()
#     call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
#     _hung_up.add(call_uuid)
#     _responding.pop(call_uuid, None)
#     logger.info(f"Vobiz hangup | call_uuid={call_uuid[:12]}")
#     background_tasks.add_task(_finalize_hangup, call_uuid, company_id, lead_id)
#     return {"result": "ok"}


# async def _finalize_hangup(call_uuid: str, company_id: Optional[str], lead_id_param: Optional[str]):
#     session = await session_manager.end(call_uuid)

#     lead_id_for_lock = (session.get("lead_id") if session else None) or lead_id_param
#     if lead_id_for_lock:
#         try:
#             import redis as redis_sync
#             from app.core.config import settings as _s
#             _r = redis_sync.from_url(
#                 getattr(_s, "REDIS_URL", "redis://localhost:6379"), decode_responses=True
#             )
#             bid = _r.get(f"lead_batch:{lead_id_for_lock}")
#             if bid:
#                 _r.delete(f"batch_call_active:{bid}")
#                 _r.delete(f"lead_batch:{lead_id_for_lock}")
#         except Exception as e:
#             logger.debug(f"Batch lock clear error: {e}")

#     if not session:
#         return

#     history     = session.get("history", [])
#     call_log_id = session.get("call_log_id")
#     lead_id     = session.get("lead_id")
#     company_id  = session["company_id"]

#     transcript = "\n".join([
#         f"{'Agent' if m['role'] == 'assistant' else 'Caller'}: {m['content']}"
#         for m in history
#     ])

#     analysis = {}
#     if transcript:
#         async with AsyncSessionLocal() as db:
#             company = await _get_company(company_id, db)
#         if company:
#             try:
#                 analysis = await llm_service.analyze_call(
#                     transcript, f"{company.name} — {company.description or ''}"
#                 )
#             except Exception as e:
#                 logger.error(f"Analysis error: {e}")

#     duration = 0
#     if session.get("started_at"):
#         try:
#             started  = datetime.fromisoformat(session["started_at"])
#             duration = int((datetime.utcnow() - started).total_seconds())
#         except Exception:
#             pass

#     # Live — call ended
#     await live_broadcaster.call_end(company_id, call_uuid, duration)

#     async with AsyncSessionLocal() as db:
#         await _update_log(call_log_id, {
#             "status": "completed", "ended_at": datetime.utcnow(),
#             "duration_seconds": duration, "conversation_history": history,
#             "transcript": transcript, "summary": analysis.get("summary", ""),
#             "sentiment": analysis.get("sentiment", ""), "intent": analysis.get("intent", ""),
#             "lead_status_after": analysis.get("lead_status", ""),
#             "transferred_to_human": analysis.get("transferred_to_human", False),
#         }, db)

#         if lead_id:
#             lead = await _get_lead(lead_id, db)
#             if lead:
#                 valid = ["new","contacted","interested","warm","cold",
#                          "closed_won","closed_lost","do_not_call"]
#                 ns = analysis.get("lead_status")
#                 if ns and ns in valid:
#                     lead.status = ns
#                 iv = analysis.get("interest_level")
#                 if iv is not None:
#                     lead.interest_level = float(iv)
#                 ki = analysis.get("key_info", {})
#                 if ki:
#                     lead.key_info = {**(lead.key_info or {}), **{k: v for k, v in ki.items() if v}}
#                 lead.updated_at = datetime.utcnow()
#                 await db.commit()

#     if duration > 0:
#         try:
#             from app.services.minutes_service import deduct_minutes as _deduct
#             from firebase_admin_init import get_db as _get_firestore
#             fs  = _get_firestore()
#             uid = None
#             for doc in fs.collection("users").where("company_id","==",company_id).limit(1).stream():
#                 uid = doc.id
#                 break
#             if uid:
#                 _deduct(uid=uid, duration_seconds=duration)
#         except Exception as e:
#             logger.warning(f"Minutes deduction error: {e}")

#     import asyncio
#     await asyncio.sleep(30)
#     _hung_up.discard(call_uuid)


# # ── XML helpers ───────────────────────────────────────────────────────────────

# def _xml_escape(text: str) -> str:
#     return (text.replace("&","&amp;").replace("<","&lt;")
#                 .replace(">","&gt;").replace('"',"&quot;"))


# def _xml_escape_speak(text: str, voice_cfg: Dict) -> str:
#     return (
#         f'<Speak voice="{voice_cfg["voice"]}" '
#         f'language="{voice_cfg["language"]}">{_xml_escape(text)}</Speak>'
#     )


# def _xml_speak_then_listen(text: str, voice_cfg: Dict, action_url: str) -> str:
#     speak = _xml_escape_speak(text, voice_cfg)
#     if RECORD_MODE:
#         listen = (
#             f'<Record action="{action_url}" method="POST" '
#             f'maxLength="8" silence="2" finishOnKey="" />'
#         )
#     else:
#         # Correct Vobiz attribute names (NOT Plivo/Twilio names):
#         # - inputType="speech"       (not input="speech")
#         # - executionTimeout="15"    (not timeout — valid range 5-60, default 15)
#         # - speechEndTimeout="auto"  (not speechTimeout — valid 2-10 or auto)
#         # Vobiz POSTs to action URL on speech OR on timeout (empty fields).
#         listen = (
#             f'<Gather inputType="speech" action="{action_url}" method="POST" '
#             f'language="{voice_cfg["language"]}" executionTimeout="15" speechEndTimeout="auto">'
#             f'</Gather>'
#         )
#     return f'<Response>{speak}{listen}</Response>'


# def _make_action_url(company_id: Optional[str], lead_id: Optional[str], mode: Optional[str]) -> str:
#     endpoint = "recording" if RECORD_MODE else "gather"
#     return (
#         f"{_get_base_url()}/api/v1/vobiz/{endpoint}"
#         f"?company_id={company_id}&amp;lead_id={lead_id or ''}&amp;mode={mode or 'support'}"
#     )


# # ── Hindi prompt builder ──────────────────────────────────────────────────────

# def _build_hindi_prompt(company: Any, lead: Any, rag_context: str, mode: str) -> str:
#     agent = company.agent_name or "Aria"
#     desc  = company.description_hi or company.description or ""
#     serv  = company.services_hi or company.services or ""
#     faqs  = company.faqs_hi or company.faqs or ""

#     products_txt = ""
#     for p in (company.products or []):
#         name  = p.get("name_hi")  or p.get("name", "")
#         pdesc = p.get("description_hi") or p.get("description", "")
#         price = p.get("price", "")
#         feats = p.get("features_hi") or p.get("features") or []
#         products_txt += f"\n- {name} ({price}): {pdesc}"
#         if feats:
#             products_txt += f" | Features: {', '.join(feats)}"

#     base = (
#         f"Aap {agent} hain, {company.name} ke liye ek AI phone agent. "
#         f"HAMESHA natural Hindi-English mix (Hinglish) mein baat karein.\n\n"
#         f"Company: {company.name}\nVivaran: {desc}\nSevayein: {serv}\n"
#     )
#     if products_txt:
#         base += f"\nProducts:{products_txt}\n"
#     if faqs:
#         base += f"\nFAQs:\n{faqs}\n"
#     if rag_context:
#         base += f"\nAdditional context:\n{rag_context}\n"

#     if mode == "sales":
#         ln = getattr(lead, "name", None) or ""
#         base += (
#             f"\nOutbound sales call. Lead: {ln or 'pata nahi'}. "
#             f"Product pitch karein, interest judge karein. "
#             f"Jawab CHHOTE rakhein — jaise real phone call."
#         )
#     else:
#         base += f"\nInbound support call. Sawaal ka seedha jawab dein. CHHOTA rakhein."
#     return base


# # ── DB helpers ────────────────────────────────────────────────────────────────

# async def _get_company(company_id: str, db) -> Optional[Company]:
#     r = await db.execute(select(Company).where(Company.id == company_id))
#     return r.scalar_one_or_none()

# async def _get_lead(lead_id: Optional[str], db) -> Optional[Lead]:
#     if not lead_id:
#         return None
#     r = await db.execute(select(Lead).where(Lead.id == lead_id))
#     return r.scalar_one_or_none()

# async def _update_log(call_log_id: Optional[str], updates: dict, db):
#     if not call_log_id:
#         return
#     r = await db.execute(select(CallLog).where(CallLog.id == call_log_id))
#     log = r.scalar_one_or_none()
#     if log:
#         for k, v in updates.items():
#             setattr(log, k, v)
#         log.updated_at = datetime.utcnow()
#         await db.commit()

# def _parse_callback_datetime(iso_str: Optional[str]):
#     if not iso_str:
#         return None
#     try:
#         from datetime import time as dtime
#         import pytz
#         tz = pytz.timezone("Asia/Kolkata")
#         dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
#         if dt.tzinfo:
#             dt = dt.astimezone(tz).replace(tzinfo=None)
#         t = dt.time()
#         if t < dtime(9, 0):
#             dt = dt.replace(hour=9, minute=0, second=0)
#         elif t > dtime(18, 0):
#             from datetime import timedelta
#             dt = (dt + timedelta(days=1)).replace(hour=9, minute=0, second=0)
#         return dt
#     except Exception:
#         return None
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

SARVAM FIXES (running history):
  1. Sarvam's TTS REST API expects the text under an `inputs` LIST field,
     not a `text` string field. Sending `"text": "..."` got a silent 400
     Bad Request with no useful detail unless you log the response body —
     which is what was happening (calls fell back to Vobiz's native Speak
     on every single turn). Fixed to send `"inputs": [text]`.
  2. _synthesize_sarvam now logs `e.response.text` on HTTPStatusError so
     any future Sarvam-side rejection (bad speaker name, model mismatch,
     etc.) is visible in the logs instead of just "400 Bad Request".
  3. MODEL UPGRADE: switched bulbul:v2 → bulbul:v3. v3 is Sarvam's newer
     model — noticeably more natural on code-mixed Hinglish specifically
     (handles number normalization, mixed-language prosody, etc. with
     less preprocessing needed), which is what was behind "Sarvam sounds
     worse than Vobiz". v2's voices sound flatter/more robotic on natural
     Hinglish by comparison. v3 has a COMPLETELY DIFFERENT speaker
     catalog from v2 — v2 names (anushka, hitesh, abhilash, etc.) do not
     exist on v3 and will 400 if sent with model="bulbul:v3". v3 also
     drops pitch/loudness controls (v2-only) in favor of a `temperature`
     param for expressiveness — not currently set here, defaults to 0.6.

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

KNOWN OPEN ISSUE (not fixed here — flagging so it doesn't surprise you):
  _transcribe_url() still has the Vobiz auth ID hardcoded as a literal
  ("MA_52FARPL9" as of this revision) instead of read from company/
  settings. It works today because it matches the current sub-account,
  but will silently 401 again (same failure mode as before) the moment
  a call routes through a different Vobiz sub-account. Worth wiring to
  company.vobiz_auth_id when you get a chance.
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
from app.services import plan_service

logger = logging.getLogger(__name__)
router = APIRouter()

RECORD_MODE: bool = True
DIAGNOSTIC_MODE: bool = False

# How long Vobiz waits for silence after the caller stops talking before it
# finalizes the recording and POSTs to /recording. This sits directly on
# the critical path of every turn, so it's worth tuning down from a generic
# default — but too low risks cutting callers off mid-sentence (especially
# with Hinglish, where speakers pause between code-switches). 0.6s was the
# original default; 0.45-0.5s is usually still safe. TEST ON A HANDFUL OF
# REAL CALLS before shipping a lower value — this is exactly the kind of
# thing that looks fine on a quiet test line and clips people on a noisy
# mobile connection.
RECORD_SILENCE_SECONDS: float = 0.5
RECORD_MAX_LENGTH_SECONDS: int = 8

# How many consecutive turns in a row can come back with no speech
# detected (empty recording / empty transcript) before we give up and
# hang up automatically. Previously there was no cap at all — if someone
# picked up and just left the line open (or the call connected to
# voicemail/silence), the flow would re-prompt "kya aap sun pa rahe
# hain?" forever, sitting connected and burning minutes with no way to
# end the call on its own.
MAX_SILENT_TURNS: int = 2
# call_uuid -> how many consecutive silent/no-speech turns in a row.
# Reset to 0 the moment a real transcript comes back.
_SILENT_TURNS: Dict[str, int] = {}

import re as _re

def _trim_to_sentence(text: str) -> str:
    """
    If an LLM reply got cut off by hitting max_tokens mid-word/mid-clause
    (e.g. "...jo a"), trim back to the last complete sentence or clause
    instead of sending the fragment straight to TTS. Falls back to the
    last complete word if there's no sentence-ending punctuation at all,
    and to the original text untouched if it's already short/complete.
    """
    if not text:
        return text
    text = text.strip()
    # Prefer the last full sentence (., !, ?, or Hindi purna viram ।).
    matches = list(_re.finditer(r"[.!?\u0964]", text))
    if matches:
        return text[: matches[-1].end()].strip()
    # No sentence-ending punctuation — at least don't cut mid-word: back
    # up to the last complete word/comma boundary.
    trimmed = text.rsplit(" ", 1)[0].strip() if " " in text else text
    return (trimmed or text).rstrip(",;:") + ("." if trimmed else "")

# ── Filler + redirect (perceived-latency trick) ─────────────────────────────
# Record mode has a hard floor: Vobiz has to finalize the recording, POST
# it to us, and then we still have to download + transcribe + RAG + LLM +
# TTS before we can say anything back. Instead of the caller sitting in
# dead air the whole time, play a short pre-cached filler clip immediately
# and use Vobiz's <Redirect> verb to hop to a second endpoint once the
# filler finishes — by which point the real reply is usually ready (or
# close to it), because we kick off the actual processing in the
# background the instant /recording is hit, not after the filler plays.
# This overlaps real processing time with filler playback instead of just
# masking it, so it also shaves real wall-clock, not just perceived time.
#
# CAVEAT: this file already has a documented case of a Vobiz verb
# (<Gather>) not behaving reliably on this account. <Redirect> is a
# standard verb on Plivo-style telephony XML, but VERIFY IT AGAINST A REAL
# CALL before trusting this in production — check the logs for "/continue
# hit" actually firing. If it doesn't work on your account, flip this to
# False and everything falls back to the original fully-synchronous
# behavior with zero other changes needed.
ENABLE_FILLER_REDIRECT: bool = True
# A pool instead of one fixed phrase — playing the exact same "Hmm, ek
# second..." on every single turn of a multi-minute call sounds robotic
# fast. First-bounce fillers are the ones played most often (almost every
# turn, per production logs), so that pool is bigger; second-bounce is
# rarer so a smaller pool is fine. _pick_filler() also avoids repeating
# whatever was just used on THIS call.
FILLER_POOL_1 = [
    "Hmm, ek second...",
    "Achha, ek min...",
    "Theek hai, dekhta hoon...",
    "Ji, ek second...",
]
FILLER_POOL_2 = [
    "Bas ek second aur...",
    "Bas thoda sa aur...",
    "Ho gaya, bas...",
]
FILLER_BOUNCE_TIMEOUT = 2.5   # seconds to wait before playing another filler
MAX_FILLER_BOUNCES = 2        # after this many, wait out the remaining budget silently

# call_uuid -> asyncio.Task running _process_recording_turn() in the
# background while the filler clip plays. Consumed (popped) by /continue.
_PENDING_TURNS: Dict[str, "asyncio.Task"] = {}
# call_uuid -> how many filler bounces have already played for this turn.
_CONTINUE_BOUNCES: Dict[str, int] = {}
# call_uuid -> last filler text played, so back-to-back turns don't reuse it.
_LAST_FILLER: Dict[str, str] = {}


def _pick_filler(call_uuid: str, pool: list) -> str:
    import random
    last = _LAST_FILLER.get(call_uuid)
    choices = [p for p in pool if p != last] or pool
    choice = random.choice(choices)
    _LAST_FILLER[call_uuid] = choice
    return choice

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
# MODEL: bulbul:v3 (see module docstring for why this replaced bulbul:v2).
# v3 speaker names are NOT shared with v2 — do not mix them.
#
# "sophia" (the old default here) does not exist in bulbul:v3 — Sarvam's
# own 400 response confirms the actual valid speaker list, which does
# NOT include "sophia": anushka, abhilash, manisha, vidya, arya, karun,
# hitesh, aditya, ritu, priya, neha, rahul, pooja, rohan, simran, kavya,
# amit, dev, ishita, shreya, ratan, varun, manan, sumit, roopa, kabir,
# aayan, shubh, ashutosh, advait, anand, tanya, tarun, sunny, mani,
# gokul, vijay, shruti, suhani, mohit, kavitha, rehan, soham, rupali.
# Every static-phrase TTS warmup call was silently failing with a 400
# because of this — "priya" is confirmed valid (it's what the live call
# pipeline itself already uses successfully) and used as the new default.
SARVAM_DEFAULT_VOICE = {"female": "priya", "male": "shubh"}
# other female v3 speakers worth trying: "kavya", "manisha", "neha"
# other male v3 speakers worth trying: "aditya", "advait", "ashutosh"

async def _synthesize_sarvam(text: str, company: Company) -> Optional[bytes]:
    """Returns raw WAV bytes at 8kHz (matches Vobiz telephony audio — no
    resampling needed), or None on failure (caller should fall back to
    Vobiz's native <Speak> for that turn rather than fail the whole call)."""
    api_key = getattr(settings, "SARVAM_API_KEY", None)
    if not api_key:
        logger.error("SARVAM_API_KEY not set — cannot use sarvam TTS provider")
        return None

    gender  = (getattr(company, "voice_gender", None) or "female").lower()
    speaker = getattr(company, "tts_voice", None) or SARVAM_DEFAULT_VOICE.get(gender, "priya")

    try:
        resp = await _client().post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
            json={
                # Sarvam's TTS endpoint expects the text under an `inputs`
                # LIST field, not a `text` string field — sending "text"
                # silently 400s with no useful detail unless you log the
                # response body.
                "inputs": [text],
                "target_language_code": "hi-IN",
                "speaker": speaker,
                "model": "bulbul:v3",
                "pace": 1.0,                    # 0.5–2.0 on v3; lower = slower/calmer
                "enable_preprocessing": True,   # better Hinglish/number normalization
                "speech_sample_rate": 8000,     # matches telephony — no resample step
                # NOTE: pitch/loudness are v2-only params, intentionally
                # omitted — v3 ignores them if sent.
            },
        )
        resp.raise_for_status()
        data = resp.json()
        audios = data.get("audios") or []
        if not audios:
            logger.error(f"Sarvam TTS returned no audio | resp={data}")
            return None
        return base64.b64decode(audios[0])
    except httpx.HTTPStatusError as e:
        # Log the actual response body — this is what tells you WHY
        # Sarvam rejected the request (bad speaker name, model mismatch,
        # auth issue, malformed payload, etc.) instead of just "400 Bad
        # Request" with zero context.
        body = None
        try:
            body = e.response.text
        except Exception:
            pass
        logger.error(f"Sarvam TTS error: {e} | response_body={body}")
        return None
    except Exception as e:
        logger.error(f"Sarvam TTS error: {e}")
        return None


# ── XML prompt builder (Speak OR Play, depending on tts_provider) ───────────

# Fixed phrases (reprompts, error messages, human-transfer message, and
# each company's configured greetings) get synthesized through Sarvam over
# and over — same text, same voice, every time — even though the audio
# never changes. Cache the resulting WAV bytes keyed by (company, voice,
# text) so repeats are served instantly with zero TTS API round trip.
# Small in-memory cache (short clips, capped) — not persisted across
# restarts, but warmup_static_tts() below re-primes it on boot.
_STATIC_TTS_CACHE: Dict[str, bytes] = {}
_STATIC_TTS_CACHE_MAX = 500

FIXED_PHRASES_HI = [
    "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?",
    "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?",
    "Kya aap sun pa rahe hain? Kuch kehna chahte hain toh boliye.",
    "Bilkul, main abhi team se kisi ko connect karta hoon. Ek minute rukiye!",
] + FILLER_POOL_1 + FILLER_POOL_2


def _static_tts_key(company: Any, text: str) -> str:
    gender  = (getattr(company, "voice_gender", None) or "female").lower()
    speaker = getattr(company, "tts_voice", None) or f"default-{gender}"
    cid     = getattr(company, "id", "") or "anon"
    return f"{cid}|{speaker}|{text}"


async def warmup_static_tts(companies: list) -> int:
    """Pre-synthesize the fixed reprompt/error/transfer phrases, plus each
    company's own greeting text, for every Sarvam-using company. Call once
    at app startup so the first live call of the day doesn't pay a live
    Sarvam round trip on these — they're already sitting in the cache.
    Safe to call repeatedly; already-cached entries are skipped."""
    n = 0
    for company in companies:
        if (getattr(company, "tts_provider", None) or "").lower() != "sarvam":
            continue
        phrases = list(FIXED_PHRASES_HI)
        if getattr(company, "greeting_inbound_hi", None):
            phrases.append(company.greeting_inbound_hi)
        if getattr(company, "greeting_outbound_hi", None):
            phrases.append(company.greeting_outbound_hi)
        for text in phrases:
            key = _static_tts_key(company, text)
            if key in _STATIC_TTS_CACHE:
                continue
            try:
                audio = await _synthesize_sarvam(text, company)
            except Exception as e:
                logger.warning(f"TTS warmup error (non-fatal) | company={getattr(company, 'id', '?')}: {e}")
                continue
            if audio:
                _STATIC_TTS_CACHE[key] = audio
                n += 1
    logger.info(f"TTS warmup complete — {n} phrases pre-synthesized and cached")
    return n


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
        cache_key   = _static_tts_key(company, text) if company else None
        audio_bytes = _STATIC_TTS_CACHE.get(cache_key) if cache_key else None
        if audio_bytes:
            logger.debug(f"Static TTS cache hit | company={getattr(company, 'id', '?')}")
        else:
            audio_bytes = await _synthesize_sarvam(text, company)
            if audio_bytes and cache_key:
                if len(_STATIC_TTS_CACHE) >= _STATIC_TTS_CACHE_MAX:
                    _STATIC_TTS_CACHE.pop(next(iter(_STATIC_TTS_CACHE)), None)
                _STATIC_TTS_CACHE[cache_key] = audio_bytes
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

        # IDEMPOTENCY: Vobiz (like most telephony providers) can and does
        # retry a webhook if the first response is slow or a connection
        # blips — same CallUUID, sent again. The old code assumed /answer
        # only ever fires once per call and unconditionally INSERTed a
        # CallLog row, which crashes with a UNIQUE constraint violation on
        # the retry (call_control_id already exists) → 500 → Vobiz gets no
        # valid XML on that attempt → "Error Reaching Answer URL" (7011)
        # and hangs up, even though the FIRST /answer attempt had already
        # succeeded fine. Check for an existing row first instead of
        # assuming this is always the first delivery.
        existing = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
        call_log = existing.scalar_one_or_none()
        if call_log:
            logger.warning(
                f"Vobiz /answer — duplicate webhook delivery for call_uuid={call_uuid[:12]} "
                f"(retry), reusing existing CallLog instead of inserting again"
            )
        else:
            call_log = CallLog(
                company_id=company.id, lead_id=lead.id if lead else None,
                direction="outbound", status="in_progress", mode=mode or "support",
                provider="vobiz",
                from_number=from_num or company.vobiz_phone_number or "",
                to_number=to_num,
                call_control_id=call_uuid, started_at=datetime.utcnow(),
            )
            db.add(call_log)
            try:
                await db.commit()
                await db.refresh(call_log)
            except Exception:
                # Backstop for a true race (two near-simultaneous /answer
                # deliveries both passed the existence check above before
                # either had committed) — roll back and reuse whichever
                # row actually made it in, instead of 500ing.
                await db.rollback()
                existing2 = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
                call_log = existing2.scalar_one_or_none()
                if not call_log:
                    raise  # genuinely not a duplicate-insert issue — surface the real error

        await session_manager.create(
            call_control_id=call_uuid, company_id=company.id,
            lead_id=lead.id if lead else None,
            direction="outbound", mode=mode or "support", call_log_id=call_log.id,
            company_snapshot=_company_snapshot(company),
        )

        agent = company.agent_name or "Alex"
        is_male = (getattr(company, "voice_gender", None) or "female").lower() == "male"
        if mode == "sales":
            first = lead.name.split()[0] if lead and lead.name else ""
            greeting = (
                company.greeting_outbound_hi
                or (
                    f"Namaste{' ' + first if first else ''} ji! Main {agent} "
                    f"{'bol raha hoon' if is_male else 'bol rahi hoon'} "
                    f"{company.name} ki taraf se. Aapka thoda sa time milega kya?"
                )
            )
        else:
            greeting = (
                company.greeting_inbound_hi
                or (
                    f"Namaste! {company.name} mein call karne ke liye dhanyawad, "
                    f"main {agent} hoon. Main aapki kaise madad "
                    f"{'kar sakta hoon' if is_male else 'kar sakti hoon'}?"
                )
            )

    await session_manager.add_turn(call_uuid, "assistant", greeting)

    phone = lead.phone if lead else (to_num or "Unknown")
    await live_broadcaster.call_start(company_id, call_uuid, phone, mode or "support")
    # FIX: nothing was ever emitting call_answered, so the Live tab stayed
    # stuck on the "ringing / waiting for pickup" screen for the entire
    # call and never showed the chat panel — even though messages were
    # arriving fine underneath it. Vobiz only hits /answer once the callee
    # has actually picked up (there's no separate pre-answer webhook in
    # this flow), so it's correct to flip straight to answered here.
    await live_broadcaster.call_answered(company_id, call_uuid)
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
        silent_count = _SILENT_TURNS.get(call_uuid, 0) + 1
        _SILENT_TURNS[call_uuid] = silent_count
        if silent_count >= MAX_SILENT_TURNS:
            return await _silence_hangup_response(call_uuid, company_id, lead_id, mode)
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Maafi chahta hoon, mujhe sunai nahi diya. Kya aap dobara bol sakte hain?")

    if ENABLE_FILLER_REDIRECT and RECORD_MODE:
        # Kick off the real work (download + transcribe + RAG + LLM + TTS)
        # in the background RIGHT NOW, then immediately answer with a short
        # cached filler clip + <Redirect>. By the time Vobiz finishes
        # playing the filler and hits /continue, the background task is
        # usually done (or nearly so) — its time overlaps with filler
        # playback instead of stacking after it.
        task = asyncio.create_task(
            _process_recording_turn(call_uuid, recording_url, company_id, lead_id, mode)
        )
        _PENDING_TURNS[call_uuid] = task

        session = await session_manager.get(call_uuid)
        company = await _company_for_session(session) if session else None
        voice_cfg = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}

        filler_xml   = await _xml_prompt(_pick_filler(call_uuid, FILLER_POOL_1), voice_cfg, company)
        continue_url = _make_continue_url(company_id, lead_id, mode)
        xml = f'<Response>{filler_xml}<Redirect method="POST">{continue_url}</Redirect></Response>'
        logger.info(f"Vobiz /recording — dispatched background turn, replying with filler | call_uuid={call_uuid[:12]}")
        return Response(content=xml, media_type="text/xml")

    # Filler/redirect disabled (or not Record mode) — original fully
    # synchronous behavior, unchanged.
    return await _process_recording_turn(call_uuid, recording_url, company_id, lead_id, mode)


async def _process_recording_turn(
    call_uuid: str, recording_url: str,
    company_id: Optional[str], lead_id: Optional[str], mode: Optional[str],
) -> Response:
    """The actual per-turn work: transcribe the recording, then build the
    reply. Runs either inline (filler/redirect disabled) or as a background
    asyncio.Task consumed by /continue (filler/redirect enabled)."""
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        company = await _get_company(company_id, db) if company_id else None
    vobiz_auth_id = getattr(company, "vobiz_auth_id", "") or "" if company else ""
    vobiz_auth_token = getattr(company, "vobiz_auth_token", "") or "" if company else ""

    transcript = await _transcribe_url(recording_url, vobiz_auth_id, vobiz_auth_token)
    if not transcript:
        logger.warning(f"Empty transcript | call_uuid={call_uuid[:12]} — asking to repeat")
        silent_count = _SILENT_TURNS.get(call_uuid, 0) + 1
        _SILENT_TURNS[call_uuid] = silent_count
        if silent_count >= MAX_SILENT_TURNS:
            return await _silence_hangup_response(call_uuid, company_id, lead_id, mode)
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Kuch sunai nahi diya. Kya aap thoda louder bol sakte hain?")

    return await _build_reply_response(call_uuid, transcript, company_id, lead_id, mode)


@router.post("/continue")
async def continue_callback(
    request: Request,
    company_id: Optional[str] = None,
    lead_id:    Optional[str] = None,
    mode:       Optional[str] = "support",
):
    """Hit by Vobiz's <Redirect> once a filler clip finishes playing.
    Picks up the background task dispatched from /recording. If it's not
    done yet, plays ANOTHER short filler and redirects back here instead
    of leaving the caller in dead air for the rest of the wait — up to
    MAX_FILLER_BOUNCES times, after which it waits out the remaining
    budget in one go."""
    form = await request.form()
    call_uuid = form.get("CallUUID") or form.get("RequestUUID") or ""
    logger.info(f"Vobiz /continue hit | call_uuid={call_uuid[:12] if call_uuid else '?'}")

    task = _PENDING_TURNS.get(call_uuid)
    if task is None:
        _CONTINUE_BOUNCES.pop(call_uuid, None)
        logger.warning(f"Vobiz /continue — no pending turn for call_uuid={call_uuid[:12]}")
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Ek second, phir se boliye.")

    bounces = _CONTINUE_BOUNCES.get(call_uuid, 0)
    # shield() keeps the timeout below from cancelling the background task
    # itself — it just gives up on THIS wait and lets a later /continue
    # hit (or the final hard-budget wait) pick the same task back up.
    remaining_budget = 10.0 if bounces >= MAX_FILLER_BOUNCES else FILLER_BOUNCE_TIMEOUT
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining_budget)
        _PENDING_TURNS.pop(call_uuid, None)
        _CONTINUE_BOUNCES.pop(call_uuid, None)
        return result
    except asyncio.TimeoutError:
        if bounces < MAX_FILLER_BOUNCES:
            _CONTINUE_BOUNCES[call_uuid] = bounces + 1
            session = await session_manager.get(call_uuid)
            company = await _company_for_session(session) if session else None
            voice_cfg = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
            filler_xml   = await _xml_prompt(_pick_filler(call_uuid, FILLER_POOL_2), voice_cfg, company)
            continue_url = _make_continue_url(company_id, lead_id, mode)
            xml = f'<Response>{filler_xml}<Redirect method="POST">{continue_url}</Redirect></Response>'
            logger.info(f"Vobiz /continue — still processing, bounce filler #{bounces + 1} | call_uuid={call_uuid[:12]}")
            return Response(content=xml, media_type="text/xml")

        _PENDING_TURNS.pop(call_uuid, None)
        _CONTINUE_BOUNCES.pop(call_uuid, None)
        logger.error(f"Vobiz /continue — background turn exceeded total budget | call_uuid={call_uuid[:12]}")
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Maafi chahta hoon, thoda time lag raha hai. Kya aap dobara bol sakte hain?")
    except Exception as e:
        _PENDING_TURNS.pop(call_uuid, None)
        _CONTINUE_BOUNCES.pop(call_uuid, None)
        logger.error(f"Vobiz /continue — background turn failed: {e} | call_uuid={call_uuid[:12]}")
        return await _error_response(call_uuid, company_id, lead_id, mode,
                                     "Kuch problem hua. Kya aap dobara bol sakte hain?")


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
        company = await _company_for_session(session) if session else None
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
    # Real speech came back — clear the consecutive-silence counter so a
    # single quiet/misheard turn earlier in the call doesn't count toward
    # the auto-hangup threshold later on.
    _SILENT_TURNS.pop(call_uuid, None)

    if call_uuid in _hung_up:
        return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

    session = await session_manager.get(call_uuid)
    if not session:
        logger.warning(f"No session | call_uuid={call_uuid[:12]}")
        return Response(content="<Response><Hangup/></Response>", media_type="text/xml")

    company = await _company_for_session(session)
    if not company:
        return Response(content="<Response><Hangup/></Response>", media_type="text/xml")
    async with AsyncSessionLocal() as db:
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
            # LATENCY: production logs show Sarvam TTS taking 5-8s on
            # replies generated at max_tokens=65 (~200-260 chars), and
            # ~3s at max_tokens=40 (~100-150 chars) — TTS synthesis time
            # scales with text length, so this was directly adding to
            # every turn's wall-clock time on top of network latency.
            #
            # max_tokens=24 (the value this used to be) is too aggressive
            # for Hindi/Hinglish specifically — Hindi text tokenizes less
            # efficiently per word than English, so 24 tokens often lands
            # mid-word instead of even completing one full sentence,
            # which is exactly the cut-off-mid-sentence behavior this was
            # causing. 48 is a middle ground; _trim_to_sentence() below is
            # the actual safety net regardless of the exact number chosen
            # here — it guarantees TTS never receives a mid-word fragment
            # even if a reply does hit the cap.
            #
            # NOTE: this whole max_tokens-chasing tradeoff is a symptom of
            # the old per-turn webhook flow (generate full text -> THEN
            # synthesize the whole thing -> THEN send). The streaming
            # pipeline (vobiz_stream_pipeline.py, gated by
            # settings.USE_STREAMING_CALLS) streams LLM tokens into TTS as
            # they're generated instead, so it doesn't need a tight cap
            # here at all. Once calls are running through that path this
            # function stops being the bottleneck — treat this cap as a
            # fallback-path safety valve, not the real latency fix.
            raw = await llm_service.generate_response(
                messages=session["history"], system_prompt=prompt,
                max_tokens=80, temperature=0.9,
            )
            return _trim_to_sentence(raw)
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

    reply_task  = asyncio.create_task(_get_reply())
    intent_task = asyncio.create_task(_get_intent())
    reply = await reply_task

    # LATENCY: the reply audio is identical no matter which branch below
    # ends up firing (callback-note-and-continue, hangup, or plain
    # continue) — it only depends on `reply`, never on `intent`. Kick off
    # Sarvam synthesis the moment the reply text is ready instead of
    # waiting on asyncio.gather() for the intent classifier too. Intent
    # detection is a second, independent Groq call — this overlaps its
    # ~150-300ms with TTS network time instead of paying for both back
    # to back.
    tts_task = asyncio.create_task(_xml_prompt(str(reply), voice_cfg, company))
    intent = await intent_task

    logger.info(f"Reply: '{str(reply)[:80]}' | intent={intent} | call_uuid={call_uuid[:12]}")

    await session_manager.add_turn(call_uuid, "assistant", str(reply))
    await live_broadcaster.ai_msg(session["company_id"], call_uuid, str(reply))

    # Callback scheduling — note it, but do NOT hang up on this alone.
    #
    # BUG FIX: this used to hang up immediately whenever wants_callback
    # fired with confidence >= 0.7. But the classifier's own prompt treats
    # casual words like "later" / "after that" as callback signals, and a
    # caller can just as easily use those mid-sentence to mean "later in
    # THIS conversation" rather than "call me back another day". Caught in
    # production: caller said "...उसके बाद बताता हूं मैं" (roughly "I'll
    # tell you after that [once you've explained more]") and the call was
    # hung up mid-conversation on a false positive.
    #
    # A genuine callback request should only end the call when the caller
    # has ALSO actually signaled they're done (wants_to_end, high
    # confidence) — that's a much stronger, harder-to-misfire-on signal.
    # Otherwise: save the callback note and keep the conversation going.
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
        if not (intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9):
            prompt_xml = await tts_task
            xml = _xml_wrap_with_listen(prompt_xml, action_url)
            return Response(content=xml, media_type="text/xml")

    # End-of-call — the caller has actually indicated they're done (bye,
    # not interested, stop calling, etc.), independent of any callback
    # note above.
    if intent.get("wants_to_end") and intent.get("confidence", 0) >= 0.9:
        prompt_xml = await tts_task
        return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")

    # Normal reply — continue listening
    prompt_xml = await tts_task
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
    company = await _company_for_session(session) if session else None
    voice_cfg  = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
    action_url = _make_action_url(company_id, lead_id, mode)
    prompt_xml = await _xml_prompt(message, voice_cfg, company)
    return Response(content=_xml_wrap_with_listen(prompt_xml, action_url), media_type="text/xml")


async def _silence_hangup_response(
    call_uuid: str,
    company_id: Optional[str],
    lead_id: Optional[str],
    mode: Optional[str],
) -> Response:
    """Plays a short farewell and hangs up — used once MAX_SILENT_TURNS
    consecutive no-speech turns have been hit, so a caller who picked up
    and never said anything (or went silent) doesn't sit connected
    indefinitely. See MAX_SILENT_TURNS/_SILENT_TURNS above."""
    logger.info(f"Vobiz — silence timeout, auto-disconnecting | call_uuid={call_uuid[:12]}")
    _SILENT_TURNS.pop(call_uuid, None)
    _hung_up.add(call_uuid)
    session = await session_manager.get(call_uuid)
    company = await _company_for_session(session) if session else None
    voice_cfg = get_vobiz_voice(company) if company else {"voice": "WOMAN", "language": "hi-IN"}
    farewell = "Lagta hai koi jawab nahi mil raha, isliye main call disconnect kar raha hoon. Dhanyavaad!"
    prompt_xml = await _xml_prompt(farewell, voice_cfg, company)
    return Response(content=f"<Response>{prompt_xml}<Hangup/></Response>", media_type="text/xml")


# ── Deepgram transcription ────────────────────────────────────────────────────

async def _transcribe_url(audio_url: str, vobiz_auth_id: str, vobiz_auth_token: str) -> str:
    try:
        api_key = settings.DEEPGRAM_API_KEY or ""
        if not api_key:
            logger.error("DEEPGRAM_API_KEY not set")
            return ""

        logger.info(f"Downloading recording: {audio_url}")
        # Was hardcoded as a literal (previously "MA_OBGHKHK4", then
        # "MA_52FARPL9"), then later read from the single global
        # settings.VOBIZ_AUTH_ID/TOKEN in .env — both wrong for a
        # multi-tenant deployment: a company using its own separate
        # Vobiz account would 401 downloading its own recordings,
        # since this was always authenticating as whichever single
        # account was hardcoded/in .env, never as the company that
        # actually owns the call. Now takes the company's own
        # credentials as required arguments — see _process_recording_turn
        # above, which is the only caller and loads them from the
        # Company row before calling this.
        if not vobiz_auth_id or not vobiz_auth_token:
            logger.error("No Vobiz credentials for this company — set vobiz_auth_id/vobiz_auth_token in Settings; recording download will 401")

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
    hangup_cause      = form.get("HangupCause", "")
    hangup_cause_name = form.get("HangupCauseName", "")
    call_status_field = form.get("CallStatus", "")
    _hung_up.add(call_uuid)
    _responding.pop(call_uuid, None)
    # Clean up the per-call tracking dicts used by the filler/redirect
    # flow — the background task (if any) either already finished or its
    # result is now moot since the caller hung up.
    _PENDING_TURNS.pop(call_uuid, None)
    _CONTINUE_BOUNCES.pop(call_uuid, None)
    _LAST_FILLER.pop(call_uuid, None)
    _SILENT_TURNS.pop(call_uuid, None)
    logger.info(f"Vobiz hangup | call_uuid={call_uuid[:12]} | all_fields={dict(form)}")
    background_tasks.add_task(
        _finalize_hangup, call_uuid, company_id, lead_id,
        hangup_cause, hangup_cause_name, call_status_field,
    )
    return {"result": "ok"}


async def _finalize_hangup(
    call_uuid: str, company_id: Optional[str], lead_id_param: Optional[str],
    hangup_cause: str = "", hangup_cause_name: str = "", call_status_field: str = "",
):
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
        # A duplicate Vobiz webhook, or a race with another cleanup path, can
        # arrive after the Redis session has already been consumed. Never turn
        # an already-completed/answered call into "no_answer" just because the
        # Redis session is gone. Recover the authoritative CallLog first.
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
            recovered_log = r.scalar_one_or_none()

            if recovered_log and recovered_log.status == "completed":
                logger.info(
                    f"Duplicate/late hangup ignored; call already finalized | "
                    f"call_uuid={call_uuid[:12]}"
                )
                return

            if recovered_log and recovered_log.status == "in_progress":
                # The call was answered and the CallLog proves it. Reconstruct
                # the small session shape needed by the normal finalizer. This
                # prevents the old race where session_manager.end() won first,
                # then /hangup saw no session and incorrectly wrote no_answer.
                recovered_history = recovered_log.conversation_history or []
                recovered_started = recovered_log.started_at or recovered_log.created_at or datetime.utcnow()
                session = {
                    "call_control_id": call_uuid,
                    "company_id": str(recovered_log.company_id),
                    "lead_id": str(recovered_log.lead_id) if recovered_log.lead_id else lead_id_param,
                    "call_log_id": str(recovered_log.id),
                    "history": recovered_history,
                    "started_at": recovered_started.isoformat(),
                    "live_call_uuid": call_uuid,
                    "product_focus": None,
                }
                logger.warning(
                    f"Recovered answered call without Redis session | "
                    f"call_uuid={call_uuid[:12]} | call_log_id={recovered_log.id}"
                )
            else:
                # No connected session and no answered CallLog: this is the
                # genuine busy/no-answer/rejected path. Preserve the existing
                # behavior for those calls.
                cause_upper = f"{hangup_cause} {hangup_cause_name}".upper()
                if "BUSY" in cause_upper:
                    outcome = "busy"
                elif any(k in cause_upper for k in ("NO_ANSWER", "NO ANSWER", "TIMEOUT", "RINGING")):
                    outcome = "no_answer"
                elif any(k in cause_upper for k in ("REJECT", "DECLINE", "CANCEL")):
                    outcome = "rejected"
                elif call_status_field and call_status_field.lower() not in ("completed",):
                    outcome = "failed"
                else:
                    outcome = "no_answer"

                if recovered_log and recovered_log.status == "ringing":
                    recovered_log.status = outcome
                    recovered_log.ended_at = datetime.utcnow()
                    await db.flush()
                    if recovered_log.lead_id:
                        lead = await _get_lead(str(recovered_log.lead_id), db)
                        if lead and lead.status in ("new", "contacted"):
                            lead.status = "called"
                            lead.updated_at = datetime.utcnow()
                    await db.commit()
                try:
                    await live_broadcaster.call_no_answer(
                        company_id or (recovered_log.company_id if recovered_log else None),
                        call_uuid, outcome,
                    )
                except Exception:
                    pass
                logger.info(
                    f"Call never connected | call_uuid={call_uuid[:12]} | "
                    f"outcome={outcome} | cause={hangup_cause_name or hangup_cause}"
                )
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
                lead_ctx = ""
                async with AsyncSessionLocal() as _db2:
                    if lead_id:
                        _lead2 = await _get_lead(lead_id, _db2)
                        if _lead2:
                            lead_ctx = f"status={_lead2.status}; key_info={_lead2.key_info or {}}; notes={(_lead2.notes or '')[-700:]}"
                product_ctx = ""
                focus = session.get("product_focus") or company.active_product
                if focus:
                    from app.services.llm.prompts import find_product, _product_text
                    product = find_product(company, focus)
                    product_ctx = _product_text(product) if product else "UNVERIFIED PRODUCT FOCUS: do not infer product facts."
                analysis = await llm_service.analyze_call(
                    transcript,
                    f"{company.name} | industry={company.industry or ''} | description={company.description or ''} | verified_product={product_ctx}",
                    lead_ctx,
                )
            except Exception as e:
                logger.error(f"Analysis error: {e}")

    # Deterministic intent override: explicit follow-up requests must win over
    # a conservative LLM result such as "contacted". This also protects the
    # database when the post-call LLM analysis fails.
    low_tx = transcript.casefold()
    explicit_site_visit = any(x in low_tx for x in (
        "site visit", "site-visit", "site pe", "site par", "site mein", "site me",
        "site dekh", "site jaana", "site jana", "site ja", "property dekh",
        "property visit", "property dekhna", "ghar dekh", "property dekhne",
        "visit kar", "visit chahi", "visit ke liye", "visit karna",
        "visit karenge", "visit pe", "visit par",
    ))
    explicit_meeting = any(x in low_tx for x in (
        "meeting", "milna hai", "milne", "office aana", "office meeting"
    ))
    explicit_callback = any(x in low_tx for x in (
        "callback", "call back", "baad mein call", "baad me call",
        "phir call", "call kar lena", "dobara call"
    ))
    explicit_human = any(x in low_tx for x in (
        "human", "human call", "human se baat", "human se connect",
        "insaan", "insaan se baat", "kisi insaan", "aadmi se baat",
        "agent se baat", "real agent", "specialist", "manager se baat",
        "representative", "person se baat", "kisi se baat",
        "team se baat", "team se connect",
    ))
    if explicit_site_visit:
        analysis.update({
            "lead_status": "hot",
            "intent": "site_visit",
            "follow_up_required": True,
            "follow_up_note": "Admin to confirm site visit manually.",
        })
        if not analysis.get("summary") or "no structured" in str(analysis.get("summary")).casefold():
            analysis["summary"] = "Customer requested a site visit. Admin needs to confirm the visit manually."
    elif explicit_meeting and analysis.get("lead_status") not in ("closed_won", "closed_lost", "do_not_call"):
        analysis.update({"lead_status": "warm", "intent": "meeting", "follow_up_required": True, "follow_up_note": "Admin to confirm meeting manually."})
    elif explicit_human and analysis.get("lead_status") not in ("closed_won", "closed_lost", "do_not_call"):
        analysis.update({
            "lead_status": "warm",
            "intent": "human_followup",
            "follow_up_required": True,
            "follow_up_note": "Admin to arrange human follow-up.",
        })
        analysis["transferred_to_human"] = True
        if not analysis.get("summary"):
            analysis["summary"] = "Customer requested to speak with a human representative. Admin should arrange human follow-up."
        analysis["note"] = "Customer requested a human representative; admin to follow up."
    elif explicit_callback and analysis.get("lead_status") not in ("closed_won", "closed_lost", "do_not_call"):
        analysis.update({"lead_status": "warm", "intent": "wants_callback", "follow_up_required": True, "follow_up_note": "Admin to arrange callback."})

    # Never persist the old generic fallback text. If analysis somehow ended
    # up empty, derive a transcript-grounded result before touching CallLog.
    if not analysis:
        analysis = llm_service._deterministic_call_analysis(transcript)

    duration = 0
    if session.get("started_at"):
        try:
            started  = datetime.fromisoformat(session["started_at"])
            duration = int((datetime.utcnow() - started).total_seconds())
        except Exception:
            pass

    # Prefer the session's stashed live_call_uuid (set by the streaming
    # flow — see vobiz_stream_webhook.py — when the Live tab's card was
    # created under a different id than call_uuid). Record-mode sessions
    # never set this, so live_call_uuid is None there and this falls
    # straight back to call_uuid, unchanged from before.
    await live_broadcaster.call_end(company_id, session.get("live_call_uuid") or call_uuid, duration)

    async with AsyncSessionLocal() as db:
        # The websocket session normally carries call_log_id. If the session was
        # cleaned up/raced before the hangup webhook, recover the CallLog by the
        # provider CallUUID instead of silently skipping the entire post-call save.
        resolved_call_log_id = call_log_id
        if not resolved_call_log_id:
            r = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
            existing_log = r.scalar_one_or_none()
            resolved_call_log_id = str(existing_log.id) if existing_log else None
            if resolved_call_log_id:
                logger.warning(
                    f"Recovered CallLog by call_control_id during hangup | call_uuid={call_uuid[:12]}"
                )

        updates = {
            "status": "completed", "ended_at": datetime.utcnow(),
            "duration_seconds": duration, "conversation_history": history,
            "transcript": transcript, "summary": analysis.get("summary", ""),
            "sentiment": analysis.get("sentiment", ""), "intent": analysis.get("intent", ""),
            "lead_status_after": analysis.get("lead_status", ""),
            "transferred_to_human": analysis.get("transferred_to_human", False),
        }
        await _update_log(resolved_call_log_id, updates, db)

        # If the CallLog still cannot be resolved, create/update it from the
        # webhook UUID so a completed answered call is never lost from Call Logs.
        if not resolved_call_log_id:
            r = await db.execute(select(CallLog).where(CallLog.call_control_id == call_uuid))
            existing_log = r.scalar_one_or_none()
            if existing_log:
                for k, v in updates.items():
                    setattr(existing_log, k, v)
                await db.commit()
                resolved_call_log_id = str(existing_log.id)
            else:
                logger.error(
                    f"Unable to resolve CallLog after answered hangup | call_uuid={call_uuid[:12]} | lead_id={lead_id}"
                )

        if lead_id:
            lead = await _get_lead(lead_id, db)
            if lead:
                valid = ["new","contacted","interested","warm","hot","cold",
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
                # Persist a short, actionable note. Never copy the full AI summary
                # or transcript into the lead Notes field.
                intent = analysis.get("intent")
                if intent == "site_visit":
                    compact_note = "Customer requested a site visit. Admin to confirm manually."
                elif intent == "meeting":
                    compact_note = "Customer requested a meeting. Admin to confirm manually."
                elif intent == "wants_callback":
                    compact_note = "Customer requested a callback. Admin to follow up."
                elif intent == "human_followup":
                    compact_note = "Customer requested a human representative. Admin to follow up."
                elif intent == "not_interested":
                    compact_note = "Customer is not interested."
                else:
                    # Prefer the analyzer's dedicated CURRENT lead note. Fall
                    # back to its short summary only if the note is absent.
                    summary = " ".join(str(analysis.get("note") or analysis.get("summary") or "").split())
                    compact_note = summary[:220].rstrip() if summary else "No clear next action captured."

                # Lead Notes is the CURRENT lead state, not call history.
                # Full transcript/history remains in CallLog.
                compact_note = " ".join(str(compact_note or "").split()).strip()
                if len(compact_note) > 220:
                    compact_note = compact_note[:217].rsplit(" ", 1)[0] + "..."
                lead.notes = compact_note or "No clear next action captured."
                # Streaming calls increment this at answer-stream. Record-mode
                # calls do not, so only increment here when this call has not
                # already been counted by its last_called_at timestamp.
                try:
                    started_dt = datetime.fromisoformat(session["started_at"]) if session.get("started_at") else None
                except Exception:
                    started_dt = None
                if not started_dt or not lead.last_called_at or lead.last_called_at < started_dt:
                    lead.call_attempts = int(lead.call_attempts or 0) + 1
                lead.last_called_at = datetime.utcnow()
                lead.updated_at = datetime.utcnow()
                await db.commit()

    # ── Minute deduction (Cashfree plan system) ─────────────────────────────
    # This used to call a Firebase-backed minutes_service that didn't exist
    # anywhere in this project — silently failing every call (see the old
    # "Minutes deduction error" log spam) and doing nothing. Now backed by
    # app/services/plan_service.py against Company.plan_minutes_used
    # (plain SQL column, no external service). `duration` here is the same
    # value just written to CallLog.duration_seconds above — kept as ONE
    # elapsed-time calculation (session["started_at"] -> now) so the
    # minutes a company is billed for always match what the Call Log
    # screen shows for this same call.
    if duration > 0 and company_id:
        async with AsyncSessionLocal() as db:
            company = await _get_company(company_id, db)
            if company:
                plan_service.record_minutes_used(company, duration)
                await db.commit()
            else:
                logger.warning(f"Minute deduction skipped — no company for company_id={company_id} | call_uuid={call_uuid[:12]}")

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
        # REVERTED: previously tried adding timeout="..." alongside
        # silence="..." to fix recordings running to the full maxLength
        # cap (see below). That guess was wrong — Vobiz's XML validator
        # rejected the extra attribute outright and hung up the call
        # immediately with HangupCauseName="Invalid Answer XML" before the
        # caller heard anything. Back to the single known-working
        # attribute. The maxLength-cap issue is still real (see prior
        # logs) but needs the actual attribute name confirmed from
        # Vobiz's docs/support before touching this again — guessing
        # unknown attribute names on this provider is unsafe, not just
        # ineffective; it can invalidate the whole response.
        listen = (
            f'<Record action="{action_url}" method="POST" '
            f'maxLength="{RECORD_MAX_LENGTH_SECONDS}" silence="{RECORD_SILENCE_SECONDS}" '
            f'finishOnKey="" />'
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


def _make_continue_url(company_id: Optional[str], lead_id: Optional[str], mode: Optional[str]) -> str:
    return (
        f"{_get_base_url()}/api/v1/vobiz/continue"
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
            f"Jawab BAHUT CHHOTE rakhein — 1 chhota sentence, kabhi kabhi 2. "
            f"Jaise real phone call pe log bolte hain, lecture nahi."
        )
    else:
        base += (
            f"\nInbound support call. Sawaal ka seedha jawab dein. "
            f"BAHUT CHHOTA rakhein — 1 chhota sentence, kabhi kabhi 2."
        )
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

# Fields the per-turn reply/TTS path actually reads off `company` (voice
# config, prompts, business info). Snapshotted into the Redis session at
# /answer so _build_reply_response / _error_response / gather_callback
# don't each re-hit Postgres for the same row every single turn — company
# config doesn't change mid-call.
_COMPANY_SNAPSHOT_FIELDS = [
    "id", "name", "agent_name", "description", "description_hi",
    "services", "services_hi", "faqs", "faqs_hi", "products", "active_product",
    "voice_gender", "tts_provider", "tts_voice", "forward_number",
    "greeting_inbound_hi", "greeting_outbound_hi", "vobiz_phone_number",
]

def _company_snapshot(company: Company) -> Dict:
    return {f: getattr(company, f, None) for f in _COMPANY_SNAPSHOT_FIELDS}

def _company_from_snapshot(snapshot: Optional[Dict]) -> Optional[Any]:
    """Rebuilds a lightweight attribute-access view of Company from the
    cached snapshot dict — every place that reads `company.X` downstream
    (get_vobiz_voice, _xml_prompt, _synthesize_sarvam, _build_hindi_prompt)
    only ever does getattr(), so a SimpleNamespace works as a drop-in."""
    if not snapshot:
        return None
    from types import SimpleNamespace
    return SimpleNamespace(**snapshot)

async def _company_for_session(session: Dict, db_fallback=True) -> Optional[Any]:
    """Preferred way to get a company-like object for a turn: read the
    cached snapshot from the session (no DB hit) and only fall back to a
    real Postgres query if the session predates this cache or the
    snapshot is missing for some other reason."""
    company = _company_from_snapshot(session.get("company_snapshot"))
    if company is not None:
        return company
    if not db_fallback:
        return None
    async with AsyncSessionLocal() as db:
        return await _get_company(session["company_id"], db)

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