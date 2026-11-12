# from datetime import datetime
# from typing import List, Optional
# from fastapi import APIRouter, Depends, HTTPException
# from pydantic import BaseModel
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.core.database import get_db
# from app.core.security import get_current_active_user
# from app.models.models import Company

# router = APIRouter()


# class ProductItem(BaseModel):
#     name: str
#     description: str
#     price: str = "Contact us"
#     features: List[str] = []


# class CompanyCreate(BaseModel):
#     name: str
#     industry: Optional[str] = None
#     description: Optional[str] = None
#     website: Optional[str] = None
#     location: Optional[str] = None
#     services: Optional[str] = None
#     faqs: Optional[str] = None
#     products: Optional[List[ProductItem]] = None
#     active_product: Optional[str] = None
#     agent_name: str = "Aria"
#     voice_language: str = "en-IN"
#     voice_gender: str = "female"
#     tts_provider: str = "telnyx"
#     inbound_system_prompt: Optional[str] = None
#     outbound_sales_prompt: Optional[str] = None
#     greeting_inbound: Optional[str] = None
#     greeting_outbound: Optional[str] = None
#     forward_number: Optional[str] = None
#     telnyx_phone_number: Optional[str] = None
#     email_from_address: Optional[str] = None
#     email_from_name: Optional[str] = None
#     email_reply_to: Optional[str] = None
#     email_signature: Optional[str] = None


# class CompanyUpdate(CompanyCreate):
#     name: Optional[str] = None


# def _dict(c: Company) -> dict:
#     return {
#         "id": c.id,
#         "name": c.name,
#         "industry": c.industry,
#         "description": c.description,
#         "website": c.website,
#         "location": c.location,
#         "services": c.services,
#         "faqs": c.faqs,
#         "products": c.products,
#         "active_product": c.active_product,
#         "agent_name": c.agent_name,
#         "voice_language": c.voice_language,
#         "voice_gender": c.voice_gender,
#         "tts_provider": c.tts_provider,
#         "inbound_system_prompt": c.inbound_system_prompt,
#         "outbound_sales_prompt": c.outbound_sales_prompt,
#         "greeting_inbound": c.greeting_inbound,
#         "greeting_outbound": c.greeting_outbound,
#         "forward_number": c.forward_number,
#         "telnyx_phone_number": c.telnyx_phone_number,
#         "email_from_address": c.email_from_address,
#         "email_from_name": c.email_from_name,
#         "email_reply_to": c.email_reply_to,
#         "email_signature": c.email_signature,
#         "created_at": c.created_at,
#         "updated_at": c.updated_at,
#     }


# @router.get("/")
# async def get_company(
#     current_user=Depends(get_current_active_user),
#     db: AsyncSession = Depends(get_db),
# ):
#     r = await db.execute(select(Company).where(Company.owner_id == current_user.id))
#     company = r.scalar_one_or_none()
#     if not company:
#         return None
#     return _dict(company)


# @router.post("/")
# async def create_company(
#     data: CompanyCreate,
#     current_user=Depends(get_current_active_user),
#     db: AsyncSession = Depends(get_db),
# ):
#     r = await db.execute(select(Company).where(Company.owner_id == current_user.id))
#     if r.scalar_one_or_none():
#         raise HTTPException(400, "Company already exists. Use PATCH to update.")

#     products = [p.model_dump() for p in data.products] if data.products else []
#     company = Company(
#         owner_id=current_user.id,
#         **{k: v for k, v in data.model_dump(exclude={"products"}).items()},
#         products=products,
#     )
#     db.add(company)
#     await db.commit()
#     await db.refresh(company)
#     return _dict(company)


# @router.patch("/")
# async def update_company(
#     data: CompanyUpdate,
#     current_user=Depends(get_current_active_user),
#     db: AsyncSession = Depends(get_db),
# ):
#     r = await db.execute(select(Company).where(Company.owner_id == current_user.id))
#     company = r.scalar_one_or_none()
#     if not company:
#         raise HTTPException(404, "Company not found")

