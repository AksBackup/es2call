"""
Custom Pipecat FrameSerializer for Vobiz's bidirectional WebSocket media
stream — see docs.vobiz.ai/concepts/streaming-websockets.

IMPORTANT: Pipecat ships built-in serializers for Twilio/Telnyx/Plivo/
Exotel/Vonage/Genesys but NOT Vobiz (checked pipecat's actual serializer
list — an earlier note in this codebase's history assumed one existed;
it doesn't as of writing). This is a small hand-written one, modeled on:
  - Vobiz's own documented event schema (connected/start/media/dtmf/stop,
    inbound audio as base64 G.711 mulaw in 20ms/160-byte frames)
  - Pipecat's own ExotelFrameSerializer/TwilioFrameSerializer, which
    handle a near-identical protocol shape, as a structural reference

VERIFY BEFORE PRODUCTION: Vobiz's public docs describe the four event
*types* precisely but don't publish the exact JSON key names inside
"start"/"media" (e.g. whether the stream identifier field is
"StreamUUID", "stream_id", "CallUUID", etc). This class defensively
checks several likely key spellings and — critically — logs the FULL
raw "start" payload the first time it's seen, so you can read your
actual logs after one real test call and confirm/correct the key
lookups below. Same class of issue that caused the original Gather
integration to silently misread field names; don't skip that log check.
"""
import audioop  # stdlib; deprecated in 3.13+ — see note at bottom if on 3.13+
import base64
import json
import logging
from typing import Optional

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    StartFrame,
    StartInterruptionFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType

logger = logging.getLogger(__name__)


class VobizFrameSerializer(FrameSerializer):
    """
    Converts between Pipecat frames and Vobiz's WebSocket media-stream
    JSON protocol. Keeps audio at 8kHz mulaw <-> 8kHz PCM16 only — no
    resampling — since the whole point of this path is to avoid the
    latency/quality cost of converting sample rates.
    """

    def __init__(self, call_uuid: str, params: Optional[dict] = None):
        self._call_uuid = call_uuid
        self._stream_uuid: Optional[str] = None  # learned from the "start" event
        self._logged_start_payload = False

    @property
    def type(self) -> FrameSerializerType:
        return FrameSerializerType.TEXT

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame) -> Optional[str]:
        """Pipecat frame -> outbound Vobiz WS message (audio going TO the caller)."""
        if isinstance(frame, StartInterruptionFrame):
            # Barge-in: tell Vobiz to stop/clear whatever audio it has
            # buffered. "clear" is the standard name across Twilio/Plivo-
            # style protocols for this — VERIFY this is what Vobiz expects;
            # if the caller keeps hearing stale audio after an interruption,
            # this is the first thing to check against a real call log.
            return json.dumps({"event": "clear", "streamId": self._stream_uuid})

        if isinstance(frame, AudioRawFrame):
            # frame.audio is PCM16 @ 8kHz (we requested that sample rate on
            # both the transport and every TTS service so no resample step
            # is needed here — just the codec conversion).
            mulaw_bytes = audioop.lin2ulaw(frame.audio, 2)
            payload = base64.b64encode(mulaw_bytes).decode("ascii")
            return json.dumps({
                "event": "media",
                "streamId": self._stream_uuid,
                "media": {"payload": payload},
            })

        return None

    async def deserialize(self, data) -> Optional[Frame]:
        """Inbound Vobiz WS message -> Pipecat frame (caller's audio, events)."""
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None

        event = msg.get("event") or msg.get("type")

        if event == "start":
            if not self._logged_start_payload:
                # See module docstring — confirm real key names here.
                logger.info(f"Vobiz stream 'start' payload: {msg}")
                self._logged_start_payload = True
            self._stream_uuid = (
                msg.get("streamId") or msg.get("stream_id") or
                msg.get("StreamUUID") or msg.get("streamSid") or
                self._call_uuid
            )
            return None

        if event == "connected":
            return None

        if event == "media":
            media = msg.get("media", {})
            payload_b64 = media.get("payload") or msg.get("payload")
            if not payload_b64:
                return None
            mulaw_bytes = base64.b64decode(payload_b64)
            pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
            return InputAudioRawFrame(audio=pcm_bytes, sample_rate=8000, num_channels=1)

        if event == "dtmf":
            digit = msg.get("dtmf", {}).get("digit") or msg.get("digit")
            if digit:
                return InputDTMFFrame(button=digit)
            return None

        if event == "stop":
            logger.info(f"Vobiz stream stopped | call_uuid={self._call_uuid[:12]}")
            return None

        return None
