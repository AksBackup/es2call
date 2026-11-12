"""
Alembic migration environment — wired to this app's own models and .env,
so there's exactly one place DATABASE_URL and the model list are defined.

Uses an async engine (asyncpg for Postgres/Neon, aiosqlite for local
SQLite dev) since that's what the app itself uses — sharing
app.core.database's engine setup means migrations see the same
pool/SSL/statement-cache config as the running app, not a second,
possibly-inconsistent one.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from alembic import context

# Make `app.*` importable when alembic is run from the project root
# (both `alembic upgrade head` from a shell and the Dockerfile's
# migration step run with the project root as the working directory).
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.database import Base
# Import every model module so its tables register on Base.metadata —
# autogenerate can only see models that have actually been imported.
from app.models import models  # noqa: F401

config = context.config

# Override alembic.ini's (intentionally blank) sqlalchemy.url with the
# app's own DATABASE_URL from settings/.env — one source of truth.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running against a live DB
    (`alembic upgrade head --sql`) — no DBAPI/connection needed."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
