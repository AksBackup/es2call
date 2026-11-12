"""
Deepgram STT Service — keep-alive architecture (no reconnect per turn)

FIX: en-IN was using language=hi,en (comma) which is invalid in WebSocket URL
params and causes HTTP 400. Changed to single language=hi which Deepgram nova-2
handles for Hinglish (Hindi+English mixed speech) correctly.
"""
import asyncio
import json
import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramSTT:
    """One persistent Deepgram WebSocket connection per active call."""

    def __init__(self, api_key: str, language: str = "en-US"):
        self.api_key  = api_key
        self.language = language
        self._ws      = None
        self._running = False
        self._callback: Optional[Callable] = None

        self._utterance_buffer: list = []
        self._silence_timer: Optional[asyncio.TimerHandle] = None
        self._muted: bool = False

    async def connect(self, on_transcript: Callable[[str, bool], None]):
        import websockets

        self._callback = on_transcript
        self._utterance_buffer = []

        # Language / model selection
        # ─────────────────────────
        # en-IN / hi  (Hinglish — Hindi + Indian English mixed):
        #   model    = nova-2        (nova-2-phonecall is English-only)
        #   language = hi            Single code only — comma-separated codes like
        #                            "hi,en" are NOT valid WS query params and cause
        #                            HTTP 400. nova-2 + hi handles Hinglish well.
        #
        # All others: nova-2-phonecall with their specific language code.
        if self.language in ("en-IN", "hi"):
            model    = "nova-2"
            lang_str = "hi"
        else:
            model    = "nova-2-phonecall"
            lang_str = self.language

        url = (
            f"{DEEPGRAM_WS_URL}"
            f"?model={model}"
            f"&language={lang_str}"
            f"&encoding=mulaw"
            f"&sample_rate=8000"
            f"&channels=1"
            f"&punctuate=true"
            f"&smart_format=true"
            f"&endpointing=700"
            f"&interim_results=true"
            f"&utterance_end_ms=1200"
            f"&vad_events=true"
            f"&no_delay=true"
        )

        self._ws = await websockets.connect(
            url,
            extra_headers={"Authorization": f"Token {self.api_key}"},
            ping_interval=20,
            ping_timeout=15,
        )
        self._running = True
        asyncio.create_task(self._recv_loop())
        asyncio.create_task(self._keepalive_loop())
        logger.info(f"Deepgram connected | lang={self.language} | model={model} | lang_str={lang_str}")

    def mute(self):
        self._muted = True
        self._cancel_silence_timer()
        self._utterance_buffer = []
        logger.debug("Deepgram muted — AI speaking")

    def unmute(self):
        self._muted = False
        self._utterance_buffer = []
        logger.debug("Deepgram unmuted — listening for caller")

    def _cancel_silence_timer(self):
        if self._silence_timer:
            self._silence_timer.cancel()
            self._silence_timer = None

    async def _fire_utterance(self):
        self._cancel_silence_timer()
        if not self._utterance_buffer or self._muted:
            self._utterance_buffer = []
            return
        full = " ".join(self._utterance_buffer).strip()
        self._utterance_buffer = []
        if full and self._callback:
            logger.info(f"Deepgram utterance fired: '{full[:120]}'")
            await self._callback(full, True)

    async def send_audio(self, audio_bytes: bytes):
        if self._ws and self._running and audio_bytes:
            try:
                await self._ws.send(audio_bytes)
            except Exception as e:
                logger.debug(f"Deepgram send: {e}")

    async def _recv_loop(self):
        try:
            async for msg in self._ws:
                if not self._running:
                    break
                try:
                    data  = json.loads(msg)
                    mtype = data.get("type", "")

                    if mtype == "Results":
                        alts     = data.get("channel", {}).get("alternatives", [{}])
                        text     = alts[0].get("transcript", "").strip()
                        is_final = data.get("is_final", False)
                        speech   = data.get("speech_final", False)

                        if text and not self._muted:
                            logger.info(
                                f"Deepgram result: '{text[:80]}' | "
                                f"is_final={is_final} | speech_final={speech}"
                            )

                        if self._muted:
                            continue

                        if not is_final and text and self._silence_timer:
                            self._cancel_silence_timer()

                        if is_final and text:
                            self._utterance_buffer.append(text)
                            self._cancel_silence_timer()
                            if speech:
                                await self._fire_utterance()
                            else:
                                loop = asyncio.get_event_loop()
                                self._silence_timer = loop.call_later(
                                    1.5, lambda: asyncio.create_task(self._fire_utterance())
                                )
                        elif speech and self._utterance_buffer:
                            await self._fire_utterance()

                    elif mtype == "UtteranceEnd":
                        if not self._muted and self._utterance_buffer:
                            logger.info("Deepgram UtteranceEnd safety flush")
                            await self._fire_utterance()

                    elif mtype == "SpeechStarted":
                        if not self._muted:
                            logger.info("Deepgram SpeechStarted — caller speaking")

                    elif mtype == "Error":
                        logger.error(f"Deepgram error: {data}")

                except Exception as e:
                    logger.debug(f"Deepgram recv parse error: {e}")

        except Exception as e:
            if self._running:
                logger.warning(f"Deepgram recv loop ended: {e}")
                await self._reconnect()

    async def _reconnect(self):
        if not self._running:
            return
        logger.info("Deepgram attempting reconnect...")
        await asyncio.sleep(0.5)
        try:
            cb = self._callback
            await self.connect(cb)
            logger.info("Deepgram reconnected successfully")
        except Exception as e:
            logger.error(f"Deepgram reconnect failed: {e}")

    async def _keepalive_loop(self):
        while self._running and self._ws:
            try:
                await asyncio.sleep(8)
                if self._running and self._ws:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
                    logger.debug("Deepgram KeepAlive sent")
            except Exception:
                break

    async def close(self):
        self._running = False
        self._cancel_silence_timer()
        self._utterance_buffer = []
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await asyncio.sleep(0.1)
                await self._ws.close()
            except Exception:
                pass
        self._ws = None


