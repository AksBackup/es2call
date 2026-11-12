"""
LLM Service — Groq llama-3.3-70b-versatile
"""
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


INBOUND_SUPPORT_PROMPT = """You are {agent_name}, a support agent at {company_name}. Live phone call.

Company: {company_info}
Services: {services}
FAQs: {faqs}
Context: {rag_context}

You speak like a sharp, friendly colleague — warm, direct, natural. React to what they said, then answer.

Examples:

Caller: "My order hasn't arrived yet."
You: "Oh no, let me check that — when did you place it?"

Caller: "I want to know about your pricing."
You: "Yeah sure — are you thinking monthly or the annual plan?"

Caller: "I'm not sure if this is right for me."
You: "That's fair, honestly. What are you mainly trying to solve for?"

Caller: "I'm busy right now, call me later."
You: "Of course, no problem at all. When works better for you?"

Caller: "Tomorrow morning."
You: "Perfect, I'll make a note of that. Talk to you then — take care!"
[HANG UP]

Caller: "Call me next week."
You: "Sure thing, I'll reach out next week. Have a good one!"
[HANG UP]

Rules:
- 1-2 sentences max
- If caller wants callback: confirm time warmly and end the call — do NOT keep talking
- If caller says bye/goodbye/not interested: give a warm short farewell and end
- Match their language — Hindi, Hinglish, English, whatever they use
- Never reveal you're an AI unless directly asked
"""

OUTBOUND_SALES_PROMPT = """You are {agent_name}, a sales executive calling from {company_name}. Live phone call.

Company: {company_info}
What you offer: {product_info}
Lead name: {lead_name}
Call history: {call_attempts} previous calls
What you know about them: {lead_context}

Your approach changes based on call history:

IF this is the FIRST call (call_attempts = 0 or 1):
- Introduce yourself warmly, be curious not pushy
- Goal: understand what they actually need, build rapport
- "Hey {lead_name}, glad I caught you — we help businesses with {product_info}, just wanted to see if it might be relevant for you. You got two minutes?"

IF you've called BEFORE (call_attempts > 1):
- Reference the previous conversation naturally, don't reintroduce yourself
- Pick up where you left off based on what you know
- "Hey, it's {agent_name} again from {company_name} — last time we spoke about [relevant context], just wanted to follow up on that."
- If they said they'd think about it → "Did you get a chance to think about what we discussed?"
- If they were interested → move forward, ask about next steps
- If they were cold → acknowledge it, try a different angle

Conversation style:
Lead: "Yeah I'm here." → "Hey, glad I caught you — [natural opener based on history]"
Lead: "I'm busy." → "No worries — when's a better time? I'll call you then."
Lead: "Call me tomorrow." → "Perfect, I'll call tomorrow. Talk soon!" [HANG UP]
Lead: "Not interested." (first time) → "Totally fair — can I ask what's holding you back?"
Lead: "Not interested." (second time) → "Fair enough, I appreciate your time. Take care!" [HANG UP]
Lead: "Tell me more." → "Sure — what's the main challenge you're dealing with right now?"
Lead: "Already have something." → "Oh nice — what are you using? Just curious."
Lead: "Bye." → "Alright, take care!" [HANG UP]

Rules:
- Max 2 sentences per turn
- One question per turn, then stop talking
- Always reference lead_context if available — show you remember them
- If they ask for callback: confirm and hang up immediately
- Match their language — Hindi, Hinglish, English, whatever they use
- Never reveal you're an AI unless directly asked
- Never give a product dump — one thing at a time
"""