#     update_data = data.model_dump(exclude_none=True, exclude={"products"})
#     for k, v in update_data.items():
#         setattr(company, k, v)
#     if data.products is not None:
#         company.products = [p.model_dump() for p in data.products]
#     company.updated_at = datetime.utcnow()
#     await db.commit()
#     return _dict(company)



from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.models import Company

router = APIRouter()
logger = logging.getLogger(__name__)


class ProductItem(BaseModel):
    name: str
    description: str
    price: str = "Contact us"          # shared across languages — digits/currency don't need translation
    features: List[str] = []
    name_hi: Optional[str] = None        # Vobiz/Hindi-Hinglish override — falls back to `name` if blank
    description_hi: Optional[str] = None
    features_hi: Optional[List[str]] = None
    category: Optional[str] = None
    location: Optional[str] = None
    availability: Optional[str] = None
    target_audience: Optional[str] = None
    tags: Optional[List[str]] = None


class CompanyCreate(BaseModel):
    name: str
    industry: Optional[str] = None
    description: Optional[str] = None
    description_hi: Optional[str] = None
    website: Optional[str] = None
    location: Optional[str] = None
    services: Optional[str] = None
    services_hi: Optional[str] = None
    faqs: Optional[str] = None
    faqs_hi: Optional[str] = None
    business_knowledge: Optional[dict] = None
    products: Optional[List[ProductItem]] = None
    active_product: Optional[str] = None
    agent_name: str = "Aria"
    voice_language: str = "hi-IN"
    voice_gender: str = "female"
    tts_provider: str = "sarvam"# "vobiz"
    inbound_system_prompt: Optional[str] = None
    outbound_sales_prompt: Optional[str] = None
    greeting_inbound: Optional[str] = None
    greeting_outbound: Optional[str] = None
    greeting_inbound_hi: Optional[str] = None
    greeting_outbound_hi: Optional[str] = None
    forward_number: Optional[str] = None
    vobiz_auth_id: Optional[str] = None
    vobiz_auth_token: Optional[str] = None
    vobiz_phone_number: Optional[str] = None
    telnyx_api_key: Optional[str] = None
    telnyx_connection_id: Optional[str] = None
    telnyx_phone_number: Optional[str] = None
    telnyx_public_key: Optional[str] = None
    email_from_address: Optional[str] = None
    email_from_name: Optional[str] = None
    email_reply_to: Optional[str] = None
    email_signature: Optional[str] = None


class CompanyUpdate(CompanyCreate):
    """
    PATCH schema — every field here MUST be Optional[...] = None, not a
    concrete default inherited from CompanyCreate. update_company() below
    uses model_dump(exclude_none=True) to apply only the fields the client
    actually sent; a field with a non-None default (like CompanyCreate's
    agent_name="Aria" or voice_gender="female") is NEVER excluded by that
    call, so it gets written back to the DB on every single PATCH — even
    ones that had nothing to do with that field. That silently reset
    voice_gender back to "female" and tts_provider back to "sarvam" on
    every unrelated settings save, which is exactly the "my change didn't
    stick" behavior seen in testing. If you add new fields to
    CompanyCreate in the future with a concrete default, override them
    here too — don't let this class fall back to inheriting silently.
    """
    name: Optional[str] = None
    agent_name: Optional[str] = None
    voice_language: Optional[str] = None
    voice_gender: Optional[str] = None
    tts_provider: Optional[str] = None