class DeepgramManager:
    """Manages Deepgram sessions keyed by call_control_id."""

    def __init__(self):
        self._sessions: Dict[str, DeepgramSTT] = {}

    def _api_key(self) -> str:
        from app.core.config import settings
        return settings.DEEPGRAM_API_KEY or ""

    def _language(self, company=None) -> str:
        if not company:
            return "en-US"
        lang = getattr(company, "voice_language", "en-US") or "en-US"
        if isinstance(company, dict):
            lang = company.get("voice_language", "en-US") or "en-US"
        return {
            "en-US": "en-US",
            "en-IN": "en-IN",
            "en-GB": "en-GB",
            "hi-IN": "hi",
        }.get(lang, "en-US")

    async def start(self, cid: str, on_transcript: Callable, company=None) -> bool:
        key = self._api_key()
        if not key:
            logger.error("DEEPGRAM_API_KEY not configured")
            return False
        stt = DeepgramSTT(api_key=key, language=self._language(company))
        try:
            await stt.connect(on_transcript)
            self._sessions[cid] = stt
            logger.info(f"Deepgram session started | cid={cid[:12]}")
            return True
        except Exception as e:
            logger.error(f"Deepgram start failed: {e}")
            return False

    async def audio(self, cid: str, raw_bytes: bytes):
        stt = self._sessions.get(cid)
        if stt:
            await stt.send_audio(raw_bytes)

    def mute(self, cid: str):
        stt = self._sessions.get(cid)
        if stt:
            stt.mute()

    def unmute(self, cid: str):
        stt = self._sessions.get(cid)
        if stt:
            stt.unmute()

    async def stop(self, cid: str):
        stt = self._sessions.pop(cid, None)
        if stt:
            await stt.close()
            logger.info(f"Deepgram session stopped | cid={cid[:12]}")

    async def restart(self, cid: str, on_transcript: Callable, company=None) -> bool:
        stt = self._sessions.get(cid)
        if stt:
            stt.unmute()
            logger.info(f"Deepgram unmuted (no reconnect needed) | cid={cid[:12]}")
            return True
        return await self.start(cid, on_transcript, company)

    def active(self, cid: str) -> bool:
        return cid in self._sessions


deepgram_manager = DeepgramManager()