# Callback intent detection — fast, cheap, separate LLM call
CALLBACK_DETECTION_PROMPT = """Analyze this phone conversation message and return ONLY valid JSON.

Message: "{message}"
Conversation history: {history}

Detect if the caller wants to end the call or reschedule.

Return:
{{
  "wants_callback": true/false,
  "wants_to_end": true/false,
  "callback_time_raw": "exact words they used for time, or null",
  "callback_datetime_iso": "ISO 8601 datetime in Asia/Kolkata timezone, or null",
  "confidence": 0.0
}}

Time parsing rules (use current datetime: {now}):
- "tomorrow morning" → next day 09:00
- "tomorrow afternoon" → next day 14:00  
- "tomorrow evening" → next day 18:00
- "tomorrow" (no time) → next day 10:00
- "morning" (today) → today 09:00 if not passed, else tomorrow 09:00
- "afternoon" → today 14:00 if not passed, else tomorrow 14:00
- "evening" → today 18:00 if not passed, else tomorrow 18:00
- "after lunch" → today 14:00 if not passed, else tomorrow 14:00
- "later today" / "later" → +3 hours from now, capped at 18:00; if past 15:00 → tomorrow 10:00
- "next week" → next Monday 10:00
- "Monday"/"Tuesday" etc → next occurrence of that day 10:00
- "3pm" / "3 baje" / "teen baje" → today at 15:00 if not passed, else tomorrow 15:00
- Specific time + day → parse literally
- If time would be outside 09:00-18:00 window → snap to next 09:00
- wants_to_end=true for: "bye", "goodbye", "not interested", "stop calling", "don't call again", "hang up"
- wants_callback=true ONLY when the caller is explicitly asking to END THIS CALL and be phoned back another time — e.g. "call me back tomorrow", "phone me later today", "can we do this another time", "I'm busy right now, try me this evening". The caller must be asking to reschedule the CALL ITSELF.
- wants_callback=false for words like "later"/"after that"/"after this" used WITHIN the conversation to mean "after you explain more" or "after I decide" — e.g. "tell me more, I'll decide after that" is NOT a callback request, it's the caller continuing the current conversation. Do not set wants_callback=true just because a time-word like "later" or "tomorrow" appears somewhere in the message — it must clearly refer to rescheduling this call.
- When in doubt between "continuing this conversation" and "wants a callback", prefer wants_callback=false and a lower confidence score.

Return ONLY JSON, no markdown.
"""

ANALYSIS_PROMPT = """Analyze this sales/support call and return ONLY valid JSON.

Transcript:
{transcript}

Company context: {company_context}

lead_status guide:
- "hot": caller showed clear buying intent — asked for pricing/next steps, agreed to a demo/site visit, said things like "I want this" / "how do I proceed" / "send me the details to book".
- "warm": caller was engaged and asked genuine questions or showed interest, but did NOT signal they're ready to act now.
- "interested": caller listened and responded positively but gave little detail either way.
- "cold": caller was disengaged, gave one-word answers, or seemed uninterested throughout.
- "closed_won"/"closed_lost"/"do_not_call": only when the call reached an explicit, unambiguous outcome.
- "contacted": default when none of the above clearly fit (e.g. call barely started).

{{
  "summary": "2-3 sentences",
  "sentiment": "positive|neutral|negative",
  "intent": "interested|not_interested|wants_callback|objection|complaint|query_resolved|other",
  "lead_status": "new|contacted|interested|warm|hot|cold|closed_won|closed_lost|do_not_call",
  "interest_level": 0.0,
  "callback_requested": false,
  "callback_time_raw": null,
  "key_info": {{"budget": "", "timeline": "", "pain_points": "", "objections": "", "next_action": ""}},
  "transferred_to_human": false,
  "follow_up_required": false,
  "follow_up_note": ""
}}

Return ONLY JSON.
"""

EMAIL_ANALYSIS_PROMPT = """Analyze this email reply and return ONLY valid JSON.

Original: {original_email}
Reply: {reply_body}
Thread: {thread_context}
Product: {product_info}

{{
  "sentiment": "positive|neutral|negative",
  "intent": "interested|not_interested|asking_question|wants_callback|pricing_query|objection|other",
  "lead_status": "contacted|interested|warm|cold|closed_won|closed_lost",
  "confidence": 0.0,
  "reply_draft": "natural warm reply, same language as lead, max 4 sentences",
  "key_info": {{"budget": "", "timeline": "", "objection": ""}},
  "summary": "one sentence"
}}

Return ONLY JSON.
"""


