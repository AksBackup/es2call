"""
Low-latency call pipeline for Vobiz's bidirectional media stream.

Rewritten to use Vobiz's OWN official Pipecat integration package
(`pipecat-vobiz` on PyPI — see requirements.txt) instead of a hand-rolled
serializer. That change fixes real, confirmed-against-source bugs the old
hand-rolled version had:

  - Outbound audio must be sent as `{"event": "playAudio", "media": {...}}`
    — the old code sent `{"event": "media", ...}`, which Vobiz's server
    does not recognize as a valid inbound-to-Vobiz message at all, so even
    with every import fixed the caller would never have heard anything.
  - Barge-in/interruption must send `{"event": "clearAudio", ...}`, not
    `{"event": "clear", ...}`.
  - The exact `start` event field names (`streamId`, `callId`,
    `mediaFormat.encoding`, `mediaFormat.sampleRate`) are now read via
    `parse_vobiz_start()`, which is Vobiz's own helper, instead of the old
    code's defensive multi-key guessing.

Replaces the per-turn HTTP+XML round trip in vobiz_webhook.py's Gather
flow with one persistent WebSocket: caller audio streams in continuously,
STT/LLM/TTS run as a pipeline, and reply audio streams back as it's
generated — instead of "wait for full reply, then POST a new webhook".

Only used when the company's resolved TTS provider is "sarvam" or
"deepgram" (see resolve_tts_provider in app/services/tts/providers.py).
"vobiz" as a provider stays on the existing XML <Speak>/<Gather> flow,
since Vobiz has no standalone synthesize API to plug in here.
"""
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Auto call-cut on caller silence ─────────────────────────────────────────
# If the callee picks up and never says anything (or goes silent mid-call),
# the pipeline previously just sat there indefinitely — the caller could
# leave the line open with dead air for the whole call, burning minutes
# with no way to end automatically. SILENCE_NUDGE_SECONDS is how long to
# wait after the last thing we heard (or the greeting finishing) before
# playing one "are you there?" prompt; SILENCE_HANGUP_SECONDS is how much
# additional silence after that nudge before the call is auto-disconnected.
SILENCE_NUDGE_SECONDS: float = 15.0
# Additional silence after the nudge before the provider call is actually hung up.
# Total idle protection is therefore ~60 seconds (15s + 45s), excluding the
# greeting itself.  This is deliberately separate from Smart Turn/VAD, which
# only decides when an individual speech turn has ended.
SILENCE_HANGUP_SECONDS: float = 45.0


