"""
Email Service
- Send via SendGrid (custom domain)
- Receive/poll replies via IMAP (stdlib imaplib — no extra package)
"""
import asyncio
import email
import imaplib
import logging
import re
from datetime import datetime
from email.header import decode_header
from typing import Any, Dict, List, Optional, Tuple

import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailService:

    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url="https://api.sendgrid.com/v3",
                headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
                timeout=30.0,
            )
        return self._http

    # ── Send ──────────────────────────────────────────────────────────────

    async def send(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        email_log_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Send via SendGrid. Returns (success, message_id)."""
        if not settings.SENDGRID_API_KEY:
            logger.warning("SendGrid not configured — email not sent")
            return False, None

        payload: Dict[str, Any] = {
            "personalizations": [{"to": [{"email": to_email, "name": to_name}], "subject": subject}],
            "from": {"email": from_email or settings.EMAIL_FROM_ADDRESS, "name": from_name or settings.EMAIL_FROM_NAME},
            "reply_to": {"email": reply_to or settings.EMAIL_REPLY_TO or from_email or settings.EMAIL_FROM_ADDRESS},
            "content": [{"type": "text/plain", "value": body_text}],
            "tracking_settings": {"click_tracking": {"enable": True}, "open_tracking": {"enable": True}},
            "headers": {"X-Email-Log-ID": email_log_id or ""},
        }
        if body_html:
            payload["content"].append({"type": "text/html", "value": body_html})

        try:
            client = await self._client()
            resp = await client.post("/mail/send", json=payload)
            resp.raise_for_status()
            msg_id = resp.headers.get("X-Message-Id")
            logger.info(f"Email sent to {to_email} | msg_id={msg_id}")
            return True, msg_id
        except httpx.HTTPStatusError as e:
            logger.error(f"SendGrid error {e.response.status_code}: {e.response.text}")
            return False, None
        except Exception as e:
            logger.error(f"Email send error: {e}")
            return False, None

    # ── IMAP Poll ─────────────────────────────────────────────────────────

    async def poll_replies(self) -> List[Dict]:
        """Poll IMAP inbox for unseen replies. Returns list of parsed reply dicts."""
        return await asyncio.get_event_loop().run_in_executor(None, self._sync_poll)

    def _sync_poll(self) -> List[Dict]:
        if not settings.IMAP_USERNAME or not settings.IMAP_PASSWORD:
            logger.warning("IMAP not configured — skipping poll")
            return []

        replies = []
        try:
            imap = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
            imap.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
            imap.select(settings.IMAP_MAILBOX)

            _, nums = imap.search(None, "UNSEEN")
            if not nums or not nums[0]:
                imap.logout()
                return []

            for num in nums[0].split():
                try:
                    _, data = imap.fetch(num, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])

                    from_raw  = msg.get("From", "")
                    subject   = self._decode_header(msg.get("Subject", ""))
                    log_id    = msg.get("X-Email-Log-ID", "")
                    date_str  = msg.get("Date", "")
                    body      = self._extract_body(msg)
                    clean     = self._strip_quoted(body)

                    sender_match = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]+", from_raw)
                    sender_email = sender_match.group(0) if sender_match else from_raw

                    if clean:
                        replies.append({
                            "imap_uid":    num.decode(),
                            "from_email":  sender_email,
                            "subject":     subject,
                            "body":        clean,
                            "email_log_id": log_id,
                            "received_at": date_str,
                        })

                    imap.store(num, "+FLAGS", "\\Seen")
                except Exception as e:
                    logger.error(f"IMAP parse error msg {num}: {e}")

            imap.logout()
        except Exception as e:
            logger.error(f"IMAP poll error: {e}")

        return replies

    def _decode_header(self, value: str) -> str:
        parts = decode_header(value)
        out = []
        for part, enc in parts:
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(part))
        return " ".join(out)

    def _extract_body(self, msg: email.message.Message) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    except Exception:
                        pass
        else:
            try:
                return msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", errors="replace"
                )
            except Exception:
                pass
        return ""

    def _strip_quoted(self, body: str) -> str:
        """Remove quoted reply sections (lines starting with > or On...wrote: patterns)."""
        lines, clean = body.splitlines(), []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(">") or re.match(r"^On .+ wrote:$", stripped):
                break
            clean.append(line)
        return "\n".join(clean).strip()

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()


email_service = EmailService()