class LLMService:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # 8s timeout for voice calls — if Groq takes longer something is wrong
            # and it's better to give a fallback reply than make the caller wait
            self._client = httpx.AsyncClient(timeout=8.0)
        return self._client

    async def generate_response(
        self,
        messages: List[Dict],
        system_prompt: str,
        max_tokens: int = 100,
        temperature: float = 0.9,
        response_format: Optional[Dict] = None,
    ) -> str:
        clean = [m for m in messages if m.get("content", "").strip()]
        if len(clean) > 10:
            clean = clean[-10:]

        from app.core.config import settings
        payload = {
            "model": settings.GROQ_MODEL,
            "messages": [{"role": "system", "content": system_prompt}] + clean,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        try:
            client = await self._get_client()
            resp = await client.post(
                GROQ_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            logger.error(f"Groq HTTP {e.response.status_code}: {e.response.text}")
            return "Hmm, give me just a second."
        except Exception as e:
            logger.error(f"LLM error: {e}")
            return "Hmm, give me just a second."

    async def detect_callback_intent(
        self,
        message: str,
        history: List[Dict],
        now_iso: str,
    ) -> Dict:
        """
        Fast separate LLM call to detect callback/end intent and parse time.
        Runs in parallel with the main response generation.
        Returns dict with wants_callback, wants_to_end, callback_datetime_iso etc.
        """
        history_text = " | ".join([
            f"{m['role']}: {m['content'][:60]}" for m in history[-4:]
        ])
        prompt = CALLBACK_DETECTION_PROMPT.format(
            message=message,
            history=history_text,
            now=now_iso,
        )
        try:
            result = await self.generate_response(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are a JSON-only intent classifier. Return only valid JSON.",
                max_tokens=200,
                temperature=0.1,
            )
            clean = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(clean)
        except Exception as e:
            logger.debug(f"Callback detection error: {e}")
            return {
                "wants_callback": False,
                "wants_to_end": False,
                "callback_time_raw": None,
                "callback_datetime_iso": None,
                "confidence": 0.0,
            }

    async def analyze_call(self, transcript: str, company_context: str, lead_context: str = "") -> Dict:
        """Small factual post-call analysis. Uses the real transcript and never retries."""
        if len(transcript) > 6500:
            compact_transcript = transcript[:1200] + "\n...\n" + transcript[-5300:]
        else:
            compact_transcript = transcript
        prompt = f"""Analyze ONLY this completed call transcript. Do not use a sales script as evidence.
Company/product context: {company_context[:300]}
Lead before call: {lead_context[:500]}
REAL TRANSCRIPT:
{compact_transcript}

Return ONE JSON object only.
summary: one short factual sentence, maximum 30 words.
sentiment: positive|neutral|negative
intent: interested|not_interested|wants_callback|site_visit|meeting|demo|objection|complaint|query_resolved|other
lead_status: new|contacted|interested|warm|hot|cold|closed_won|closed_lost|do_not_call
interest_level: 0..1
key_info: only facts explicitly stated; fields budget,timeline,location,product,property_type,pain_points,objections,next_action
follow_up_required: true only when the caller requested a real follow-up
follow_up_note: one short sentence, maximum 20 words
Never invent a product, property, price, address, date, time or customer fact. Never copy prompt rules into the summary."""
        try:
            result = await self.generate_response(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="Return one valid JSON object only. No markdown or explanation.",
                max_tokens=220,
                temperature=0.0,
            )
            clean = result.strip()
            if clean.startswith("```"):
                clean = clean.replace("```json", "", 1).replace("```", "", 1).strip()
            return json.loads(clean)
        except Exception as e:
            logger.error(f"Call analysis failed; using deterministic fallback: {e}")
            low = transcript.casefold()
            status = "contacted"
            if any(x in low for x in ["not interested", "no interest", "nahi chahiye", "nahi interested"]):
                status = "cold"
            elif any(x in low for x in ["site visit", "site-visit", "visit karna", "property dekhna"]):
                status = "hot"
            elif any(x in low for x in ["interested", "acha laga", "pasand hai", "send details"]):
                status = "interested"
            wants_visit = any(x in low for x in ["site visit", "site-visit", "visit karna", "property dekhna"])
            wants_callback = any(x in low for x in ["callback", "call me", "phone me", "baad mein call"])
            summary = "Call completed; review the transcript for the customer's stated requirement."
            if wants_visit:
                summary = "Customer requested a site visit; admin follow-up is required to confirm it manually."
            elif wants_callback:
                summary = "Customer requested a follow-up call; a human should confirm the callback timing."
            return {
                "summary": summary,
                "sentiment": "neutral",
                "intent": "site_visit" if wants_visit else ("wants_callback" if wants_callback else "other"),
                "lead_status": status,
                "interest_level": 0.7 if status == "hot" else (0.5 if status == "interested" else 0.3),
                "key_info": {},
                "follow_up_required": wants_visit or wants_callback,
                "follow_up_note": "Admin should confirm the requested site visit manually." if wants_visit else ("Human team should confirm the callback." if wants_callback else ""),
            }

    async def analyze_email_reply(
        self, original_email: str, reply_body: str,
        thread_context: str, product_info: str,
    ) -> Dict:
        prompt = EMAIL_ANALYSIS_PROMPT.format(
            original_email=original_email, reply_body=reply_body,
            thread_context=thread_context, product_info=product_info,
        )
        result = await self.generate_response(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a JSON-only assistant. Return only valid JSON.",
            max_tokens=700, temperature=0.2,
        )
        try:
            clean = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(clean)
        except Exception as e:
            logger.error(f"Email analysis parse error: {e}")
            return {
                "sentiment": "neutral", "intent": "other",
                "lead_status": "contacted", "confidence": 0.3,
                "reply_draft": "Thank you for your response. We will get back to you shortly.",
                "key_info": {}, "summary": "Lead replied",
            }

    def build_inbound_prompt(self, company: Any, rag_context: str = "") -> str:
        if company.inbound_system_prompt:
            return company.inbound_system_prompt
        return INBOUND_SUPPORT_PROMPT.format(
            agent_name=company.agent_name or "Aria",
            company_name=company.name,
            company_info=company.description or "",
            services=company.services or "",
            faqs=company.faqs or "",
            rag_context=rag_context or "No specific context available.",
        )

    def build_outbound_prompt(self, company: Any, lead: Any, rag_context: str = "") -> str:
        if company.outbound_sales_prompt:
            return company.outbound_sales_prompt

        products = company.products or []
        active = company.active_product
        product_list = [p for p in products if p.get("name", "").lower() == active.lower()] if (products and active) else products
        if product_list:
            product_info = "\n".join([
                f"- {p.get('name')}: {p.get('description', '')} | Price: {p.get('price', 'contact us')} | Features: {', '.join(p.get('features', []))}"
                for p in product_list
            ])
        else:
            product_info = company.services or "Our products and services"

        lead_name = lead.name.split()[0] if lead and lead.name else "there"
        lead_ctx = ""
        if lead:
            parts = []
            if lead.notes:
                parts.append(lead.notes)
            if lead.key_info:
                for k, v in lead.key_info.items():
                    if v:
                        parts.append(f"{k}: {v}")
            if lead.call_attempts and lead.call_attempts > 0:
                parts.append(f"Called {lead.call_attempts} times before")
            lead_ctx = " | ".join(parts) if parts else "First contact"

        call_attempts = 0
        if lead:
            ca = getattr(lead, 'call_attempts', None)
            if ca is None and isinstance(lead, dict):
                ca = lead.get('call_attempts', 0)
            call_attempts = ca or 0

        return OUTBOUND_SALES_PROMPT.format(
            agent_name=company.agent_name or "Aria",
            company_name=company.name,
            company_info=company.description or "",
            product_info=product_info,
            lead_name=lead_name,
            call_attempts=call_attempts,
            lead_context=lead_ctx,
        )

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


llm_service = LLMService()