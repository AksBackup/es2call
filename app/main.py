import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
"""
AI Call Center — FastAPI Application
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import create_tables

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def _warm_static_tts_background():
    """Pre-synthesize the fixed reprompt/error/transfer/filler phrases
    (plus each company's own greeting) through Sarvam so the first live
    call doesn't pay a live TTS round trip. Runs in the background,
    fired off after the app is already accepting traffic — this makes
    per-company external API calls, so it must never gate the health
    check or block a deploy across N replicas."""
    try:
        from app.core.database import AsyncSessionLocal as _ASL
        from app.models.models import Company as _Company
        from app.api.routes.vobiz_webhook import warmup_static_tts
        from sqlalchemy import select as _select
        async with _ASL() as db:
            r = await db.execute(_select(_Company))
            companies = r.scalars().all()
        n = await warmup_static_tts(companies)
        logger.info(f"Static TTS cache warmed — {n} phrases")
    except Exception as e:
        logger.warning(f"Static TTS warmup failed (non-fatal): {e}")


# NOTE: _refresh_licenses_background() removed — the yearly license/
# activation-key system (Company.license_*, app/services/license_service.py,
# app/api/routes/license.py) has been replaced by the Cashfree minute-based
# plan system (app/services/plan_service.py, app/api/routes/payments.py).
# There's nothing to background-refresh for plans — balance is computed
# on read from Company.plan_minutes_used / plan_minutes_total, not
# revalidated against an external server.


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — keep this path fast. Anything that makes a per-company
    # network call (TTS pre-warm, license refresh) runs in the background
    # instead of being awaited here, so the container reaches a healthy
    # state quickly regardless of how many companies exist, and N
    # replicas don't all serialize the same slow work before accepting
    # traffic.
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.CHROMADB_LOCAL_PATH, exist_ok=True)

    # Schema is managed by Alembic (`alembic upgrade head` — see
    # Dockerfile/DEPLOYMENT.md), which you run as a separate release step
    # before starting the app, not here. create_tables() is now a no-op
    # against Postgres/Neon and only still creates tables for local
    # SQLite dev — see app/core/database.py.
    print("=== DEBUG === DATABASE_URL:", settings.DATABASE_URL)
    print("=== DEBUG === AUTO_CREATE_TABLES_SQLITE_ONLY:", settings.AUTO_CREATE_TABLES_SQLITE_ONLY)
    from app.core.database import Base
    print("=== DEBUG === tables registered:", list(Base.metadata.tables.keys()))
    await create_tables()

    # ALLOWED_USERS/provision_allowed_users() removed — Firebase Auth
    # (Aug 2026) handles account creation client-side now; there's no
    # local password bootstrap step to run at startup anymore. A new
    # account is auto-provisioned in this DB the first time its Firebase
    # ID token hits any authenticated endpoint — see
    # app/core/security.py's _get_or_create_user().

    try:
        from app.services.llm.rag_service import rag_service
        await asyncio.get_event_loop().run_in_executor(None, rag_service.warmup)
        logger.info("RAG service ready")
    except Exception as e:
        logger.warning(f"RAG warmup failed (non-fatal): {e}")

    # Fire-and-forget — do not await. See docstring above for why.
    asyncio.create_task(_warm_static_tts_background())

    yield

    # Shutdown
    from app.services.llm.llm_service import llm_service
    await llm_service.close()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Admin panel (license generation, user management) ──────────────────────────
# Served at /admin — deliberately OUTSIDE /api/v1 so it doesn't collide
# with /api/v1/admin/* (the actual admin API routes it calls). This is a
# static HTML page that authenticates itself with settings.ADMIN_TOKEN,
# same gate as the API routes it hits — see app/api/routes/admin.py.
# Restrict network access to this path at your reverse proxy / cloud
# provider (VPN, IP allowlist) — the token is the only application-level
# gate.
from fastapi.responses import FileResponse as _FileResponse
_ADMIN_HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "admin.html")

@app.get("/admin", include_in_schema=False)
async def admin_panel():
    return _FileResponse(_ADMIN_HTML_PATH)

# ── Routes ────────────────────────────────────────────────────────────────────
from app.api.routes import auth, company, leads, telephony, batches, schedules, calls, knowledge, appointments, vobiz_webhook, live_ws
from app.api.routes.payments import router as payments_router
from app.api.routes.human_calls import router as human_calls_router
from app.api.routes.admin import router as admin_router
from app.api.routes.vobiz_stream_webhook import router as vobiz_stream_router
from app.api.routes.telnyx_webhook import router as telnyx_router

app.include_router(auth.router,             prefix="/api/v1/auth",      tags=["Auth"])
app.include_router(payments_router,         prefix="/api/v1",            tags=["Payments"])
app.include_router(human_calls_router,      prefix="/api/v1",            tags=["Human Calls"])
app.include_router(admin_router,            prefix="/api/v1",            tags=["Admin"])
app.include_router(company.router,          prefix="/api/v1/company",   tags=["Company"])
app.include_router(leads.router,            prefix="/api/v1/leads",     tags=["Leads"])
app.include_router(telephony.router,        prefix="/api/v1/telephony", tags=["Telephony"])
app.include_router(batches.router,          prefix="/api/v1/batches",   tags=["Batches"])
app.include_router(schedules.router,        prefix="/api/v1/schedules", tags=["Schedules"])
app.include_router(calls.router,            prefix="/api/v1/calls",     tags=["Calls"])
app.include_router(knowledge.router,        prefix="/api/v1/knowledge", tags=["Knowledge"])
app.include_router(appointments.router,     prefix="/api/v1/appointments", tags=["Appointments"])
app.include_router(vobiz_webhook.router,    prefix="/api/v1/vobiz",     tags=["Vobiz"])
app.include_router(vobiz_stream_router,     prefix="/api/v1/vobiz-stream", tags=["vobiz-stream"])
app.include_router(telnyx_router,           prefix="/api/v1/telnyx",    tags=["Telnyx"])
app.include_router(live_ws.router,          prefix="/api/v1/live",      tags=["Live"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": settings.APP_VERSION}


@app.get("/")
async def root():
    return {"message": f"{settings.APP_NAME} is running"}


