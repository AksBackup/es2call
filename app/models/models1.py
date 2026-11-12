"""
All database models — User, Company, Lead, CallLog, EmailLog, Batch, Schedule, KnowledgeDoc
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, DateTime, Text,
    ForeignKey, Float, Integer, JSON
)
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.core.crypto import EncryptedString


def _uuid():
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# User & Company
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# License — admin-generated activation keys (merged in from the standalone
# license server; see app/services/license_service.py and
# app/api/routes/admin.py)
# ─────────────────────────────────────────────────────────────────────────────

class License(Base):
    __tablename__ = "licenses"
    key            = Column(String, primary_key=True)   # e.g. AICAL-XXXX-XXXX-XXXX-XXXX
    client_name    = Column(String)
    tier           = Column(String, default="pro")       # starter | pro | enterprise
    status         = Column(String, default="inactive")  # inactive | active | revoked
    domain         = Column(String)                      # set on activation; None until then
    notes          = Column(Text)
    created_at     = Column(DateTime, default=datetime.utcnow)
    activated_at   = Column(DateTime)
    expires_at     = Column(DateTime)
    last_validated_at = Column(DateTime)
    revoked_at     = Column(DateTime)
    revoke_reason  = Column(Text)


class User(Base):
    __tablename__ = "users"
    id            = Column(String, primary_key=True, default=_uuid)
    email         = Column(String, unique=True, index=True, nullable=False)
    full_name     = Column(String, nullable=False)
    # Firebase Auth (Aug 2026) — replaces local password auth entirely.
    # firebase_uid is the Firebase ID token's `uid` claim; this table is
    # now a PROFILE keyed by it, not the identity/credential store itself
    # (Firebase owns passwords, sessions, and token issuance — see
    # app/core/firebase.py, app/core/security.py). hashed_password is kept
    # (nullable now) only so nothing breaks reading old rows; no code path
    # writes to it anymore.
    firebase_uid  = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=True)
    is_active     = Column(Boolean, default=True)
    # Magic-link email verification (Aug 2026) — gates access to /pricing
    # and the app dashboard until the person has clicked the verification
    # link Firebase emailed them. Kept in our own DB (rather than reading
    # the Firebase ID token's `email_verified` claim fresh on every
    # request) because that claim is baked into the token at mint time and
    # only updates after the client force-refreshes it — this column is
    # the source of truth the rest of the backend checks, and is synced
    # from Firebase in app/core/security.py / GET /auth/verification-status.
    # NOTE: on an existing production DB (not a fresh create_all()), this
    # needs a manual migration: ALTER TABLE users ADD COLUMN email_verified
    # BOOLEAN NOT NULL DEFAULT false;
    email_verified = Column(Boolean, default=False, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="owner", uselist=False)


class Company(Base):
    """One company per user (single-tenant per account)."""
    __tablename__ = "companies"
    id       = Column(String, primary_key=True, default=_uuid)
    owner_id = Column(String, ForeignKey("users.id"), unique=True, nullable=False)

    # Identity
    name        = Column(String, nullable=False)
    industry    = Column(String)
    description = Column(Text)
    website     = Column(String)
    location    = Column(String)

    # What company does / sells (shown to AI for context)
    services    = Column(Text)   # plain text description of services
    faqs        = Column(Text)   # common Q&A as text
    business_hours = Column(JSON)  # {"Monday": "9-6", ...}

    # Structured AI business knowledge. Keep exact operational facts here
    # instead of forcing the model to infer them from uploaded documents.
    # Shape is managed by the Knowledge page/API and may include office,
    # service areas, appointment rules, policies, languages, etc.
    business_knowledge = Column(JSON, default=dict)

    # Hindi/Hinglish counterparts — used only on the Vobiz (India) route.
    # Hand-written, not auto-translated. Falls back to the English field
    # above if left blank, so nothing breaks before these are filled in.
    description_hi = Column(Text)
    services_hi    = Column(Text)
    faqs_hi        = Column(Text)

    # Products JSON — list of {name, description, price, features,
    # name_hi, description_hi, features_hi}. Hindi fields are optional
    # per-item overrides used only for Vobiz calls; price is shared
    # across languages (digits/currency don't need translation).
    # Any product can be set as the active pitch target
    products    = Column(JSON, default=list)
    active_product = Column(String)   # name of product to pitch right now

    # AI agent personality
    agent_name      = Column(String, default="Aria")
    voice_language  = Column(String, default="hi-IN")   # hi-IN | en-IN | en-US
    voice_gender    = Column(String, default="female")   # female | male — drives
    # both which TTS speaker is picked AND how the LLM conjugates Hindi
    # first-person verbs (see _gender_grammar_note in vobiz_webhook.py)
    tts_provider    = Column(String, default="vobiz")
    # vobiz    | sarvam | deepgram — user-selectable in Settings.
    # NOTE: "deepgram" (Aura-2) has no Hindi support at all — only usable
    # for English-mode calls. See resolve_tts_provider() in
    # app/services/tts/providers.py, which auto-falls-back to sarvam.
    tts_voice       = Column(String)   # optional: exact speaker/voice id
    # override, e.g. "anushka" (Sarvam) or "aura-2-luna-en" (Deepgram).
    # Leave blank to auto-pick a sensible default for voice_gender.

    # Custom prompts (optional — overrides defaults if set)
    inbound_system_prompt  = Column(Text)
    outbound_sales_prompt  = Column(Text)
    greeting_inbound       = Column(Text)   # English fallback
    greeting_outbound      = Column(Text)   # English fallback
    greeting_inbound_hi    = Column(Text)   # Vobiz / Hindi-Hinglish
    greeting_outbound_hi   = Column(Text)   # Vobiz / Hindi-Hinglish

    # Fallback number for human transfer
    forward_number  = Column(String)
    contact_number  = Column(String)

    # Vobiz config — sole telephony provider
    # Encrypted at rest (see app/core/crypto.py) — these are the
    # customer's own Vobiz API credentials, transparently decrypted on
    # read so vobiz_service.py etc. see plain strings as before. Requires
    # ENCRYPTION_KEY to be set in .env.
    vobiz_auth_id      = Column(EncryptedString)
    vobiz_auth_token   = Column(EncryptedString)
    vobiz_phone_number = Column(String)   # India DID, E.164 — not a secret, left plain

    # ── Demo accounts ─────────────────────────────────────────────────
    # A single shared login (e.g. demouser@ancentrix.com) handed out to
    # prospects one at a time. is_demo_account gates the check in
    # app/tasks/tasks.py's _async_outbound_call — real customer accounts
    # are unaffected regardless of what demo_calls_remaining holds.
    # Admin resets demo_calls_remaining back up via POST
    # /api/v1/admin/companies/{id}/demo-calls before handing the login to
    # the next prospect.
    is_demo_account       = Column(Boolean, default=False, nullable=False, server_default="0")
    demo_calls_remaining  = Column(Integer, default=0)

    # ── Plan / minutes balance (replaces the old yearly license system) ────
    # Removed: license_key / license_domain / license_tier / license_status /
    # license_expires_at. A company now has a minute-based plan paid for via
    # Cashfree (see app/services/payment/cashfree_service.py,
    # app/services/plan_service.py, app/api/routes/payments.py) instead of
    # an admin-issued activation key.
    #
    # plan_type: trial | basic | standard | custom | none
    #   - trial:    ₹100 / 10 min, ONE-TIME per company ever (see trial_used)
    #   - basic:    ₹2500 / 500 min  (₹5/min flat)
    #   - standard: ₹2000 / 2000 min (₹4/min flat)
    #   - custom:   customer enters any amount >= ₹1000, minutes = amount / 4.3
    plan_type          = Column(String, default="none", nullable=False, server_default="none")
    plan_minutes_total  = Column(Integer, default=0, nullable=False, server_default="0")      # minutes purchased on the current/last paid order
    plan_rate_per_minute = Column(Float, default=0.0, nullable=False, server_default="0")      # ₹/min actually charged for this plan
    plan_amount_paid    = Column(Float, default=0.0, nullable=False, server_default="0")       # ₹ actually paid for the current plan
    plan_purchased_at   = Column(DateTime)
    plan_expires_at      = Column(DateTime)                 # plan_purchased_at + 365 days
    # Minutes ACTUALLY consumed under the current plan — incremented by
    # plan_service.record_minutes_used() as calls complete (see
    # CallLog.duration_seconds), NOT derived by summing call logs on every
    # read. Reset to 0 whenever a new plan is purchased (old unused minutes
    # do not carry over — see plan_service.apply_paid_plan()).
    plan_minutes_used   = Column(Float, default=0.0, nullable=False, server_default="0")
    # Trial (₹100/10 min) is one-time-ever per company, even after it
    # expires or is fully used and the company later buys a paid plan —
    # this flag is never cleared once set.
    trial_used          = Column(Boolean, default=False, nullable=False, server_default="0")

    # Email identity
    email_from_address = Column(String)
    email_from_name    = Column(String)
    email_reply_to     = Column(String)
    email_signature    = Column(Text)

    # Vector store
    vector_collection_id = Column(String)

    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner               = relationship("User", back_populates="company")
    leads               = relationship("Lead", back_populates="company")
    call_logs           = relationship("CallLog", back_populates="company")
    email_logs          = relationship("EmailLog", back_populates="company")
    batches             = relationship("Batch", back_populates="company")
    schedules           = relationship("Schedule", back_populates="company")
    knowledge_documents = relationship("KnowledgeDocument", back_populates="company")
    appointments        = relationship("Appointment", back_populates="company")


# ─────────────────────────────────────────────────────────────────────────────
# Lead
# ─────────────────────────────────────────────────────────────────────────────

class Lead(Base):
    __tablename__ = "leads"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)

    # Contact
    name  = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String)

    # Classification
    # new → contacted → called (rang, no pickup) → interested | warm | cold
    #     → closed_won | closed_lost | do_not_call
    status         = Column(String, default="new")
    interest_level = Column(Float, default=0.0)   # 0.0 – 1.0
    source         = Column(String, default="manual")  # manual | csv | api | crm

    # AI-extracted facts from conversations
    key_info = Column(JSON, default=dict)  # {budget, timeline, pain_points, objections}
    notes    = Column(Text)

    # Set (or auto-prompted) when a call rings out with no answer — lets the
    # team leave a question/reminder for whoever tries this lead next.
    follow_up_question = Column(Text)

    # Language preference — detected automatically or set manually
    language = Column(String, default="hinglish")  # hinglish | hindi | english

    # Timezone for per-lead scheduling
    timezone = Column(String, default="Asia/Kolkata")

    # Voice call tracking
    call_attempts    = Column(Integer, default=0)
    last_called_at   = Column(DateTime)
    scheduled_call_at = Column(DateTime)

    # Email tracking
    email_status   = Column(String, default="not_contacted")
    email_attempts = Column(Integer, default=0)
    last_emailed_at = Column(DateTime)
    email_opt_out  = Column(Boolean, default=False)

    # Campaign/batch ref
    campaign_name = Column(String)

    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company     = relationship("Company", back_populates="leads")
    call_logs   = relationship("CallLog", back_populates="lead")
    email_logs  = relationship("EmailLog", back_populates="lead")
    batch_leads = relationship("BatchLead", back_populates="lead")
    appointments = relationship("Appointment", back_populates="lead")


# ─────────────────────────────────────────────────────────────────────────────
# Call Log
# ─────────────────────────────────────────────────────────────────────────────

class CallLog(Base):
    __tablename__ = "call_logs"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    lead_id    = Column(String, ForeignKey("leads.id"), nullable=True)

    direction = Column(String, default="inbound")   # inbound | outbound
    status    = Column(String, default="queued")    # queued|ringing|in_progress|completed|failed|no_answer
    mode      = Column(String, default="support")   # support | sales
    provider  = Column(String, default="vobiz")     # vobiz — sole carrier
    # ai = placed by the AI agent (existing flow). human = manually dialed
    # by an agent from the Human Call tab — click-to-call bridge (rings the
    # agent's own phone via Vobiz, then bridges to the lead once answered;
    # see app/api/routes/human_calls.py). NOT true in-browser WebRTC audio —
    # this codebase's Vobiz integration is REST/XML call-creation only, no
    # WebRTC SDK usage anywhere, so the bridge approach is what's actually
    # verified to work against this account.
    # Drives the AI/Human split on the Call Log screen.
    channel   = Column(String, default="ai", nullable=False, server_default="ai")        # ai | human
    # For channel="human" calls only — set from the post-call dialog the
    # agent fills in after hangup (lead status / summary / notes), mirroring
    # what the AI writes to lead_status_after / summary / transcript for AI
    # calls. dialed_by is the User.id of the agent who placed the call.
    dialed_by = Column(String, ForeignKey("users.id"), nullable=True)

    from_number = Column(String)
    to_number   = Column(String)

    # (Telnyx removed — Vobiz is the sole telephony provider now)
    call_control_id = Column(String, unique=True, index=True)

    started_at       = Column(DateTime)
    ended_at         = Column(DateTime)
    duration_seconds = Column(Integer, default=0)

    # Full conversation stored as JSON list [{role, content}]
    conversation_history = Column(JSON, default=list)
    transcript           = Column(Text)   # flattened version
    summary              = Column(Text)

    # AI analysis results
    sentiment         = Column(String)
    intent            = Column(String)
    lead_status_after = Column(String)   # what AI thinks lead status should be

    recording_url       = Column(String)
    transferred_to_human = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="call_logs")
    lead    = relationship("Lead", back_populates="call_logs")


# ─────────────────────────────────────────────────────────────────────────────
# Appointments
# ─────────────────────────────────────────────────────────────────────────────

class Appointment(Base):
    __tablename__ = "appointments"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False, index=True)
    lead_id    = Column(String, ForeignKey("leads.id"), nullable=True, index=True)

    appointment_type = Column(String, default="site_visit", nullable=False)  # site_visit | office_meeting | callback
    status = Column(String, default="confirmed", nullable=False)  # pending|confirmed|completed|cancelled|rescheduled|no_show
    product = Column(String)
    location = Column(String)
    scheduled_at = Column(DateTime, nullable=False, index=True)
    duration_minutes = Column(Integer, default=30)
    notes = Column(Text)
    created_by = Column(String, default="ai", nullable=False)  # ai | human | admin
    source_call_id = Column(String, ForeignKey("call_logs.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="appointments")
    lead = relationship("Lead", back_populates="appointments")



# ─────────────────────────────────────────────────────────────────────────────
# Email Log
# ─────────────────────────────────────────────────────────────────────────────

class EmailLog(Base):
    __tablename__ = "email_logs"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    lead_id    = Column(String, ForeignKey("leads.id"), nullable=True)
    batch_id   = Column(String, ForeignKey("batches.id"), nullable=True)

    to_email   = Column(String, nullable=False)
    to_name    = Column(String)
    subject    = Column(String, nullable=False)
    body_text  = Column(Text)
    from_email = Column(String)
    from_name  = Column(String)

    # Delivery status
    status             = Column(String, default="queued")  # queued|sent|delivered|opened|replied|bounced|failed
    sendgrid_message_id = Column(String)
    sent_at            = Column(DateTime)
    opened_at          = Column(DateTime)
    replied_at         = Column(DateTime)

    # Reply handling
    reply_body       = Column(Text)
    reply_status     = Column(String, default="unread")  # unread|ai_replied|queued_for_review|human_replied
    ai_reply_draft   = Column(Text)
    ai_reply_confidence = Column(Float, default=0.0)
    ai_reply_sent    = Column(Boolean, default=False)
    ai_reply_sent_at = Column(DateTime)

    # AI analysis of reply
    reply_sentiment = Column(String)
    reply_intent    = Column(String)

    # Full thread [{role: ai|lead, body, timestamp}]
    email_thread = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="email_logs")
    lead    = relationship("Lead", back_populates="email_logs")
    batch   = relationship("Batch", back_populates="email_logs")


# ─────────────────────────────────────────────────────────────────────────────
# Batch & Schedule
# ─────────────────────────────────────────────────────────────────────────────

class Batch(Base):
    """A named snapshot of leads for a voice or email campaign."""
    __tablename__ = "batches"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)

    name        = Column(String, nullable=False)
    description = Column(Text)
    batch_type  = Column(String, nullable=False)   # voice | email
    provider    = Column(String, default="vobiz")   # vobiz — sole carrier dispatching this batch's calls
    # ai = existing fully-automated flow (leads dialed by the AI agent).
    # human = leads in this batch are worked manually from the Human Call
    # tab (agent dials each lead themselves via the click-to-call bridge —
    # see app/api/routes/human_calls.py — using the SAME company Vobiz
    # credentials/number) instead of being auto-dialed. Only meaningful for
    # batch_type="voice".
    agent_type  = Column(String, default="ai", nullable=False, server_default="ai")   # ai | human

    # Filter used to build this batch (stored for reference)
    filter_criteria = Column(JSON, default=dict)
    lead_count      = Column(Integer, default=0)

    # Progress
    status           = Column(String, default="draft")  # draft|scheduled|running|paused|completed|failed
    leads_processed  = Column(Integer, default=0)
    leads_succeeded  = Column(Integer, default=0)
    leads_failed     = Column(Integer, default=0)

    # Context
    campaign_name  = Column(String)
    product_focus  = Column(String)   # which product to pitch
    call_mode      = Column(String, default="sales")  # sales | support

    # Email templates
    email_subject_template = Column(String)
    email_body_template    = Column(Text)

    started_at   = Column(DateTime)
    completed_at = Column(DateTime)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company     = relationship("Company", back_populates="batches")
    schedules   = relationship("Schedule", back_populates="batch")
    email_logs  = relationship("EmailLog", back_populates="batch")
    batch_leads = relationship("BatchLead", back_populates="batch")


class BatchLead(Base):
    __tablename__ = "batch_leads"
    id       = Column(String, primary_key=True, default=_uuid)
    batch_id = Column(String, ForeignKey("batches.id"), nullable=False)
    lead_id  = Column(String, ForeignKey("leads.id"), nullable=False)

    processed    = Column(Boolean, default=False)
    processed_at = Column(DateTime)
    result       = Column(String)  # success | failed | skipped

    batch = relationship("Batch", back_populates="batch_leads")
    lead  = relationship("Lead", back_populates="batch_leads")


class Schedule(Base):
    """When and how fast to run a batch."""
    __tablename__ = "schedules"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    batch_id   = Column(String, ForeignKey("batches.id"), nullable=False)

    start_datetime = Column(DateTime, nullable=False)
    end_datetime   = Column(DateTime)

    window_start_time = Column(String, default="09:00")   # "HH:MM"
    window_end_time   = Column(String, default="18:00")
    base_timezone     = Column(String, default="Asia/Kolkata")
    use_lead_timezone = Column(Boolean, default=True)

    # ["Monday", "Tuesday", ...] — empty = all days
    allowed_days = Column(JSON, default=lambda: ["Monday","Tuesday","Wednesday","Thursday","Friday"])

    max_per_hour           = Column(Integer, default=10)
    delay_between_seconds  = Column(Integer, default=30)

    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="schedules")
    batch   = relationship("Batch", back_populates="schedules")


# ─────────────────────────────────────────────────────────────────────────────
# Payments (Cashfree) — replaces the old license/activation-key system
# ─────────────────────────────────────────────────────────────────────────────

class PaymentOrder(Base):
    """One row per Cashfree order (one-time recharge — see
    app/services/payment/cashfree_service.py). Created in "created" status
    when the frontend asks us to start a checkout; flipped to "paid" or
    "failed" by the Cashfree webhook (app/api/routes/payments.py), which is
    also what actually credits the minutes onto Company via
    app/services/plan_service.apply_paid_plan()."""
    __tablename__ = "payment_orders"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)

    cf_order_id = Column(String, unique=True, index=True, nullable=False)  # our order_id, sent to Cashfree
    cf_cf_order_id = Column(String)   # Cashfree's own internal "cf_order_id" from the create-order response

    plan_type  = Column(String, nullable=False)   # trial | basic | standard | custom
    minutes    = Column(Integer, nullable=False)
    rate_per_minute = Column(Float, nullable=False)
    amount     = Column(Float, nullable=False)     # ₹, order_amount sent to Cashfree

    status     = Column(String, default="created")  # created | processing | paid | failed
    payment_session_id = Column(String)   # returned by Cashfree create-order, used by the frontend checkout widget

    raw_create_response = Column(JSON)
    raw_webhook_payload  = Column(JSON)

    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at    = Column(DateTime)

    company = relationship("Company")


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge Base
# ─────────────────────────────────────────────────────────────────────────────

class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id         = Column(String, primary_key=True, default=_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)

    filename    = Column(String, nullable=False)
    file_type   = Column(String)   # pdf | txt | docx | url
    file_path   = Column(String)
    file_size   = Column(Integer)

    status       = Column(String, default="pending")  # pending|processing|completed|failed
    chunks_count = Column(Integer, default=0)
    error_msg    = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="knowledge_documents")