def _dict(c: Company) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "industry": c.industry,
        "description": c.description,
        "description_hi": c.description_hi,
        "website": c.website,
        "location": c.location,
        "services": c.services,
        "services_hi": c.services_hi,
        "faqs": c.faqs,
        "faqs_hi": c.faqs_hi,
        "business_knowledge": c.business_knowledge or {},
        "products": c.products,
        "active_product": c.active_product,
        "agent_name": c.agent_name,
        "voice_language": c.voice_language,
        "voice_gender": c.voice_gender,
        "tts_provider": c.tts_provider,
        "inbound_system_prompt": c.inbound_system_prompt,
        "outbound_sales_prompt": c.outbound_sales_prompt,
        "greeting_inbound": c.greeting_inbound,
        "greeting_outbound": c.greeting_outbound,
        "greeting_inbound_hi": c.greeting_inbound_hi,
        "greeting_outbound_hi": c.greeting_outbound_hi,
        "forward_number": c.forward_number,
        "vobiz_auth_id": c.vobiz_auth_id,
        # Never return the raw token — this used to send the real secret
        # straight back to the browser on every GET/POST/PATCH response,
        # which defeats the point of encrypting it at rest (anything that
        # can see the API response — dev tools, a proxy, a stray
        # console.log — saw the real value). Instead: a masked hint plus
        # whether one is set at all. The frontend's Settings form should
        # treat this field as "type a new value to change it, leave blank
        # to keep the existing one" — the same pattern as a password
        # field — rather than pre-filling it with something editable.
        "vobiz_auth_token_set": bool(c.vobiz_auth_token),
        "vobiz_auth_token_masked": (
            f"••••{c.vobiz_auth_token[-4:]}" if c.vobiz_auth_token and len(c.vobiz_auth_token) >= 4 else None
        ),
        "vobiz_phone_number": c.vobiz_phone_number,
        # Same rule as the Vobiz token: never return the raw API key.
        "telnyx_api_key_set": bool(c.telnyx_api_key),
        "telnyx_api_key_masked": (
            f"••••{c.telnyx_api_key[-4:]}" if c.telnyx_api_key and len(c.telnyx_api_key) >= 4 else None
        ),
        "telnyx_connection_id": c.telnyx_connection_id,
        "telnyx_phone_number": c.telnyx_phone_number,
        "telnyx_public_key": c.telnyx_public_key,
        "plan_type": c.plan_type,
        "plan_minutes_total": c.plan_minutes_total,
        "plan_minutes_used": c.plan_minutes_used,
        "plan_expires_at": c.plan_expires_at,
        "trial_used": c.trial_used,
        "email_from_address": c.email_from_address,
        "email_from_name": c.email_from_name,
        "email_reply_to": c.email_reply_to,
        "email_signature": c.email_signature,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@router.get("/")
async def get_company(
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(Company).where(Company.owner_id == current_user.id))
    company = r.scalar_one_or_none()
    if not company:
        return None
    return _dict(company)


@router.post("/")
async def create_company(
    data: CompanyCreate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(Company).where(Company.owner_id == current_user.id))
    if r.scalar_one_or_none():
        raise HTTPException(400, "Company already exists. Use PATCH to update.")

    products = [p.model_dump() for p in data.products] if data.products else []
    company = Company(
        owner_id=current_user.id,
        **{k: v for k, v in data.model_dump(exclude={"products"}).items()},
        products=products,
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)

    return _dict(company)


@router.patch("/")
async def update_company(
    data: CompanyUpdate,
    current_user=Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(Company).where(Company.owner_id == current_user.id))
    company = r.scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")

    update_data = data.model_dump(exclude_none=True, exclude={"products"})
    # The Settings form never receives the stored Telnyx key back (only a
    # masked hint), so a blank field means "keep the existing key" — don't
    # let an unrelated save overwrite it with an empty string.
    if not (update_data.get("telnyx_api_key") or "").strip():
        update_data.pop("telnyx_api_key", None)
    for k, v in update_data.items():
        setattr(company, k, v)
    if data.products is not None:
        company.products = [p.model_dump() for p in data.products]
    company.updated_at = datetime.utcnow()
    await db.commit()

    return _dict(company)