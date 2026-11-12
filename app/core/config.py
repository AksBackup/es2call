"""
Central configuration — reads from .env (production) or .env.local (dev).
All optional fields default to None so the app starts without crashing.
"""
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings
from typing import List, Optional


class Settings(BaseSettings):
    # BUG FIX: many .env templates (including the one I generated) write
    # an optional/"leave blank to use default" field as `KEY=` — present,
    # but empty. To a human that reads as "not set." To pydantic-settings
    # it's a real empty-string VALUE, which then gets fed into that
    # field's type coercion — fine for a plain `str` field, but a hard
    # crash for anything else: `DB_STATEMENT_CACHE_SIZE=` (Optional[int])
    # raised "Input should be a valid integer... input_value=''". This
    # isn't a one-field problem — 27 fields in this class alone are
    # int/float/bool/Optional[int]/Optional[float], every one of them a
    # landmine under the exact same blank-line pattern. Rather than patch
    # each one as it's individually hit, this strips any blank-string
    # value out of the merged env data BEFORE pydantic's per-field type
    # coercion runs, so a blank line behaves exactly like an absent
    # line: the field's own default applies. mode="before" + a dict
    # comprehension over the raw merged-source data — verified against
    # the actual pinned pydantic-settings==2.6.1, including alongside the
    # ALLOWED_ORIGINS alias-based fix below (the two don't conflict).
    @model_validator(mode="before")
    @classmethod
    def _blank_env_vars_become_unset(cls, data):
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data

    # ── App ───────────────────────────────────────────────────────────────
    APP_NAME: str = "AI Call Center"
    APP_VERSION: str = "5.0.0"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"

    # ── Database ──────────────────────────────────────────────────────────
    # SQLite for local dev, Neon (managed Postgres, free tier) for
    # production. Neon connection strings need `?ssl=require` (asyncpg's
    # param name — NOT `sslmode=require`, that's the psycopg2 spelling).
    # e.g. postgresql+asyncpg://user:pass@ep-xxxx.neon.tech/dbname?ssl=require
    #
    # Two connection-string flavors Neon gives you:
    #   - Direct (unpooled)  — use this one by default; combine with the
    #     pool settings below.
    #   - Pooled (pgbouncer, transaction mode) — if you use this one
    #     instead, DB_STATEMENT_CACHE_SIZE below MUST be 0 (see database.py),
    #     since pgbouncer transaction pooling and asyncpg's prepared
    #     statement cache don't mix.
    DATABASE_URL: str = "sqlite+aiosqlite:///./callcenter.db"

    # Schema is managed by Alembic migrations now (see alembic/), not
    # create_all(). This only controls whether app/main.py's startup path
    # still calls create_all() as a convenience for local SQLite dev —
    # it's ignored for Postgres. Never set true against Neon/production.
    AUTO_CREATE_TABLES_SQLITE_ONLY: bool = True

    # asyncpg connection pool sizing — conservative defaults so a handful
    # of replicas + Celery workers don't blow through Neon's free-tier
    # connection limit. Raise DB_POOL_SIZE only after checking Neon's
    # dashboard for your plan's connection cap.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_RECYCLE_SECONDS: int = 300      # Neon can idle-close connections; recycle before that
    # Set to 0 if DATABASE_URL points at Neon's pooled (pgbouncer) endpoint.
    # Leave at the asyncpg default (unset/None -> asyncpg's own default)
    # for a direct/unpooled Neon connection string.
    DB_STATEMENT_CACHE_SIZE: Optional[int] = None

    # ── Redis ─────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379"

    # ── Firebase Auth ─────────────────────────────────────────────────────
    # Replaces local email/password + JWT auth entirely (see
    # app/core/firebase.py, app/core/security.py). The backend verifies
    # Firebase ID tokens via the Admin SDK — it never sees or stores
    # passwords. Provide the service account ONE of these two ways:
    #   - FIREBASE_SERVICE_ACCOUNT_JSON: paste the whole downloaded JSON as
    #     a single-line string (easiest on platforms without file mounts).
    #   - GOOGLE_APPLICATION_CREDENTIALS: path to that JSON file on disk.
    # If neither is set, falls back to Application Default Credentials
    # (works automatically on Cloud Run/GCP with an attached service
    # account, fails loudly elsewhere — see app/core/firebase.py).
    FIREBASE_PROJECT_ID: Optional[str] = None
    FIREBASE_SERVICE_ACCOUNT_JSON: Optional[str] = None
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = None

    # ── JWT ───────────────────────────────────────────────────────────────
    # No longer used for session tokens (Firebase issues those now) — kept
    # only because a couple of internal one-off signed links could still
    # use jose/jwt directly in the future. Safe to leave at the default;
    # nothing security-sensitive depends on it anymore.
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"

    # ── CORS ──────────────────────────────────────────────────────────────
    # BUG FIX: a plain List[str] field crashes on startup if the .env value
    # is a comma-separated string instead of a JSON array — pydantic-
    # settings auto-JSON-decodes any env var backing a "complex" type
    # (list/dict/set/...), and that decode happens in the settings SOURCE
    # layer, BEFORE any field_validator gets a chance to run (confirmed by
    # actually reproducing the crash — a validator alone does not fix
    # this). The fix here is deliberately version-independent — verified
    # against pydantic-settings==2.6.1 specifically, the version actually
    # pinned in requirements.txt (an earlier attempt using pydantic-
    # settings' NoDecode marker worked on 2.15.0 but doesn't exist at all
    # in 2.6.1 — checked directly, not assumed): read it as a plain `str`,
    # which never triggers the complex-type auto-decode in ANY version,
    # via `validation_alias` so the .env key stays ALLOWED_ORIGINS, and
    # expose the real List[str] as a computed @property under the same
    # public name every caller already uses — a property reads identically
    # to a field from the outside, so main.py's CORS middleware setup
    # needs zero changes.
    allowed_origins_raw: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="ALLOWED_ORIGINS",
    )

    @property
    def ALLOWED_ORIGINS(self) -> List[str]:
        v = self.allowed_origins_raw.strip()
        if v.startswith("["):
            import json
            return json.loads(v)
        return [origin.strip() for origin in v.split(",") if origin.strip()]

    # ── Public base URL(s) ────────────────────────────────────────────────
    # These are used for two DIFFERENT purposes and deliberately kept
    # separate — collapsing them into one setting was a real bug (Aug
    # 2026): Cashfree's return_url is only ever navigated to by the
    # PERSON'S OWN BROWSER after checkout, so it never needs to be
    # internet-reachable — http://localhost:3000 works fine for local
    # dev. notify_url is called SERVER-TO-SERVER by Cashfree itself and
    # genuinely does need a public tunnel (ngrok/cloudflared) in local
    # dev. Forcing both through the same tunnel URL meant a flaky local
    # tunnel dying broke the person's own post-payment redirect, which
    # never needed to leave their machine in the first place.
    #
    # PUBLIC_BASE_URL: this backend's own public address — Vobiz
    # answer/hangup webhooks, human-call bridge webhooks, and Cashfree's
    # notify_url all use this. Needs a real tunnel/domain even in local
    # dev, since external services call it. Was TELNYX_WEBHOOK_BASE_URL;
    # renamed since it's not Telnyx-specific. Old var name still read as a
    # fallback by vobiz_service._get_base_url() env-file check.
    PUBLIC_BASE_URL: Optional[str] = None
    # FRONTEND_PUBLIC_URL: where the Next.js app is reachable, for
    # building Cashfree's return_url. In local dev this is just
    # http://localhost:3000 — no tunnel needed. Falls back to
    # PUBLIC_BASE_URL if unset, so an existing single-domain production
    # setup (one domain serving both frontend and backend) keeps working
    # unchanged.
    FRONTEND_PUBLIC_URL: Optional[str] = None

    # ── Vobiz (sole telephony provider) ──────────────────────────────────
    # Real (non-demo) companies must still set their own vobiz_auth_id/
    # vobiz_auth_token/vobiz_phone_number via Settings — there is NO
    # fallback to these for a normal paying company, so one customer can
    # never silently place/receive calls (and incur charges) on another
    # customer's or the shared demo account. See _creds() in
    # app/services/telephony/vobiz_service.py and vobiz_stream_pipeline.py.
    #
    # The one deliberate exception is the shared demo account
    # (Company.is_demo_account=True — see models.py). Prospects handed the
    # demo login shouldn't have to go set up their own Vobiz account just
    # to try the product, so a demo company with blank vobiz_* fields (or
    # any of the three left blank) falls back to these .env values
    # instead. Leave all three blank here to require the demo account to
    # have its own credentials too.
    VOBIZ_AUTH_ID: Optional[str] = None
    VOBIZ_AUTH_TOKEN: Optional[str] = None
    VOBIZ_PHONE_NUMBER: Optional[str] = None
    # True -> outbound calls use the Pipecat streaming pipeline
    # (/vobiz-stream/answer-stream). False -> rollback to the old
    # Record+Gather XML flow (/vobiz/answer). Override with
    # USE_STREAMING_CALLS=false in .env if you need to roll back without
    # a code change.
    USE_STREAMING_CALLS: bool = True

    # ── Telnyx (second telephony provider, alongside Vobiz) ──────────────
    # Same multi-tenant rule as Vobiz: every real company brings its own
    # Telnyx account (Settings -> Telephony). These .env values are ONLY a
    # fallback for the shared demo account (Company.is_demo_account=True).
    #   TELNYX_API_KEY        - v2 API key ("KEY0...") from Mission Control
    #   TELNYX_CONNECTION_ID  - ID of a *Call Control Application* (a.k.a.
    #                           Voice API application) — NOT a SIP/FQDN
    #                           connection and NOT a TeXML app.
    #   TELNYX_PHONE_NUMBER   - E.164 number assigned to that application
    #   TELNYX_PUBLIC_KEY     - account Ed25519 public key (Mission Control
    #                           -> Keys & Credentials -> Public Key), used
    #                           to verify webhook signatures.
    TELNYX_API_KEY: Optional[str] = None
    TELNYX_CONNECTION_ID: Optional[str] = None
    TELNYX_PHONE_NUMBER: Optional[str] = None
    TELNYX_PUBLIC_KEY: Optional[str] = None
    # True -> reject Telnyx webhooks (403) when no public key is configured
    # or the signature is bad. False (default) -> a bad signature is still
    # rejected, but a missing key only logs a warning, so first-time setup
    # doesn't silently drop every hangup webhook.
    TELNYX_REQUIRE_SIGNATURE: bool = False

    # ── License server — REMOVED ───────────────────────────────────────────
    # The yearly license/activation-key system (app/services/license_service.py,
    # app/api/routes/license.py, Company.license_*) has been replaced by the
    # Cashfree minute-based plan system below. LICENSE_SERVER_URL is left
    # defined ONLY so an old .env with this key set doesn't crash Settings
    # parsing (extra="ignore" would handle that anyway) — nothing reads it.
    LICENSE_SERVER_URL: str = "http://localhost:8100"

    # ── Payment gateway selection ────────────────────────────────────────
    # paypal (default) | cashfree. Existing Cashfree code stays in place and
    # can be re-enabled by setting PAYMENT_GATEWAY=cashfree.
    PAYMENT_GATEWAY: str = "paypal"

    # ── PayPal (Orders v2) — see app/services/payment/paypal_service.py ──
    # developer.paypal.com -> Apps & Credentials -> create a REST app.
    # Use the Sandbox pair + PAYPAL_ENV=sandbox first, then Live + live.
    PAYPAL_CLIENT_ID: Optional[str] = None
    PAYPAL_CLIENT_SECRET: Optional[str] = None
    PAYPAL_ENV: str = "sandbox"            # sandbox | live
    # Plans are priced in rupees; PayPal normally can't charge INR, so the
    # rupee price is converted to this currency at PAYPAL_INR_PER_USD.
    # Update the rate whenever you want (it is applied per order).
    PAYPAL_CURRENCY: str = "USD"
    PAYPAL_INR_PER_USD: float = 90.0

    # ── Cashfree Payment Gateway ─────────────────────────────────────────
    # Get these from Merchant Dashboard -> Developers -> API Keys.
    # Use the SANDBOX keys + CASHFREE_ENV=sandbox for testing before you
    # have a live/production Cashfree account approved.
    CASHFREE_CLIENT_ID: Optional[str] = None
    CASHFREE_CLIENT_SECRET: Optional[str] = None
    # Separate secret from Merchant Dashboard -> Developers -> Webhooks ->
    # your endpoint's "Webhook Secret" — NOT the same as CLIENT_SECRET.
    # Used only to verify inbound webhook signatures.
    CASHFREE_WEBHOOK_SECRET: Optional[str] = None
    CASHFREE_ENV: str = "sandbox"          # sandbox | production
    CASHFREE_API_VERSION: str = "2023-08-01"

    # ── Plan pricing (₹) — see app/services/plan_service.py ──────────────
    PLAN_TRIAL_AMOUNT: float = 100.0
    PLAN_TRIAL_MINUTES: int = 10
    PLAN_BASIC_AMOUNT: float = 2500.0
    PLAN_BASIC_MINUTES: int = 500
    # Standard: 2000 minutes @ ₹4/min = ₹8000 total (NOT ₹2000 — that was
    # this constant's own bug on first pass, caught by the plan_service
    # unit check below before it ever shipped: amount must be minutes ×
    # rate, not the minutes figure itself).
    PLAN_STANDARD_AMOUNT: float = 8000.0
    PLAN_STANDARD_MINUTES: int = 2000
    PLAN_CUSTOM_RATE_PER_MINUTE: float = 4.3
    PLAN_CUSTOM_MIN_AMOUNT: float = 1000.0
    PLAN_EXPIRY_DAYS: int = 365

    # Bearer token that gates every /api/v1/admin/* route (license
    # generation, user list, password resets) and the admin.html panel.
    # CHANGE THIS in production — treat it like a root password. Access
    # this panel only from a trusted network (VPN/IP-allowlist at your
    # reverse proxy) since the token alone is the only gate.
    ADMIN_TOKEN: str = "change-this-admin-token-in-production"

    # Login rate limiting (see app/api/routes/auth.py) — keyed per
    # email+IP in Redis so it's consistent across worker processes.
    LOGIN_RATE_LIMIT_ATTEMPTS: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 900   # 15 minutes

    # Admin route rate limiting (see app/api/routes/admin.py) — keyed per
    # IP in Redis, same mechanism as login. The admin token has no
    # lockout of its own, so this is what actually slows down someone
    # hammering /api/v1/admin/* with guessed tokens.
    ADMIN_RATE_LIMIT_ATTEMPTS: int = 20
    ADMIN_RATE_LIMIT_WINDOW_SECONDS: int = 300   # 5 minutes

    # ── Field-level encryption ────────────────────────────────────────────
    # Fernet key (32 url-safe base64 bytes) used to encrypt sensitive
    # per-tenant columns at rest — currently Company.vobiz_auth_token and
    # Company.vobiz_auth_id (see app/core/crypto.py). Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Required in production once any company has Vobiz credentials
    # saved; app/core/crypto.py raises loudly instead of silently storing
    # plaintext if this is unset and encryption is actually attempted.
    ENCRYPTION_KEY: Optional[str] = None

    # ── Deepgram STT ──────────────────────────────────────────────────────────
    # Free tier: 45,000 minutes/month
    # Sign up: https://console.deepgram.com
    DEEPGRAM_API_KEY: Optional[str] = None

    # ── Sarvam AI (Hindi/Hinglish TTS) ──────────────────────────────────────
    # Sign up: https://dashboard.sarvam.ai — purpose-built for Indian
    # languages, low-latency WS streaming, outputs mulaw/8kHz natively
    # (matches Vobiz's stream format with zero resampling).
    SARVAM_API_KEY: Optional[str] = None

    # ── LLM ───────────────────────────────────────────────────────────────
    LLM_PROVIDER: str = "groq"                      # groq | openai | anthropic
    GROQ_API_KEY: Optional[str] = None
    # BUG FIX (Aug 2026): Groq deprecated llama-3.3-70b-versatile on
    # June 17, 2026 (see console.groq.com/docs/deprecations) — it now
    # 404s with "model_not_found" on every request, breaking both live
    # call responses AND post-call summary/sentiment analysis (both go
    # through this same setting — see llm_service.py and
    # vobiz_stream_pipeline.py's GroqLLMService.Settings). Groq's own
    # docs recommend openai/gpt-oss-120b as the direct replacement.
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"

    # ── TTS ───────────────────────────────────────────────────────────────
    # Global fallback default; per-company override lives on
    # Company.tts_provider so each customer can pick their own in Settings.
    #
    # "vobiz"    = Vobiz's native XML <Speak> (turn-based, no extra vendor
    #              key needed, but adds a full webhook round-trip per turn)
    # "sarvam"   = Sarvam AI — recommended for Hindi/Hinglish. Purpose-built
    #              for Indian languages, streams over Vobiz's WS media
    #              stream for real-time playback instead of round-tripping.
    # "deepgram" = Deepgram Aura-2 — DOES NOT SUPPORT HINDI (only en, es,
    #              de, fr, nl, it, ja as of this writing — see
    #              developers.deepgram.com/docs/tts-models). Only usable
    #              for English-mode calls; resolve_tts_provider() below
    #              auto-falls-back to Sarvam if "deepgram" is selected for
    #              a Hindi/Hinglish call.
    TTS_PROVIDER: str = "vobiz"

    # ── STT ───────────────────────────────────────────────────────────────
    # Vobiz calls are record-then-transcribe via Deepgram REST (see
    # vobiz_webhook.py), or Gather-mode where Vobiz transcribes for us.
    STT_PROVIDER: str = "deepgram"

    # ── Email ─────────────────────────────────────────────────────────────
    SENDGRID_API_KEY: Optional[str] = None
    EMAIL_FROM_ADDRESS: str = "noreply@yourdomain.com"
    EMAIL_FROM_NAME: str = "AI Agent"
    EMAIL_REPLY_TO: Optional[str] = None
    IMAP_HOST: str = "imap.gmail.com"
    IMAP_PORT: int = 993
    IMAP_USERNAME: Optional[str] = None
    IMAP_PASSWORD: Optional[str] = None
    IMAP_MAILBOX: str = "INBOX"
    EMAIL_AUTO_REPLY_CONFIDENCE: float = 0.75
    EMAIL_POLL_INTERVAL_SECONDS: int = 120

    # ── Vector DB ─────────────────────────────────────────────────────────
    CHROMADB_HOST: str = "localhost"
    CHROMADB_PORT: int = 8001
    CHROMADB_LOCAL_PATH: str = "./chroma_data"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # ── Storage ───────────────────────────────────────────────────────────
    UPLOAD_DIR: str = "./uploads"
    MAX_FILE_SIZE_MB: int = 50

    # ── Celery ────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # ── Call settings ─────────────────────────────────────────────────────
    MAX_CALL_DURATION_SECONDS: int = 1800
    OUTBOUND_CONCURRENT_LIMIT: int = 10   # raise after Vobiz raises your account's concurrency cap

    class Config:
        env_file = (".env.local", ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
