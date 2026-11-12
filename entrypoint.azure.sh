#!/bin/bash
# Only used by Dockerfile.azure. The VM deployment runs migrations via
# docker-compose.yml's separate one-shot `migrate` service instead —
# there's no equivalent "run once before the others" concept inside a
# single supervisord container, so it happens here, before supervisord
# (and therefore before web/worker/beat) ever starts.
set -e

mkdir -p /app/chroma_data /app/uploads

echo "Running database migrations (alembic upgrade head)..."
alembic upgrade head

echo "Starting supervisord (redis, web, worker, beat)..."
exec supervisord -c /etc/supervisord.conf
