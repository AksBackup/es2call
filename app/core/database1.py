from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.core.config import settings

_is_sqlite = "sqlite" in settings.DATABASE_URL

if _is_sqlite:
    # SQLite (local dev only) needs check_same_thread=False and doesn't
    # take a connection pool the way Postgres does.
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        connect_args={"check_same_thread": False},
    )
else:
    # Postgres (Neon in production). Conservative pool sizing + pre-ping
    # so idle-closed connections (Neon does this) surface as a fresh
    # reconnect instead of a random request failure, and so N replicas *
    # (pool_size + max_overflow) stays well under Neon's connection cap.
    #
    # DB_STATEMENT_CACHE_SIZE: leave unset for Neon's *direct* connection
    # string. Set DB_STATEMENT_CACHE_SIZE=0 in .env if DATABASE_URL points
    # at Neon's *pooled* (pgbouncer, transaction-mode) endpoint instead —
    # asyncpg's prepared-statement cache and pgbouncer transaction pooling
    # are incompatible, and asyncpg will otherwise raise
    # "prepared statement ... does not exist" errors under load.
    _connect_args = {}
    if settings.DB_STATEMENT_CACHE_SIZE is not None:
        _connect_args["statement_cache_size"] = settings.DB_STATEMENT_CACHE_SIZE

    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
        connect_args=_connect_args,
    )

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_tables():
    """Create local tables and apply the small additive schema used by the
    current AI appointments/structured-knowledge flow. Existing production
    databases are not rebuilt; only the new column/table are added.

    This is intentionally additive and idempotent so an existing Neon/
    Postgres deployment can boot the new application before a formal
    migration pipeline is added.
    """
    from sqlalchemy import text
    from app.models.models import Appointment

    async with engine.begin() as conn:
        if _is_sqlite:
            if settings.AUTO_CREATE_TABLES_SQLITE_ONLY:
                await conn.run_sync(Base.metadata.create_all)
            else:
                # Still ensure the new appointment table exists when an
                # existing local DB has disabled full create_all.
                await conn.run_sync(lambda sync_conn: Appointment.__table__.create(sync_conn, checkfirst=True))
        else:
            # PostgreSQL/Neon: additive, idempotent schema changes only.
            await conn.execute(text(
                "ALTER TABLE companies ADD COLUMN IF NOT EXISTS business_knowledge JSONB DEFAULT '{}'::jsonb"
            ))
            await conn.run_sync(lambda sync_conn: Appointment.__table__.create(sync_conn, checkfirst=True))
