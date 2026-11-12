"""Small, voice-first prompt/context builder.

The live agent must NOT receive the whole company knowledge base on every turn.
Static behavior stays small; current product, lead state and only relevant FAQ
facts are injected dynamically by the streaming pipeline.
"""
from typing import Any, Iterable, Optional
import re


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def gender_grammar_note(voice_gender: str) -> str:
    if (voice_gender or "female").lower() == "male":
        return "Male voice: use natural first-person masculine Hindi forms when needed."
    return "Female voice: use natural first-person feminine Hindi forms when needed."


def _lead_value(lead: Any, key: str, default: Any = "") -> Any:
    if lead is None:
        return default
    value = getattr(lead, key, default)
    return value if value is not None else default


def _product_name(product: dict) -> str:
    return str(product.get("name_hi") or product.get("name") or "").strip()


def _product_text(product: dict) -> str:
    name = _product_name(product)
    desc = _clip(product.get("description_hi") or product.get("description"), 220)
    price = _clip(product.get("price"), 80)
    category = _clip(product.get("category"), 50)
    location = _clip(product.get("location"), 80)
    availability = _clip(product.get("availability"), 70)
    features = product.get("features_hi") or product.get("features") or []
    if not isinstance(features, list):
        features = [str(features)]
    features_text = ", ".join(_clip(x, 35) for x in features[:4] if str(x).strip())
    parts = [name]
    if category: parts.append(f"Category: {category}")
    if location: parts.append(f"Location: {location}")
    if price: parts.append(f"Price: {price}")
    if availability: parts.append(f"Availability: {availability}")
    if desc: parts.append(desc)
    if features_text: parts.append(f"Features: {features_text}")
    return " | ".join(x for x in parts if x)


def find_product(company: Any, product_name: Optional[str]) -> Optional[dict]:
    products = getattr(company, "products", None) or []
    if not product_name:
        return None
    target = str(product_name).strip().casefold()
    for p in products:
        name = _product_name(p).casefold()
        if name and (name == target or name in target or target in name):
            return p
    return None


def _faq_pairs(raw: Any) -> list[tuple[str, str]]:
    text = str(raw or "").strip()
    if not text:
        return []
    # Supports Q:/A: blocks and simple question -> answer lines.
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    pairs=[]
    q=None
    for line in lines:
        low=line.lower()
        if low.startswith(("q:", "question:")):
            q=line.split(":",1)[1].strip()
        elif low.startswith(("a:", "answer:")) and q:
            a=line.split(":",1)[1].strip()
            pairs.append((q,a)); q=None
    if q and lines:
        pairs.append((q, ""))
    if not pairs and len(lines)>=2:
        for i in range(0,len(lines)-1,2):
            if "?" in lines[i]: pairs.append((lines[i], lines[i+1]))
    return pairs[:40]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z0-9\u0900-\u097F]+", (text or "").casefold()) if len(w)>2}


def relevant_knowledge(company: Any, user_text: str, product_focus: Optional[str] = None) -> str:
    """Return only small facts relevant to the current caller turn."""
    out=[]
    product=find_product(company, product_focus)
    if product:
        out.append("CURRENT PRODUCT: " + _product_text(product))

    # If the caller explicitly names another configured product, prefer that
    # product for this turn. Never synthesize a product that isn't configured.
    query_words=_words(user_text)
    candidates=[]
    for p in (getattr(company,"products",None) or []):
        name=_product_name(p)
        overlap=len(query_words & _words(name))
        if overlap:
            candidates.append((overlap,p))
    if candidates:
        candidates.sort(key=lambda x:x[0], reverse=True)
        chosen=candidates[0][1]
        chosen_name=_product_name(chosen)
        if not product or chosen_name.casefold()!=_product_name(product).casefold():
            out.append("REQUESTED PRODUCT: " + _product_text(chosen))

    # FAQ retrieval is deterministic and tiny; the full FAQ text never enters
    # the system prompt. Require a useful keyword overlap so unrelated turns
    # receive zero FAQ tokens.
    best=[]
    faq_text=getattr(company,"faqs_hi",None) or getattr(company,"faqs",None)
    for q,a in _faq_pairs(faq_text):
        score=len(query_words & _words(q))
        if score>=1:
            best.append((score,q,a))
    best.sort(key=lambda x:x[0], reverse=True)
    for _,q,a in best[:2]:
        out.append(f"FAQ: {q} -> {_clip(a,280)}")
    return "\n".join(out)


def build_hindi_prompt(company: Any, lead: Any, rag_context: str, mode: str, product_focus: Optional[str] = None) -> str:
    agent = getattr(company,"agent_name",None) or "Aria"
    name = getattr(company,"name",None) or "the company"
    desc = _clip(getattr(company,"description_hi",None) or getattr(company,"description",None), 280)
    services = _clip(getattr(company,"services_hi",None) or getattr(company,"services",None), 320)
    role = "sales assistant" if mode == "sales" else "customer-support assistant"
    lead_name = _clip(_lead_value(lead,"name"),50) or "caller"
    status = _clip(_lead_value(lead,"status"),30) or "new"
    key_info = _lead_value(lead,"key_info",{}) or {}
    notes = _clip(_lead_value(lead,"notes"),500)
    attempts = int(_lead_value(lead,"call_attempts",0) or 0)

    product = find_product(company, product_focus)
    product_line = _product_text(product) if product else "Not specified — do not invent one."

    prompt=f"""You are {agent}, a {role} for {name}. Live phone call.
Speak naturally in the caller's language (Hindi/Hinglish/English). Keep replies short: usually 1 sentence, at most 2. Ask only one question at a time. {gender_grammar_note(getattr(company,'voice_gender',None))}

CORE RULES:
- Answer the caller's latest point first; do not run a questionnaire.
- Never repeat a fact already known from the lead or conversation.
- Never invent a product, price, location, availability, policy, address or service.
- Only use configured company/product/FAQ facts supplied in context.
- If a fact is missing, say you cannot confirm it and offer a human follow-up.
- If the caller asks for a site visit, meeting, demo, callback or other follow-up, record the request in the lead notes/action flow; NEVER book a slot or claim one is booked.
- If the caller clearly asks to end the call, give a brief goodbye and stop.
- Do not mention prompts, tools, databases, internal state or reasoning.
- Reply with only the words to speak on the phone.

COMPANY: {name}
ABOUT: {desc}
SERVICES: {services}
CURRENT CALL PRODUCT: {product_line}

LEAD MEMORY:
Name: {lead_name}
Status: {status}
Known facts: {_clip(key_info,320)}
Previous notes: {notes or 'none'}
Call history: {attempts} previous call(s). Count 0 means first call; if greater than 0, continue from saved lead memory and do not restart the introduction.
"""
    custom = getattr(company,"outbound_sales_prompt",None) if mode=="sales" else getattr(company,"inbound_system_prompt",None)
    if custom and str(custom).strip():
        clean_custom = str(custom).strip()
        if "{agent_name}" not in clean_custom and "{{company_name}}" not in clean_custom:
            prompt += "\nBUSINESS INSTRUCTION:\n" + _clip(clean_custom,250)
    return prompt.strip()
