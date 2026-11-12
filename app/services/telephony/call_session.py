"""
Call Session Manager — provider-agnostic (Vobiz only now).

Previously lived inside telnyx_service.py. Moved here on Telnyx removal
so vobiz_webhook.py and telephony.py don't need to import a telnyx module
just to get session state. Behavior is unchanged.
"""
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CallSessionManager:
    SESSION_TTL = 7200

    async def create(self, call_control_id: str, company_id: str,
                     lead_id: Optional[str], direction: str,
                     mode: str, call_log_id: str,
                     company_snapshot: Optional[Dict] = None,
                     live_call_uuid: Optional[str] = None,
                     product_focus: Optional[str] = None) -> Dict:
        from app.core.redis_client import redis_client
        session = {
            "call_control_id": call_control_id,
            "company_id":      company_id,
            "lead_id":         lead_id,
            "direction":       direction,
            "mode":            mode,
            "call_log_id":     call_log_id,
            "history":         [],
            "started_at":      datetime.utcnow().isoformat(),
            "turn_count":      0,
            # Optional — only set by the streaming (vobiz-stream) flow.
            # The id the Live Call tab's session card actually uses, which
            # can differ from call_control_id (Vobiz's CallUUID) when a
            # pre-existing "ringing" card was created at dial time under a
            # different id (request_uuid). /hangup reads this back so its
            # own call_end broadcast lands on the right card too.
            "live_call_uuid":  live_call_uuid,
            "product_focus":   product_focus,
            # Snapshot of the Company row fields the per-turn reply/TTS path
            # needs (name, agent_name, tts config, prompts, products...).
            # Read from here on every turn instead of re-querying Postgres
            # each time — the company config doesn't change mid-call.
            "company_snapshot": company_snapshot,
        }
        await redis_client.set(f"call:{call_control_id}", session, expire=self.SESSION_TTL)
        logger.info(f"Session created | cid={call_control_id[:12]} | company={company_id} | mode={mode}")
        return session

    async def get(self, call_control_id: str) -> Optional[Dict]:
        from app.core.redis_client import redis_client
        return await redis_client.get(f"call:{call_control_id}")

    async def add_turn(self, call_control_id: str, role: str, content: str):
        from app.core.redis_client import redis_client
        session = await self.get(call_control_id)
        if session:
            session["history"].append({"role": role, "content": content})
            session["turn_count"] = session.get("turn_count", 0) + 1
            await redis_client.set(f"call:{call_control_id}", session, expire=self.SESSION_TTL)

    async def end(self, call_control_id: str) -> Optional[Dict]:
        from app.core.redis_client import redis_client
        session = await self.get(call_control_id)
        await redis_client.delete(f"call:{call_control_id}")
        return session

    async def set_live_transcript(self, call_control_id: str, text: str):
        from app.core.redis_client import redis_client
        await redis_client.set(f"transcript:{call_control_id}", text, expire=120)

    async def get_live_transcript(self, call_control_id: str) -> str:
        from app.core.redis_client import redis_client
        return await redis_client.get(f"transcript:{call_control_id}") or ""


session_manager = CallSessionManager()