async def run_vobiz_stream_pipeline(
    websocket,
    call_uuid: str,
    company: Any,
    lead: Any,
    mode: str,
    greeting: str,
    live_call_uuid: Optional[str] = None,
    product_focus: Optional[str] = None,
) -> None:
    """
    Entry point called by the /api/v1/vobiz-stream/media-stream WebSocket
    route, AFTER websocket.accept() has already been called there.

    Builds and runs one Pipecat pipeline for the lifetime of this call.
    """
    # `live_call_uuid` is the id the Live Call tab's session card actually
    # uses (see vobiz_stream_webhook.py) — can differ from call_uuid
    # (Vobiz's CallUUID) when a pre-existing "ringing" card was created at
    # dial time under a different id. Falls back to call_uuid so this
    # function still works if a caller doesn't pass it. Used ONLY for the
    # live_broadcaster.* calls below — call_uuid itself is untouched
    # everywhere else (session history, logging).
    live_call_uuid = live_call_uuid or call_uuid

    # Imports are local to this function so importing this module doesn't
    # hard-require pipecat/pipecat-vobiz unless this streaming path is
    # actually used (the "vobiz" native provider never touches this file).
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.frames.frames import (
        TTSSpeakFrame, TranscriptionFrame, TextFrame, EndFrame,
        LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMContextFrame,
    )
    import asyncio
    from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.serializers.vobiz import VobizFrameSerializer, parse_vobiz_start
    from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    from app.core.config import settings
    from app.services.llm.prompts import build_hindi_prompt, relevant_knowledge

    def _resolve_tts_provider(requested: str, language: str) -> tuple[str, Optional[str]]:
        """
        Guards against picking a TTS provider that can't actually speak the
        requested language. Inlined here (rather than imported from
        app/services/tts/providers.py) because that file is currently
        entirely commented out in this codebase — if you restore it later,
        feel free to swap this back to an import instead.

        Deepgram Aura-2 doesn't support Hindi as of writing (confirmed via
        developers.deepgram.com/docs/tts-models — en/es/de/fr/nl/it/ja only),
        so Hindi/Hinglish calls requesting "deepgram" fall back to "sarvam".
        """
        DEEPGRAM_AURA_SUPPORTED_LANGS = {"en", "es", "de", "fr", "nl", "it", "ja"}
        provider = (requested or "vobiz").lower()
        lang_short = (language or "hi").lower().split("-")[0]

        # BUG FIX (Aug 2026): "vobiz" was being passed straight through
        # unchanged, which then crashed _build_tts_service() below with
        # "'vobiz' has no Pipecat streaming path" — Vobiz's native TTS is
        # the OLD XML <Speak> per-turn flow (app/services/tts/providers.py),
        # which has no equivalent in this Pipecat streaming pipeline at
        # all. "vobiz" is BOTH Company.tts_provider's AND
        # settings.TTS_PROVIDER's default value, and USE_STREAMING_CALLS
        # defaults to True — so this crashed on every single streaming
        # call for any company that hadn't explicitly picked
        # sarvam/deepgram in Settings, which is every fresh account,
        # confirmed by the exact traceback this fix responds to. Treat
        # "vobiz" here as "no real streaming provider was actually
        # chosen" and pick the right default for the call's language:
        # Sarvam for Hindi/Hinglish (purpose-built for it — see every
        # other Sarvam comment in this file), Deepgram Aura-2 for English.
        if provider == "vobiz":
            fallback = "sarvam" if lang_short not in DEEPGRAM_AURA_SUPPORTED_LANGS else "deepgram"
            return fallback, (
                f"tts_provider='vobiz' has no Pipecat streaming path — using "
                f"'{fallback}' for this call instead. Set a real streaming "
                f"provider (sarvam/deepgram) in Settings to silence this warning."
            )

        if provider == "deepgram" and lang_short not in DEEPGRAM_AURA_SUPPORTED_LANGS:
            warning = (
                f"Deepgram Aura-2 does not support language='{language}' — "
                f"falling back to sarvam for this call."
            )
            return "sarvam", warning
        return provider, None

    # ── Read Vobiz's authoritative "start" event off the socket FIRST ──────
    # This is a raw websocket.receive_text() call — it must happen before
    # the Pipecat transport starts its own receive loop, and it's what
    # tells us the real streamId/callId and the wire audio format Vobiz
    # actually negotiated (mediaFormat), rather than guessing/assuming.
    parsed = await parse_vobiz_start(websocket)
    logger.info(
        f"Vobiz stream start | call_uuid={call_uuid[:12] if call_uuid else '?'} | "
        f"streamId={parsed['stream_id']!r} callId={parsed['call_id']!r} "
        f"mediaFormat=({parsed['encoding']!r}, {parsed['sample_rate']})"
    )
    vobiz_call_id = parsed["call_id"] or call_uuid
    vobiz_sample_rate = parsed["sample_rate"] or 8000
    vobiz_encoding = parsed["encoding"] or "audio/x-mulaw"

    language_code = "hi-IN" if mode != "english" else "en-US"
    provider, warning = _resolve_tts_provider(
        getattr(company, "tts_provider", "vobiz"), language_code,
    )
    if warning:
        logger.warning(f"call_uuid={call_uuid[:12]} | {warning}")

    gender = (getattr(company, "voice_gender", None) or "female").lower()
    voice_override = getattr(company, "tts_voice", None)

    # Per-company Vobiz credentials for every real customer — no fallback
    # to a shared/global .env credential for them. Each customer must set
    # their own vobiz_auth_id/vobiz_auth_token in Settings; without it,
    # this stream's TTS (Vobiz's own Speak Text API, used to send audio
    # back on this same call) will 401 rather than silently running under
    # someone else's account.
    #
    # ONE exception: the shared demo account (company.is_demo_account).
    # Falls back to settings.VOBIZ_AUTH_ID/VOBIZ_AUTH_TOKEN from .env only
    # for that account, and only for whichever field is left blank — real
    # customer accounts are unaffected.
    vobiz_auth_id = getattr(company, "vobiz_auth_id", None) or ""
    vobiz_auth_token = getattr(company, "vobiz_auth_token", None) or ""
    if getattr(company, "is_demo_account", False) and (not vobiz_auth_id or not vobiz_auth_token):
        vobiz_auth_id = vobiz_auth_id or (settings.VOBIZ_AUTH_ID or "")
        vobiz_auth_token = vobiz_auth_token or (settings.VOBIZ_AUTH_TOKEN or "")
    if not vobiz_auth_id or not vobiz_auth_token:
        logger.error(f"call_uuid={call_uuid[:12]} | No Vobiz credentials set for this company — set vobiz_auth_id/vobiz_auth_token in Settings")

    # ── Transport: Vobiz <-> Pipecat over the raw WebSocket ────────────────
    serializer = VobizFrameSerializer(
        stream_id=parsed["stream_id"],
        call_id=vobiz_call_id,
        auth_id=vobiz_auth_id,
        auth_token=vobiz_auth_token,
        params=VobizFrameSerializer.InputParams(
            vobiz_sample_rate=vobiz_sample_rate,
            encoding=vobiz_encoding,
            sample_rate=None,  # take pipeline rate from StartFrame.audio_in_sample_rate below
            auto_hang_up=True,   # sends {"event":"stop"} + REST DELETE safety net on EndFrame
        ),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,   # CRITICAL for telephony — raw frames, no WAV container
            serializer=serializer,
            # NOTE: vad_analyzer does NOT go here in pipecat 1.x — it's a
            # silent no-op on the transport. It's wired on
            # LLMUserAggregatorParams below instead.
        ),
    )

    # ── STT: Deepgram streaming (Nova-2 supports Hindi via language="hi") ──
    # Deepgram's default endpointing finalizes very eagerly on brief mid-
    # sentence pauses — confirmed in call logs: one continuous caller
    # utterance ("...call to location available at... apartment.") came
    # through as TWO separate "final" transcripts, and each one
    # independently triggered LLMUserAggregator's
    # TranscriptionUserTurnStartStrategy → a full LLM inference cycle. The
    # result was two uncoordinated bot responses stacked back to back with
    # no user turn between them, each answering a different half of the
    # same sentence — which read as the bot ignoring what was actually
    # asked. `utterance_end_ms` tells Deepgram to hold off finalizing
    # until a real pause (not just a breath), giving one coherent final
    # per turn instead of fragments.
    # BUG FIX (Aug 2026) — "AI doesn't understand/remember what caller
    # asked, Deepgram not listening correctly", Hindi calls only:
    #
    # This was pinned to model="nova-2", language="hi" — a MONOLINGUAL
    # Hindi model. Real Hindi sales/support calls are Hinglish: callers
    # constantly drop in English words (product names, numbers, "yes",
    # "okay", brand/company names). A monolingual Hindi model has no
    # option but to force those English words into Hindi phonetics,
    # which mangles or drops them outright — the LLM then genuinely IS
    # answering the wrong question, not "forgetting" anything; the
    # transcript it received was already wrong. This looks identical to
    # a memory bug from the caller's side but the context aggregator
    # (see LLMContext below) is retaining turns correctly.
    #
    # Fix: nova-3 with language="multi" is Deepgram's dedicated
    # Hindi<->English code-switching mode (one of the 10 languages it
    # explicitly supports for real-time code-switching — see
    # developers.deepgram.com/docs/multilingual-code-switching). It
    # transcribes whichever language each word/phrase is actually said
    # in instead of forcing everything into one script.
    #
    # keywords= biases the model toward the caller's own agent name /
    # company name / product names — exactly the tokens most likely to
    # be OOV proper nouns a generic model mishears.
    #
    # BUG FIX (Aug 2026): this was being passed unconditionally, on
    # every call regardless of which Deepgram model got selected below.
    # keywords does NOT work with nova-3 at all — confirmed against
    # Deepgram's own docs and SDK maintainers (deepgram/deepgram-python-
    # sdk#515; livekit's deepgram plugin docs state it explicitly:
    # "keywords does not work with Nova-3 models. Use keyterm instead.").
    # Deepgram rejects the WebSocket handshake outright (400,
    # "Unexpected error when initializing websocket connection") the
    # moment both are present together. Since nova-3 is exactly what
    # every Hindi/Hinglish call uses (see model= below) — this product's
    # primary use case — STT was failing to connect AT ALL on every
    # non-English call, not just degrading in quality. Restricting
    # keywords to the nova-2/English path only (where it IS supported —
    # confirmed against the actual pinned deepgram-sdk LiveOptions
    # dataclass) restores basic Hindi call functionality. nova-3 has its
    # own equivalent (`keyterm`) but that field's exact availability
    # wasn't re-verified against this environment's actual deepgram-sdk
    # version (7.7.0, confirmed from this bug's own traceback) before
    # this fix — leaving nova-3 calls without keyword boosting for now
    # rather than guess wrong a second time on a call-breaking parameter.
    #
    # endpointing / utterance_end_ms deliberately left as-is — those
    # values were already tuned against real call logs (see comment
    # above) to fix a *different*, confirmed issue (one utterance being
    # split into two finals). Re-tune only if nova-3/multi changes that
    # behavior in testing; don't change blind.
    _keyterms = [t for t in {
        (company.agent_name or "").strip(),
        (company.name or "").strip(),
        *[
            (p.get("name_hi") or p.get("name") or "").strip()
            for p in (company.products or [])
        ],
    } if t]
    _model = "nova-3" if mode != "english" else "nova-2"

    # BUG FIX (Aug 2026) — "STT not listening to / not recognizing Hindi":
    # `model=` and `language=` were being passed as TOP-LEVEL kwargs to
    # DeepgramSTTService(...). Checked against the actual installed
    # pipecat-ai (1.7.0) source: DeepgramSTTService.__init__ has NO
    # `model` or `language` parameter anymore (moved to `live_options`/
    # `settings` — this constructor signature changed upstream at some
    # point after this file was first written). Passing them as bare
    # kwargs meant they silently fell into **kwargs and were forwarded to
    # the parent FrameProcessor's __init__, which does nothing with them —
    # they never reached Deepgram's actual connection settings at all.
    # The service was silently falling back to its hardcoded default
    # (model="nova-3-general", language=Language.EN — ENGLISH ONLY) on
    # every single call, regardless of `_model`/mode computed above. That
    # is the actual reason Hindi/Hinglish speech wasn't being recognized:
    # Deepgram was never told to listen for anything but English, and a
    # transcript produced under the wrong language is also why the LLM
    # looked like it was "not responding correctly" — it was correctly
    # answering a garbled/empty English mis-transcription of what was
    # actually said in Hindi.
    #
    # Fix: model/language now live INSIDE LiveOptions(...), which is the
    # path DeepgramSTTService actually reads from (confirmed against
    # source: LiveOptions.to_dict() is merged into Settings when
    # `live_options=` is supplied). Functionally identical to before,
    # just passed where the library actually looks for it.
    stt = DeepgramSTTService(
        api_key=settings.DEEPGRAM_API_KEY,
        live_options=LiveOptions(
            model=_model,
            language="multi" if mode != "english" else "en",
            interim_results=True,
            utterance_end_ms="1200",
            vad_events=True,
            endpointing=300,
            **({"keywords": _keyterms} if _model == "nova-2" and _keyterms else {}),
        ),
    )

    # ── LLM: use a dedicated low-latency voice model ───────────────────────
    # Live calls should not share the larger post-call analysis model. GPT-OSS
    # 20B is substantially faster on Groq and is enough for short conversational
    # turns. We also disable the OpenAI SDK's automatic retries: a 429 retry can
    # otherwise leave a caller waiting 10-30 seconds.
    llm = _build_llm_service(settings, voice=True, mode=mode)

    # ── TTS: Sarvam or Deepgram Aura, per resolve_tts_provider() above ─────
    tts, tts_sample_rate = _build_tts_service(provider, settings, gender, voice_override, language_code)

    # Batch product_focus is authoritative. Only fall back to active_product
    # when the call was not created from a product-specific batch.
    if not product_focus:
        product_focus = getattr(company, "active_product", None)
    system_prompt = build_hindi_prompt(company, lead, rag_context="", mode=mode, product_focus=product_focus)
    # Tell the model directly, in the prompt, that it already greeted —
    # rather than relying on the greeting showing up as an assistant turn
    # in context. That still depended on frame/pipeline timing: if the
    # caller says anything before the greeting audio finishes playing
    # (confirmed happening — background noise or an early "Hello?" is
    # common), the LLM gets invoked before context_aggregator.assistant()
    # has captured the greeting, sees no assistant turns at all, and
    # reintroduces itself in its own words. This is deterministic instead
    # — always true regardless of when the model first gets invoked.
    system_prompt += "\nGREETING_ALREADY_SPOKEN: yes. Do not introduce yourself or repeat the opening greeting."
    context = LLMContext(
        messages=[
            {"role": "system", "content": system_prompt},
        ]
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # start_secs raised slightly above pipecat's default (0.2 -> 0.3):
            # logs showed frequent barge-in interruptions firing very close
            # together with TTS starting, consistent with brief noise/breath
            # sounds being treated as a real interruption and cutting the
            # bot's own reply off mid-sentence. 0.3s requires marginally more
            # sustained speech before confirming a real interruption, without
            # making genuine fast barge-ins feel sluggish. If real
            # interruptions start feeling delayed, drop this back toward 0.2.
            # min_volume/confidence left at Silero's defaults (0.6/0.7) were
            # tuned for clean browser-mic input. Phone audio arrives here as
            # compressed 8kHz mulaw over a carrier network — generally
            # quieter and noisier — so that threshold could reject legitimate
            # but softer-spoken caller audio as silence, which is consistent
            # with "the bot doesn't seem to hear the caller" reports. Lowered
            # both to be more permissive for telephony-quality audio.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(start_secs=0.3, confidence=0.5, min_volume=0.35)),
            # THE main latency fix: pipecat's default turn-stop strategy is
            # TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3()),
            # and LocalSmartTurnAnalyzerV3's default SmartTurnParams.stop_secs
            # is 3.0 — a hard 3-second silence fallback used whenever the
            # semantic "is this sentence finished?" model isn't confident.
            # That fallback is exactly what the logs showed ("End of Turn
            # complete due to stop_secs. Silence in ms: 3000.0") — a flat 3s
            # tax added on top of STT+LLM+TTS time, independent of Vobiz or
            # network latency entirely. Dropping it to 1.0s keeps the
            # semantic model's benefit (it still fires immediately when
            # confident — see the 200ms-ish COMPLETE results in the logs)
            # while capping the worst case. If real calls start getting cut
            # off mid-sentence, raise this — don't drop it all the way back
            # to 3.0 first, try 1.5-2.0.
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=1.0))
                )],
            ),
        ),
    )

    # Mutable holder (plain dict, not a local var) so the nested watchdog
    # coroutine and _LiveTap.process_frame above can both read/write the
    # same state without needing `nonlocal` on every access.
    _silence_state = {"last_activity": datetime.utcnow(), "nudged": False}
    _watchdog_holder: dict = {}

    async def _silence_watchdog() -> None:
        """Auto-disconnects a call where the caller picked up but never
        said anything, or went silent partway through — see the module
        docstring above SILENCE_NUDGE_SECONDS for why this exists. Plays
        one gentle nudge, then hangs up if there's still no response."""
        try:
            while True:
                await asyncio.sleep(1.0)
                idle = (datetime.utcnow() - _silence_state["last_activity"]).total_seconds()
                if not _silence_state["nudged"] and idle >= SILENCE_NUDGE_SECONDS:
                    _silence_state["nudged"] = True
                    _silence_state["last_activity"] = datetime.utcnow()
                    nudge_text = (
                        "Hello? Are you still there?" if mode == "english"
                        else "Hello? Kya aap wahin hain? Main aapki awaaz nahi sun paa raha hoon."
                    )
                    logger.info(f"Silence nudge | call_uuid={call_uuid[:12]}")
                    await task.queue_frames([TTSSpeakFrame(text=nudge_text)])
                elif _silence_state["nudged"] and idle >= SILENCE_HANGUP_SECONDS:
                    if not _call_state["ending"]:
                        _call_state["ending"] = True
                        logger.info(
                            f"Silence timeout — auto-disconnecting call | "
                            f"call_uuid={call_uuid[:12]} | idle_after_nudge={idle:.1f}s"
                        )
                        farewell = (
                            "I'm not getting a response, so I'll disconnect now. Thank you!"
                            if mode == "english"
                            else "Lagta hai koi jawab nahi mil raha, isliye main call disconnect kar raha hoon. Dhanyavaad!"
                        )
                        await _finish_call_after_farewell(farewell, "caller_idle_timeout")
                    return
        except asyncio.CancelledError:
            pass

    # Explicit caller-requested end-call phrases are handled locally so the
    # call does not depend on a second LLM classifier or a fragile action
    # processor. This saves TPM and makes "call cut kar do" deterministic.
    _END_PHRASES = (
        # English / Hinglish. Keep these short enough to catch natural STT
        # variants such as "phone rakho aap" / "phone rakh do" without
        # requiring the LLM to classify the intent.
        "call cut", "call kat", "call band", "call bandh", "phone cut",
        "phone rakh", "disconnect", "end the call", "end call", "hang up",
        "goodbye", "good bye", "bye", "that's all", "that is all",
        "bas itna hi", "bas ho gaya", "theek hai bye", "thik hai bye",
        "nahi chahiye bye", "not interested", "stop calling", "call mat karna",
        "aur kuch nahi", "bas karo", "bas kar do", "phone kaat", "call kaat",
        "rakh do", "theek hai rakh do", "thik hai rakh do",
        # Devanagari — Deepgram can return Hindi script for the same intent.
        "फोन रख", "फोन रखना", "फोन रखो", "फोन रख दो", "अभी फोन रखो",
        "ठीक है फोन रखो", "ठीक है फोन रख दो", "अच्छा फोन रखो", "बस फोन रखो",
        "कॉल काट", "कॉल बंद", "कॉल बंद करो", "कॉल बंद कर दो", "कॉल रखो",
        "कॉल रख दो", "अलविदा", "बाय", "गुडबाय", "बस इतना ही", "बस हो गया",
        "मुझे फोन रखना है", "फोन रखना है", "अब फोन रखो", "अब फोन रख दो",
    )
    _end_task_holder = {"task": None}
    _call_state = {
        "ending": False,
        "provider_hangup_requested": False,
    }

    def _normalize_end_text(text: str) -> str:
        # STT frequently adds punctuation/extra whitespace. Unicode casefold
        # also handles the Devanagari variants without changing their script.
        text = (text or "").replace("’", "'").replace("‘", "'")
        return " ".join(text.casefold().split())

    def _is_explicit_end(text: str) -> bool:
        low = _normalize_end_text(text)
        if not low:
            return False
        return any(p in low for p in _END_PHRASES)

    async def _request_provider_hangup(reason: str) -> bool:
        """Request the carrier hangup exactly once.

        EndFrame only stops the Pipecat pipeline/websocket; it is NOT the
        telephony hangup. Always call the Vobiz API first so the provider sends
        its authoritative /hangup webhook, which owns DB/lead finalization.
        """
        if _call_state["provider_hangup_requested"]:
            return True
        _call_state["provider_hangup_requested"] = True
        try:
            from app.services.telephony.vobiz_service import vobiz_service
            ok = await vobiz_service.hangup(call_uuid, company)
            logger.info(
                f"Provider hangup requested | reason={reason} | "
                f"call_uuid={call_uuid[:12]} | provider_ok={ok}"
            )
            return bool(ok)
        except Exception as exc:
            # Do not swallow the exception silently: if the carrier request
            # fails, the websocket can remain alive and the caller can be
            # charged. The next cleanup path/webhook can still finish DB work.
            logger.exception(
                f"Provider hangup request failed | reason={reason} | "
                f"call_uuid={call_uuid[:12]} | error={exc}"
            )
            _call_state["provider_hangup_requested"] = False
            return False

    async def _finish_call_after_farewell(
        farewell: str = "Theek hai ji, thank you. Aapka din achha rahe.",
        reason: str = "caller_requested_end",
    ):
        try:
            await task.queue_frames([TTSSpeakFrame(text=farewell)])
            # Give Sarvam enough time to emit/play the short farewell. The
            # provider hangup itself is still explicit and happens immediately
            # after this bounded grace period; it no longer depends on a
            # websocket EndFrame alone.
            await asyncio.sleep(2.0)
            await _request_provider_hangup(reason)
            await task.queue_frames([EndFrame()])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                f"End-call farewell cleanup failed | reason={reason} | "
                f"call_uuid={call_uuid[:12]} | error={exc}"
            )
            # If queueing TTS failed, still make the provider hangup request.
            await _request_provider_hangup(reason + ":fallback")

    class _EndCallGuard(FrameProcessor):
        """Intercept caller hang-up requests before they reach the LLM.

        Once an end request is detected, all later caller text frames are
        dropped so a trailing 'hello'/'okay' cannot start another LLM turn
        while the farewell is being spoken and the provider hangup is pending.
        """
        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if _call_state["ending"]:
                if isinstance(frame, (TranscriptionFrame, TextFrame)):
                    return
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, (TranscriptionFrame, TextFrame)):
                text = getattr(frame, "text", None)
                if text and _is_explicit_end(text):
                    _call_state["ending"] = True
                    from app.services.telephony.call_session import session_manager
                    from app.api.routes.live_ws import live_broadcaster
                    await session_manager.add_turn(call_uuid, "user", text)
                    await live_broadcaster.user_msg(company.id, live_call_uuid, text)
                    logger.info(
                        f"Explicit caller end intercepted before LLM | "
                        f"call_uuid={call_uuid[:12]} | text={text[:100]}"
                    )
                    if _end_task_holder["task"] is None or _end_task_holder["task"].done():
                        _end_task_holder["task"] = asyncio.create_task(_finish_call_after_farewell())
                    return
            await self.push_frame(frame, direction)

    class _LiveTap(FrameProcessor):
        """Passes every frame through completely unchanged — this must
        never alter the pipeline's behavior, only observe it. Does two
        things with user/assistant text as it flows through:
          1. Mirrors it to the Live Call tab via live_broadcaster (fixes
             the "just says Connecting" issue — see earlier fix).
          2. Records it via session_manager.add_turn() — this is what
             builds session["history"], which post-call analysis below
             reads to build the transcript. Nothing was calling
             add_turn() before, so history stayed [] for the whole call
             — which is the actual reason summary/sentiment/lead-status
             were always blank: analyze_call() either never ran (see
             below) or would have gotten an empty transcript even if it
             had.

        BUG FIX (Aug 2026) — "Live Call tab shows a new bubble for every
        word instead of one bubble per reply":
        This tap sits BEFORE `tts` in the pipeline (see Pipeline([...])
        below), so on the assistant side it sees the LLM's raw streamed
        output — pipecat's LLM services push out many small `TextFrame`/
        `LLMTextFrame` chunks (often single words or a few tokens each)
        as they're generated, specifically so the downstream TTS can
        start speaking before the full sentence is ready. Firing
        live_broadcaster.ai_msg() on every one of those chunks — as this
        used to do — meant every word landed as its own chat bubble on
        the frontend instead of one bubble per complete reply.
        The caller side never showed this bug: DeepgramSTTService only
        emits a `TranscriptionFrame` once per FINAL result (interim
        results come through as a different frame type this tap never
        matched), so user turns were already whole utterances.
        Fix: buffer assistant text between `LLMFullResponseStartFrame`
        and `LLMFullResponseEndFrame` — the two control frames pipecat
        sends around every complete LLM turn — and only mirror/record it
        once, as one full sentence, when the response is actually done.
        """
        def __init__(self, kind: str):
            super().__init__()
            self._kind = kind
            self._buffer: list = []  # assistant-side only — accumulates streamed chunks for one turn

        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)

            if self._kind == "ai":
                if isinstance(frame, LLMFullResponseStartFrame):
                    self._buffer = []
                elif isinstance(frame, TextFrame) and not isinstance(frame, TranscriptionFrame):
                    chunk = getattr(frame, "text", None)
                    if chunk:
                        self._buffer.append(chunk)
                elif isinstance(frame, LLMFullResponseEndFrame):
                    full_text = "".join(self._buffer).strip()
                    self._buffer = []
                    if full_text:
                        from app.api.routes.live_ws import live_broadcaster
                        from app.services.telephony.call_session import session_manager
                        await session_manager.add_turn(call_uuid, "assistant", full_text)
                        await live_broadcaster.ai_msg(company.id, live_call_uuid, full_text)
            else:
                text = getattr(frame, "text", None)
                if text and isinstance(frame, (TranscriptionFrame, TextFrame)):
                    # Defensive second interception. _EndCallGuard normally
                    # catches this first, but keeping the detector here makes
                    # the behavior resilient to Pipecat/STT frame variations.
                    if not _call_state["ending"] and _is_explicit_end(text):
                        _call_state["ending"] = True
                        from app.services.telephony.call_session import session_manager
                        from app.api.routes.live_ws import live_broadcaster
                        await session_manager.add_turn(call_uuid, "user", text)
                        await live_broadcaster.user_msg(company.id, live_call_uuid, text)
                        logger.info(
                            f"Explicit caller end intercepted in LiveTap fallback | "
                            f"call_uuid={call_uuid[:12]} | text={text[:100]}"
                        )
                        if _end_task_holder["task"] is None or _end_task_holder["task"].done():
                            _end_task_holder["task"] = asyncio.create_task(_finish_call_after_farewell())
                        return
                    from app.api.routes.live_ws import live_broadcaster
                    from app.services.telephony.call_session import session_manager
                    await session_manager.add_turn(call_uuid, "user", text)
                    # Caller actually said something — reset the silence
                    # watchdog below so it doesn't nudge/hang up mid-turn.
                    _silence_state["last_activity"] = datetime.utcnow()
                    _silence_state["nudged"] = False
                    await live_broadcaster.user_msg(company.id, live_call_uuid, text)
            await self.push_frame(frame, direction)

    class _VoiceContextCompactor(FrameProcessor):
        """Keep only the useful recent turns before each live LLM request.

        The aggregator retains the complete call transcript by design, but a
        phone LLM does not need the whole transcript on every turn. This also
        removes repeated STT fragments such as "Rent ke liye" followed by
        "Hello. Rent ke liye" that were appearing in the model context.
        """
        def __init__(self, max_turns: int = 8):
            super().__init__()
            self.max_turns = max_turns

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                messages = frame.context.messages
                if len(messages) > 1:
                    system = [m for m in messages if m.get("role") == "system"][:1]
                    turns = [m for m in messages if m.get("role") != "system"]
                    compact = []
                    for msg in turns:
                        content = str(msg.get("content") or "").strip()
                        if not content:
                            continue
                        if compact and msg.get("role") == compact[-1].get("role") == "user":
                            previous = str(compact[-1].get("content") or "").strip()
                            # Exact duplicate or a near-duplicate STT final.
                            a = " ".join(previous.lower().split())
                            b = " ".join(content.lower().split())
                            if a == b or a in b or b in a:
                                if len(content) > len(previous):
                                    compact[-1] = {"role": "user", "content": content}
                                continue
                        compact.append(msg)
                    frame.context.set_messages(system + compact[-self.max_turns:])
            await self.push_frame(frame, direction)

    context_compactor = _VoiceContextCompactor(max_turns=6)

    class _DynamicKnowledgeContext(FrameProcessor):
        """Inject only the facts relevant to the latest caller turn."""
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                messages = frame.context.messages
                latest_user = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        latest_user = str(m.get("content") or "").strip()
                        break
                dynamic = relevant_knowledge(company, latest_user, product_focus)
                for m in messages:
                    if m.get("role") == "system":
                        base = m.get("content", "")
                        marker = "\n\nTURN-RELEVANT FACTS:\n"
                        base = base.split(marker, 1)[0]
                        m["content"] = base + (marker + dynamic if dynamic else "")
                        break
            await self.push_frame(frame, direction)

    dynamic_knowledge = _DynamicKnowledgeContext()

    pipeline = Pipeline([
        transport.input(),
        stt,
        _EndCallGuard(),
        _LiveTap("user"),
        context_aggregator.user(),
        context_compactor,
        dynamic_knowledge,
        llm,
        _LiveTap("ai"),
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=vobiz_sample_rate,
            audio_out_sample_rate=tts_sample_rate,  # native TTS rate — VobizFrameSerializer resamples to 8kHz for the wire
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    # Speak the pre-built greeting directly via TTS the moment the socket
    # is live — mirrors what the XML flow's <Speak> greeting did, but
    # through the selected provider instead of Vobiz's own (Hindi-incapable)
    # Speak verb. Deliberately NOT routed through the LLM: that would cost
    # a full LLM round trip before the caller hears anything, AND the LLM
    # would generate its own opening line instead of speaking the actual
    # configured `greeting` text.
    _call_started_at = datetime.utcnow()

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        logger.info(f"Vobiz stream pipeline — client connected, speaking greeting | call_uuid={call_uuid[:12]}")
        from app.api.routes.live_ws import live_broadcaster
        await live_broadcaster.call_answered(company.id, live_call_uuid)
        await task.queue_frames([TTSSpeakFrame(text=greeting)])
        # Greeting is spoken directly (see comment above) rather than
        # through the LLM, so it never passes through the _LiveTap("ai")
        # hook in the pipeline — mirror it to Live Call AND record it in
        # session history manually so the transcript used for post-call
        # analysis doesn't start with a gap either.
        from app.services.telephony.call_session import session_manager
        await session_manager.add_turn(call_uuid, "assistant", greeting)
        await live_broadcaster.ai_msg(company.id, live_call_uuid, greeting)

    # Start the silence timer only once the greeting audio has actually
    # finished PLAYING (not the moment it's queued for TTS). Starting it
    # at queue-time meant the ~6-8s it takes to generate + play the
    # greeting was already eating most of SILENCE_NUDGE_SECONDS, leaving
    # the caller almost no real time to respond before the "Hello? Are
    # you there?" nudge fired — confirmed in call logs where the nudge
    # landed ~0.1s after the bot finished speaking. The watchdog task
    # itself is also only created here now, once, on first bot-stopped
    # event, guarded by _watchdog_holder so later utterances (nudges,
    # normal replies) don't spawn duplicate watchdog loops.
    @transport.event_handler("on_bot_stopped_speaking")
    async def _on_bot_stopped_speaking(_transport, _client):
        _silence_state["last_activity"] = datetime.utcnow()
        _silence_state["nudged"] = False
        if "task" not in _watchdog_holder:
            _watchdog_holder["task"] = asyncio.create_task(_silence_watchdog())

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        logger.info(f"Vobiz stream pipeline — client disconnected | call_uuid={call_uuid[:12]}")
        watchdog_task = _watchdog_holder.get("task")
        if watchdog_task:
            watchdog_task.cancel()
        from app.api.routes.live_ws import live_broadcaster
        duration = int((datetime.utcnow() - _call_started_at).total_seconds())
        # Immediate broadcast only, for a snappy Live Call UI update the
        # instant the media stream drops. The actual session finalize —
        # post-call analysis (summary/sentiment/lead-status) + DB writes
        # — happens exactly once, in vobiz_webhook.py's /hangup route,
        # which is the authoritative Vobiz-side call-end event (it also
        # already owns clearing the batch dispatch lock). This handler
        # used to ALSO call session_manager.end() + run the full
        # analysis/DB-write here, which raced with /hangup doing the same
        # thing on the same call_uuid — session_manager.end() deletes the
        # Redis session on first call, so whichever of the two fired
        # first "won" and the other silently no-op'd. If the winner threw
        # partway through (e.g. analyze_call() erroring), the session was
        # already gone and there was no fallback — which matches the
        # intermittent "lead status / call status / summary just didn't
        # update" reports. One source of truth now.
        await live_broadcaster.call_end(company.id, live_call_uuid, duration)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


def _build_llm_service(settings, voice: bool = False, mode: str = "sales"):
    """Build the LLM service used by the live call pipeline.

    Voice calls use the dedicated fast model and strict client settings.
    Post-call analysis continues to use settings.GROQ_MODEL elsewhere.
    """
    provider = (settings.LLM_PROVIDER or "groq").lower()

    if provider == "groq":
        from pipecat.services.groq.llm import GroqLLMService

        class VoiceGroqLLMService(GroqLLMService):
            def create_client(self, api_key=None, base_url=None, **kwargs):
                # The upstream BaseOpenAILLMService currently does not forward
                # arbitrary client kwargs from its constructor. Create the
                # client explicitly so Groq's SDK cannot retry a 429 for 10-30s.
                from openai import AsyncOpenAI
                import httpx
                return AsyncOpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    max_retries=0,
                    timeout=5.0,
                    http_client=httpx.AsyncClient(
                        limits=httpx.Limits(
                            max_keepalive_connections=20,
                            max_connections=50,
                            keepalive_expiry=None,
                        )
                    ),
                )

            async def _process_context(self, context):
                try:
                    await super()._process_context(context)
                except Exception as exc:
                    # A rate limit or transient provider failure must not leave
                    # a phone caller in silence. The base LLM service has no
                    # generic error-to-speech hook, so emit one short spoken
                    # fallback inside the normal response frame boundaries.
                    logger.warning(f"Voice LLM failed; using fallback: {exc}")
                    fallback = (
                        "Ji, ek second. Aapki baat samajh rahi hoon, please ek moment."
                        if mode != "english"
                        else "Just a second please, I’m with you."
                    )
                    await self._push_llm_text(fallback)

        model = getattr(settings, "GROQ_VOICE_MODEL", None) or settings.GROQ_MODEL
        voice_settings = VoiceGroqLLMService.Settings(
            model=model,
            temperature=0.35,
            max_tokens=160,
            extra={"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {},
        )
        return VoiceGroqLLMService(
            api_key=settings.GROQ_API_KEY,
            settings=voice_settings,
            retry_on_timeout=False,
        )

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService
        return AnthropicLLMService(
            api_key=settings.ANTHROPIC_API_KEY,
            settings=AnthropicLLMService.Settings(model=settings.ANTHROPIC_MODEL),
        )

    from pipecat.services.openai.llm import OpenAILLMService
    return OpenAILLMService(
        api_key=settings.OPENAI_API_KEY,
        settings=OpenAILLMService.Settings(model=settings.OPENAI_MODEL),
    )

def _build_tts_service(
    provider: str, settings, gender: str, voice_override: Optional[str], language_code: str,
):
    """
    Uses Pipecat's own maintained TTS service classes (not the hand-rolled
    clients in app/services/tts/providers.py — those stay as a non-Pipecat
    fallback/reference; Pipecat's built-ins are tested against real
    accounts and handle reconnect/backoff already).

    Returns (service, native_sample_rate) — the caller sets
    PipelineTask's audio_out_sample_rate to match, and lets
    VobizFrameSerializer's own resampler handle downconverting to Vobiz's
    8kHz wire format, rather than forcing the TTS engine itself to
    synthesize at 8kHz (lower fidelity than its natural rate).
    """
    if provider == "sarvam":
        from pipecat.services.sarvam.tts import SarvamTTSService
        from pipecat.transcriptions.language import Language
        # bulbul:v3 (not v2): Sarvam's own docs position v2 as the "standard"
        # model and v3 as "advanced ... with temperature control" — v2 is
        # the one that tends to sound flat/robotic; v3 is the fix, not a
        # tuning knob on v2. v3 also natively synthesizes at 24kHz (v2: only
        # 22050) — matched below instead of forcing 8kHz at the source.
        default_voice = ("priya" if gender == "female" else "rahul")
        return (
            SarvamTTSService(
                api_key=settings.SARVAM_API_KEY,
                sample_rate=24000,  # bulbul:v3's native rate — see docstring above
                settings=SarvamTTSService.Settings(
                    voice=voice_override or default_voice,
                    model="bulbul:v3",
                    language=Language.HI,
                    temperature=0.7,  # v3-only: more natural/expressive than v2's flat default; try 0.5-0.9 to taste
                ),
            ),
            24000,
        )
    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService
        default_voice = "aura-2-luna-en" if gender == "female" else "aura-2-orion-en"
        return (
            DeepgramTTSService(
                api_key=settings.DEEPGRAM_API_KEY,
                voice=voice_override or default_voice,
                sample_rate=8000,
            ),
            8000,
        )
    raise ValueError(
        f"'{provider}' has no Pipecat streaming path — this function should only "
        f"be called after resolve_tts_provider() has already ruled out 'vobiz'"
    )
