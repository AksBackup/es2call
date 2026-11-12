# ── Backend Dockerfile ──────────────────────────────────────────────────────
# Python 3.11 deliberately, not 3.13 — Python 3.13 removed the stdlib
# `audioop` module, which app/api/routes/vobiz_webhook.py's fallback
# Record-mode flow still imports. 3.11 also matches what's actually been
# tested against throughout development (see requirements.txt comments on
# the pipecat-ai / pipecat-vobiz pins).
FROM python:3.11-slim AS base

# System deps:
#   build-essential + gcc — some pip packages (sentencepiece, tokenizers
#     used transitively by sentence-transformers) need to compile.
#   curl — used by the HEALTHCHECK below.
#   libsndfile1 — needed by some audio-processing deps pulled in via
#     pipecat's silero/onnxruntime stack.
# Cleaned up in the same layer to keep the image smaller.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        curl \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first, as their own layer — this is the single
# biggest Docker build-cache win here: requirements.txt changes far less
# often than application code, so `docker build` skips this ~5-10 minute
# step (torch, sentence-transformers, chromadb are all large) on every
# code-only change.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Now the application code.
COPY app ./app

# Migrations — schema is managed by Alembic now, not create_all(). This
# image can run `alembic upgrade head` (see docker-compose.yml's
# `migrate` service, which does exactly that and runs once per deploy
# before backend/celery-* start).
COPY alembic.ini .
COPY alembic ./alembic

# Non-root user — standard container hardening, no reason to run as root
# here.
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/chroma_data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Schema against Postgres/Neon is created/updated by `alembic upgrade
# head` — see docker-compose.yml's `migrate` service, which runs it once
# before backend/celery-* start. app/core/database.py's create_tables()
# is now a no-op against Postgres and only still auto-creates tables for
# local SQLite dev.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# WEB_CONCURRENCY defaults to 2 — each worker independently loads Silero
# VAD + the Smart Turn ONNX model into memory the first time a streaming
# call hits it (a few hundred MB each), so don't set this arbitrarily
# high on a small container without checking RAM headroom first. Azure B2
# (2 vCPU / 4GB) comfortably fits 2; going to 4 workers on a B2 is where
# you'd likely start seeing OOM kills under concurrent calls.
ENV WEB_CONCURRENCY=2
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY}"]
